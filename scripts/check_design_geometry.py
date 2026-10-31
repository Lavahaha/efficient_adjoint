"""Offline geometry checks: python scripts/check_design_geometry.py (no CST)."""
from __future__ import annotations

import copy
import contextlib
from dataclasses import asdict
import importlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eaopt.config import CaseConfig
from eaopt.geometry.design import (
    compose_metal_regions, connection_boundary_mask, design_box, polygon_union, region_rings,
)
from eaopt.geometry.levelset import make_level_set
from eaopt.solver.case import load_case


def geometry(regions):
    return unary_union([Polygon(r.outer, r.holes) for r in regions])


class DesignGeometryChecks(unittest.TestCase):
    cases = ("coupler", "divider", "divider_multifreq", "antenna")

    def synthetic(self):
        return CaseConfig.from_dict({
            "name": "synthetic", "design_region": {
                "box": {"x": [0., 1.], "y": [0., 1.]}, "grid_step_mm": .05},
            "initial_metal": [{"vertices": [[0., 0.], [1., 0.], [1., 1.], [0., 1.]]}],
        })

    def test_all_cases_keep_initial_geometry_when_design_box_shrinks(self):
        for name in self.cases:
            with self.subTest(case=name):
                cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
                original = polygon_union([p.vertices for p in cfg.initial_metal])
                cfg.design_region.box.x = (cfg.design_region.box.x[0] + .5,
                                           cfg.design_region.box.x[1] - .5)
                cfg.validate()
                ls = make_level_set(cfg)
                result = geometry(compose_metal_regions(cfg, ls.phi))
                self.assertLess(result.symmetric_difference(original).area, 1e-10)
                self.assertAlmostEqual(ls.xs[0], cfg.design_region.box.x[0])
                self.assertAlmostEqual(ls.xs[-1], cfg.design_region.box.x[1])

    def test_evolving_geometry_never_changes_outside_initial_metal(self):
        for name in self.cases:
            with self.subTest(case=name):
                cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
                cfg.design_region.box.x = (cfg.design_region.box.x[0] + .5,
                                           cfg.design_region.box.x[1] - .5)
                ls = make_level_set(cfg)
                X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
                phi = np.hypot(X - X.mean(), Y - Y.mean()) - 1.0
                result = geometry(compose_metal_regions(cfg, phi))
                outside = polygon_union([p.vertices for p in cfg.initial_metal]).difference(design_box(cfg))
                self.assertLess(result.difference(design_box(cfg)).symmetric_difference(outside).area, 1e-9)

    def test_corners_select_metal_side_instead_of_shorter_air_side(self):
        cfg = self.synthetic()
        ls = make_level_set(cfg)
        X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
        result = geometry(compose_metal_regions(cfg, X + Y - 1.5))
        self.assertAlmostEqual(result.area, .875)
        self.assertTrue(result.contains(Point(.1, .1)))
        self.assertFalse(result.contains(Point(.9, .9)))

    def test_full_empty_and_holed_design_regions(self):
        cfg = self.synthetic()
        ls = make_level_set(cfg)
        self.assertAlmostEqual(geometry(compose_metal_regions(cfg, -np.ones_like(ls.phi))).area, 1.)
        self.assertTrue(geometry(compose_metal_regions(cfg, np.ones_like(ls.phi))).is_empty)
        X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
        radius = np.hypot(X - .5, Y - .5)
        regions = compose_metal_regions(cfg, (radius - .1) * (radius - .3))
        self.assertEqual(len(regions), 1)
        self.assertEqual(len(regions[0].holes), 1)
        self.assertFalse(geometry(regions).contains(Point(.5, .5)))

    def test_overlapping_initial_polygons_have_no_internal_zero_boundary(self):
        cfg = self.synthetic()
        cfg.initial_metal[0].vertices = [[0., 0.], [.7, 0.], [.7, 1.], [0., 1.]]
        second = copy.deepcopy(cfg.initial_metal[0])
        second.vertices = [[.3, 0.], [1., 0.], [1., 1.], [.3, 1.]]
        cfg.initial_metal.append(second)
        ls = make_level_set(cfg)
        self.assertLess(ls.phi[6, 10], -.29)

    def test_frozen_connections_are_pinned_without_freezing_growth_edges(self):
        for name in self.cases:
            with self.subTest(case=name):
                cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
                cfg.design_region.box.x = (cfg.design_region.box.x[0] + .5,
                                           cfg.design_region.box.x[1] - .5)
                ls = make_level_set(cfg)
                _, model = load_case(cfg)
                pinned = connection_boundary_mask(ls, cfg, model.fixed_metal_polygons())
                self.assertTrue(pinned[0].any())
                self.assertFalse(pinned[:, 0].any())
                self.assertFalse(pinned[:, -1].any())

    def test_default_antenna_feed_contact_survives_feature_filter(self):
        from eaopt.optimize.constraints import apply_min_feature

        cfg = CaseConfig.from_yaml(ROOT / "configs/antenna.yaml")
        ls = make_level_set(cfg)
        _, model = load_case(cfg)
        pinned = connection_boundary_mask(ls, cfg, model.fixed_metal_polygons())
        initial_values = ls.phi.copy()
        apply_min_feature(ls, cfg.constraints.min_feature_mm)
        ls.phi[pinned] = initial_values[pinned]
        metal = geometry(compose_metal_regions(cfg, ls.phi))
        feed = polygon_union(model.fixed_metal_polygons())
        self.assertGreaterEqual(metal.intersection(feed).length, 4.9)

    def test_cst_and_manual_snapshot_paths_preserve_the_full_geometry(self):
        fake = {name: types.ModuleType(name) for name in ("cst", "cst.interface", "cst.results")}
        fake["cst"].__path__ = []
        with patch.dict(sys.modules, fake):
            cst = importlib.import_module("eaopt.solver.cst")
            update = importlib.import_module("scripts.cst_update")
        with tempfile.TemporaryDirectory() as temporary:
            for name in self.cases:
                with self.subTest(case=name):
                    cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
                    cfg.design_region.box.x = (cfg.design_region.box.x[0] + .5,
                                               cfg.design_region.box.x[1] - .5)
                    cfg.output.dir = str(Path(temporary) / name)
                    setup, model = load_case(cfg)
                    ls = make_level_set(cfg)
                    rings = update._contours_from_phi(
                        cfg, {"phi": ls.phi, "x": ls.xs, "y": ls.ys}, log=lambda _: None)
                    solver = cst.CstSolver(cfg, setup=setup, model=model, clear_results=False, log=lambda _: None)
                    solver._ensure_projects = lambda: None
                    solver._open = lambda _: types.SimpleNamespace(
                        model3d=types.SimpleNamespace(add_to_history=lambda *args: None))
                    solver.begin_iteration(0)
                    solver.build_model(rings)
                    saved = json.loads((Path(cfg.output.dir) / "iter_000/shape.json").read_text())
                    result = polygon_union(saved["polygons"])
                    initial = polygon_union([p.vertices for p in cfg.initial_metal])
                    self.assertLess(result.symmetric_difference(initial).area, 1e-10)

    def test_all_runner_cases_preserve_frozen_geometry_and_save_full_shapes(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot  # Load class definitions before patching sys.modules.
        import adjoint_levelset as als
        from eaopt import artifacts, runner
        from eaopt.adjoint.fields import FieldGrid
        from eaopt.solver.base import Solution

        fake = {name: types.ModuleType(name) for name in ("cst", "cst.interface", "cst.results")}
        fake["cst"].__path__ = []
        with patch.dict(sys.modules, fake):
            cst = importlib.import_module("eaopt.solver.cst")

        class Solver:
            def __init__(self, cfg, **kwargs):
                self.cfg, self.M, self.setup = cfg, kwargs["model"], kwargs["setup"]
                self.values = iter([.5, .4])
                self.grid = FieldGrid((0., 0., -.1), (1., 1., .1), np.ones((3, 3, 3, 3), complex))

            def begin_iteration(self, it):
                self.it = it

            def build_model(self, rings, fixed):
                self.last_prepared_polys = rings
                artifacts.save_shape(self.cfg.output.dir, self.it, rings)

            def solve_forward(self):
                value = next(self.values)
                params = {(p, self.cfg.objective.from_port): value for p in self.setup.ports}
                frequencies = {}
                if self.cfg.objective.frequencies_ghz:
                    from eaopt.solver.base import FrequencyResult
                    frequencies = {f: FrequencyResult(params, self.grid, self.grid)
                                   for f in self.cfg.objective.frequencies_ghz}
                return Solution(params, self.grid, self.grid, .5, frequency_results=frequencies)

            def export_field_for_plot(self, box, *, eval_id, tag):
                assert self.it == eval_id
                return self.grid

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            for name in self.cases:
                with self.subTest(case=name):
                    cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
                    cfg.design_region.box.x = (cfg.design_region.box.x[0] + .5,
                                               cfg.design_region.box.x[1] - .5)
                    config_path = Path(temporary) / f"{name}.yaml"
                    config_path.write_text(yaml.safe_dump(asdict(cfg)), encoding="utf-8")
                    out = Path(temporary) / name
                    velocity = lambda opt, ls, fwd: (opt.design_mask * 1e6, 0., None, None)
                    with patch.dict(sys.modules, {"eaopt.solver.cst": cst}), \
                            patch.object(cst, "CstSolver", Solver), \
                            patch.object(runner, "make_log", return_value=lambda _: None), \
                            patch.object(als.AdjointLevelSetOptimizer, "velocity", velocity), \
                            patch.object(sys, "argv", ["run", str(config_path), "--output-dir", str(out), "--iterations", "1"]), \
                            contextlib.redirect_stdout(io.StringIO()):
                        runner.main(expected_case=name, maximize=(name != "antenna"))
                    saved = json.loads((out / "final_shape.json").read_text())
                    full = unary_union([Polygon(r["outer"], r["holes"]) for r in saved["regions"]])
                    original = polygon_union([p.vertices for p in cfg.initial_metal])
                    domain = design_box(cfg)
                    self.assertLess(full.difference(domain).symmetric_difference(original.difference(domain)).area, 1e-9)
                    with np.load(out / "phi_final.npz") as state:
                        ls = make_level_set(cfg)
                        _, model = load_case(cfg)
                        pinned = connection_boundary_mask(ls, cfg, model.fixed_metal_polygons())
                        np.testing.assert_array_equal(state["phi"][pinned], ls.phi[pinned])
                    self.assertTrue((out / "iter_001/evaluation.png").is_file())


if __name__ == "__main__":
    unittest.main()
