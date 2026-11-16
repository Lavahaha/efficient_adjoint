"""Replay generated geometry commands without CST to catch stale union solids."""
import re
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from eaopt.config import CaseConfig
from eaopt.geometry.contour import MetalRegion, classify_metal_regions
from eaopt.solver.case import known_cases, load_case
from eaopt.solver.cst_boolean import plan_pec_unions
from eaopt.solver import cst_model_patch_antenna as patch

ROOT = Path(__file__).resolve().parents[1]


def replay(code, solids):
    """Small geometry-only interpreter; fail on missing Add/Subtract operands."""
    pattern = (r'With (?:Extrude|Brick)\n.*?End With'
               r'|Component.Delete "[^"]+"|Solid.Delete "[^"]+"'
               r'|Solid.(?:Add|Subtract) "[^"]+", "[^"]+"')
    for match in re.finditer(pattern, code, re.S):
        command = match[0]
        quoted = re.findall(r'"([^"]+)"', command)
        if command.startswith("With "):
            name = re.search(r'\.Component "([^"]+)"', command)[1] + ":"
            name += re.search(r'\.Name "([^"]+)"', command)[1]
            if name in solids:
                raise AssertionError(f"Duplicate solid: {name}")
            if command.startswith("With Extrude"):
                points = re.findall(r'\.(?:Point|LineTo) "([^"]+)", "([^"]+)"', command)
                polygon = Polygon([(float(x), float(y)) for x, y in points])
            else:
                x0, x1 = map(float, re.search(r'\.Xrange "([^"]+)", "([^"]+)"', command).groups())
                y0, y1 = map(float, re.search(r'\.Yrange "([^"]+)", "([^"]+)"', command).groups())
                polygon = box(x0, y0, x1, y1)
            solids[name] = polygon
        elif command.startswith("Component.Delete"):
            for name in list(solids):
                if name.startswith(quoted[0] + ":"):
                    del solids[name]
        elif command.startswith("Solid.Delete"):
            solids.pop(quoted[0], None)
        else:
            first, second = quoted
            # Coaxial bore subtraction is outside this planar signal-metal replay.
            if first in ("component1:substrate", "component1:ground") and second not in solids:
                continue
            operation = solids[first].union if command.startswith("Solid.Add") else solids[first].difference
            solids[first] = operation(solids.pop(second))


