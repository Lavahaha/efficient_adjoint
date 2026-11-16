"""All initialization templates must use the supplied YAML geometry exactly."""
import copy
import re
import unittest
from pathlib import Path

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from eaopt.config import CaseConfig
from eaopt.geometry.design import polygon_union
from eaopt.solver.case import known_cases, load_case

ROOT = Path(__file__).resolve().parents[1]


def initial_solid(commands):
    """Read the generated extrusion points and subtract the hole tools."""
    solids, holes = [], []
    for command in commands:
        if not command.startswith("With Extrude"):
            continue
        name = re.search(r'\.Name "([^"]+)"', command)[1]
        if not name.startswith(("initial_metal_", "initial_hole_")):
            continue
        points = re.findall(r'\.(?:Point|LineTo) "([^"]+)", "([^"]+)"', command)
        polygon = Polygon([(float(x), float(y)) for x, y in points])
        (holes if name.startswith("initial_hole_") else solids).append(polygon)
    return unary_union(solids).difference(unary_union(holes))


class InitialMetalTests(unittest.TestCase):
    def test_every_case_and_excitation_uses_modified_yaml(self):
        for name in known_cases():
            cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
            setup, model = load_case(cfg)
            original = [p.vertices for p in cfg.initial_metal]
            changed = copy.deepcopy(original)
            # Stretch the first polygon, then add disconnected metal outside D.
            top = max(y for _, y in changed[0])
            for p in changed[0]:
                if p[1] == top:
                    p[1] += .23
            changed.append([[70., 0.], [71., 0.], [71., 1.], [70., 1.]])
            self.assertGreater(polygon_union(original).symmetric_difference(polygon_union(changed)).area, 1.)
            for tag in setup.project_names:
                for solver in (("td",) if cfg.objective.frequencies_ghz else ("td", "fd")):
                    with self.subTest(case=name, tag=tag, solver=solver):
                        kwargs = dict(initial_metal=changed, solver_type=solver)
                        if cfg.objective.frequencies_ghz:
                            kwargs["frequencies_ghz"] = cfg.objective.frequencies_ghz
                        blocks = model.template_blocks(name, setup.stimulus(tag, cfg.objective), **kwargs)
                        actual = initial_solid([code for _, code in blocks])
                        self.assertLess(actual.symmetric_difference(polygon_union(changed)).area, 1e-10)
                        self.assertTrue(actual.covers(box(70., 0., 71., 1.)))
                        view = model.layout_view(changed)
                        for p in changed:
                            self.assertIn(p, [pts for kind, pts, _ in view["shapes"] if kind == "metal"])
                        if cfg.objective.frequencies_ghz:
                            titles = [title for title, _ in blocks]
                            for freq in cfg.objective.frequencies_ghz:
                                self.assertIn(f"Monitor Efield {freq:g} GHz", titles)

    def test_union_and_enclosed_void_match_the_optimization_geometry(self):
        # Overlapping bars form a ring; neither overlap nor the enclosed air is filled twice.
        polygons = [list(box(*bounds).exterior.coords) for bounds in
                    [(0, 0, 4, 1), (0, 3, 4, 4), (0, 0, 1, 4), (3, 0, 4, 4)]]
        for name in known_cases():
            with self.subTest(case=name):
                cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
                _, model = load_case(cfg)
                commands = model.model_blocks(polygons)
                self.assertAlmostEqual(initial_solid(commands).area, 12.)
                self.assertTrue(any(c.startswith("Solid.Subtract") and "initial_hole_" in c for c in commands))

    def test_no_case_falls_back_to_a_hardcoded_shape(self):
        for name in known_cases():
            cfg = CaseConfig.from_yaml(ROOT / f"configs/{name}.yaml")
            _, model = load_case(cfg)
            with self.subTest(case=name):
                with self.assertRaises(TypeError):
                    model.template_blocks(name, 1)
                for invalid in ([], [[[0, 0], [1, 1], [0, 1], [1, 0]]]):
                    with self.assertRaises(ValueError):
                        model.model_blocks(invalid)


if __name__ == "__main__":
    unittest.main()
