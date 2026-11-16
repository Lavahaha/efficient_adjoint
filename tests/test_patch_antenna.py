"""Drawing dimensions, YAML/CST agreement, and the fixed-feed connection."""
import unittest
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from eaopt.config import CaseConfig
from eaopt.geometry.design import compose_metal_regions, connection_boundary_mask
from eaopt.geometry.levelset import make_level_set
from eaopt.solver.case import load_case
from eaopt.solver import cst_model_patch_antenna as model

ROOT = Path(__file__).resolve().parents[1]


class PatchAntennaTests(unittest.TestCase):
    def setUp(self):
        self.cfg = CaseConfig.from_yaml(ROOT / "configs/patch_antenna.yaml")

    def test_config_matches_template_and_centered_design_box(self):
        setup, template = load_case(self.cfg)
        self.assertIs(template, model)
        self.assertEqual(self.cfg.design_region.box.x, (-20., 20.))
        self.assertEqual(self.cfg.design_region.box.y, (-20., 20.))
        self.assertIn(self.cfg.initial_metal[0].vertices,
                      [p for kind, p, _ in model.layout_view([p.vertices for p in self.cfg.initial_metal])["shapes"]
                       if kind == "metal"])
        np.testing.assert_allclose(self.cfg.fixed_region[0].vertices, model.fixed_metal_polygons()[0])
        self.assertEqual(setup.frequency_ghz, 7.5)
        self.assertEqual(setup.eps_r, 3.55)
        self.assertEqual(setup.substrate_h_mm, 1.524)
        self.assertEqual(setup.metal_thickness_mm, .018)
        setup.validate_objective(self.cfg.objective)

    def test_exact_patch_slots_and_feed_dimensions(self):
        metal = unary_union([Polygon(self.cfg.initial_metal[0].vertices),
                             Polygon(model.fixed_metal_polygons()[0])])
        top = max(y for _, y in self.cfg.initial_metal[0].vertices)
        expected = box(-6.625, -4.965, 6.625, top).difference(unary_union([
            box(-2.19, -4.965, -1.69, -1.375),
            box(1.69, -4.965, 2.19, -1.375),
        ])).union(box(-1.69, -30., 1.69, -1.375))
        self.assertTrue(metal.is_valid)
        self.assertEqual(metal.geom_type, "Polygon")
        self.assertLess(metal.symmetric_difference(expected).area, 1e-12)
        self.assertEqual(len(metal.interiors), 0)
        self.assertAlmostEqual(self.cfg.initial_metal[0].vertices[2][1] - model.BOARD[0], 28.625)
        self.assertGreater(model.AIR_H, 5 * model.SUB_H)

    def test_initial_geometry_and_connection_survive_feature_filter(self):
        from eaopt.optimize.constraints import apply_min_feature

        ls = make_level_set(self.cfg)
        regions = compose_metal_regions(self.cfg, ls.phi)
        metal = unary_union([Polygon(r.outer, r.holes) for r in regions])
        self.assertLess(metal.symmetric_difference(Polygon(self.cfg.initial_metal[0].vertices)).area, 1e-12)
        pinned = connection_boundary_mask(ls, self.cfg, model.fixed_metal_polygons())
        self.assertTrue(pinned[:, 0].any())
        self.assertFalse(pinned[0].any())
        original = ls.phi.copy()
        apply_min_feature(ls, self.cfg.constraints.min_feature_mm)
        ls.phi[pinned] = original[pinned]
        regions = compose_metal_regions(self.cfg, ls.phi)
        metal = unary_union([Polygon(r.outer, r.holes) for r in regions])
        self.assertGreater(metal.intersection(Polygon(model.fixed_metal_polygons()[0])).length, 3.19)
        for x in (-1.94, 1.94):
            self.assertFalse(metal.covers(Point(x, -3.0)))

    def test_microstrip_port_ground_and_both_solvers(self):
        commands = "".join(model.model_blocks([p.vertices for p in self.cfg.initial_metal]))
        self.assertIn('.Orientation "ymin"', model._feed_port())
        self.assertIn('.Yrange "-30", "-30"', model._feed_port())
        self.assertIn('.PortOnBound "False"', model._feed_port())
        self.assertIn('.ReferencePlaneDistance "0"', model._feed_port())
        self.assertIn('.Name "ground"', commands)
        self.assertNotIn("Solid.Subtract", commands)
        self.assertNotIn("Cylinder", commands)
        for solver in ("td", "fd"):
            blocks = model.template_blocks("test", 1, solver,
                                           initial_metal=[p.vertices for p in self.cfg.initial_metal])
            self.assertTrue(blocks)
            self.assertIn("7.5", "".join(command for _, command in blocks))
        with self.assertRaises(ValueError):
            model.setting_blocks(2)


if __name__ == "__main__":
    unittest.main()