class PecBooleanTests(unittest.TestCase):
    def test_bridge_merges_two_feeds_and_preserves_hole_and_island(self):
        main = box(0, 0, 4, 4).difference(box(1, 1, 2, 2))
        island = box(8, 8, 9, 9)
        fixed = [("feed:left", list(box(-1, 0, 0, 4).exterior.coords)),
                 ("feed:right", list(box(4, 0, 5, 4).exterior.coords))]
        commands, survivors = plan_pec_unions(
            [("design_region:main", main), ("design_region:island", island)], fixed)
        self.assertEqual([name for name, _ in survivors], ["feed:left", "design_region:island"])
        self.assertEqual("".join(commands).count("Solid.Add "), 2)
        self.assertEqual(len(survivors[0][1].interiors), 1)
        self.assertAlmostEqual(survivors[0][1].area, 23)

    def test_point_contact_and_gap_are_not_bridged(self):
        for design in (box(1, 1, 2, 2), box(1.00001, 0, 2, 1)):
            commands, survivors = plan_pec_unions(
                [("design_region:main", design)],
                [("feed:fixed", list(box(0, 0, 1, 1).exterior.coords))])
            self.assertEqual(commands, [])
            self.assertEqual(len(survivors), 2)

    def test_patch_two_updates_remove_previous_union_and_keep_fixed_component(self):
        cfg = CaseConfig.from_yaml(ROOT / "configs/patch_antenna.yaml")
        solids = {}
        replay("".join(patch.model_blocks([p.vertices for p in cfg.initial_metal])), solids)
        self.assertIn("component1:feed_fixed", solids)
        self.assertNotIn("design_region:initial_metal_0", solids)
        ground = solids["component1:ground"]
        feed = Polygon(patch.fixed_metal_polygons()[0])
        for top in (-16, -18):
            metal = box(-1.69, -20, 1.69, top).difference(box(-.5, -19.5, .5, -19))
            region = MetalRegion(np.asarray(metal.exterior.coords),
                                 tuple(np.asarray(h.coords) for h in metal.interiors))
            island = MetalRegion(np.asarray(box(10, 10, 11, 11).exterior.coords))
            code = patch.design_region_update([region, island], .018)
            self.assertLess(code.index('Solid.Delete "component1:feed_fixed"'), code.index("With Extrude"))
            self.assertLess(code.index("Solid.Subtract"), code.index("Solid.Add"))
            self.assertIn('If Solid.DoesExist("design_region:antenna_metal_0") Then Err.Raise', code)
            replay(code, solids)
            self.assertTrue(solids["component1:feed_fixed"].equals(feed.union(metal)))
            self.assertEqual(len(solids["component1:feed_fixed"].interiors), 1)
            self.assertIn("design_region:antenna_metal_1", solids)
            self.assertNotIn("design_region:antenna_metal_0", solids)
            self.assertTrue(solids["component1:ground"].equals(ground))
            self.assertIn("component1:feed_fixed", dict(patch.merged_design_solids([region, island])))
        self.assertEqual(solids["component1:feed_fixed"].bounds[3], -18)

    def test_all_cases_initialization_and_repeated_updates_match_exact_union(self):
        for case in known_cases():
            cfg = CaseConfig.from_yaml(ROOT / f"configs/{case}.yaml")
            setup, model = load_case(cfg)
            polygons = [p.vertices for p in cfg.initial_metal]
            design = unary_union([Polygon(p) for p in polygons])
            fixed = model.fixed_metal_solids()
            expected = unary_union([design] + [Polygon(p) for _, p in fixed])
            solids = {}
            replay("".join(model.model_blocks(polygons)), solids)
            rings = [np.asarray(Polygon(p).exterior.coords) for p in polygons]
            regions = classify_metal_regions(rings) if getattr(model, "SUPPORTS_NESTED_CONTOURS", False) else rings
            for _ in range(2):
                replay(model.design_region_update(regions, setup.metal_thickness_mm), solids)
                signal = [p for name, p in solids.items()
                          if name.startswith("design_region:") or name in dict(fixed)]
                with self.subTest(case=case):
                    self.assertLess(unary_union(signal).symmetric_difference(expected).area, 1e-10)
                    self.assertTrue(all(not name.startswith("design_region:") for name, _ in fixed))

    def test_non_pec_update_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "PEC"):
            patch.design_region_update([np.asarray(box(0, 0, 1, 1).exterior.coords)], .018,
                                       material="Copper")

    def test_automatic_and_manual_updates_write_same_replayable_history(self):
        try:
            from eaopt.solver.cst import _update_design_region as automatic
            from scripts.cst_update import _update_design_region as manual
        except SystemExit:
            self.skipTest("CST Python interface cannot be loaded")
        cfg = CaseConfig.from_yaml(ROOT / "configs/patch_antenna.yaml")
        setup, model = load_case(cfg)
        region = MetalRegion(np.asarray(Polygon(cfg.initial_metal[0].vertices).exterior.coords))
        commands = []
        for update in (automatic, manual):
            backend = Mock()
            backend.add_to_history.return_value = True
            update(backend, [region], setup, model, what="test", log=lambda _: None)
            backend.add_to_history.assert_called_once()
            commands.append(backend.add_to_history.call_args.args[1])
        self.assertEqual(commands[0], commands[1])
        self.assertIn('Solid.Add "component1:feed_fixed", "design_region:antenna_metal_0"', commands[0])

    def test_patch_audit_reads_final_feed_union_and_rejects_old_design_volume(self):
        region = MetalRegion(np.asarray(box(-1.69, -20, 1.69, -18).exterior.coords))
        for volume in (40.56 * .018, 6.76 * .018):
            with self.subTest(volume=volume), tempfile.TemporaryDirectory() as folder:
                destination = Path(folder) / "native.json"
                backend = Mock()
                backend._execute_vba_code.side_effect = lambda _: destination.with_suffix(".txt").write_text(
                    f"{volume}\n8\n-1.69\n1.69\n-30\n-18\n0\n0.018\n")
                if volume == 40.56 * .018:
                    rows = patch.audit_design_regions(backend, [region], .018, destination)
                    self.assertEqual(rows[0]["solid"], "component1:feed_fixed")
                else:
                    with self.assertRaisesRegex(ValueError, "solid differs"):
                        patch.audit_design_regions(backend, [region], .018, destination)
                    self.assertFalse(json.loads(destination.read_text())["passed"])
                code = backend._execute_vba_code.call_args.args[0]
                self.assertIn('Solid.GetVolume("component1:feed_fixed")', code)
                self.assertNotIn("design_region:antenna_metal_0", code)


if __name__ == "__main__":
    unittest.main()
