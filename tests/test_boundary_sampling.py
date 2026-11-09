"""Geometry-only regressions; field stubs never connect to CST."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.runner import CstAdjointLevelSetProblem
from eaopt.solver.case import load_case


class BoundarySamplingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = CaseConfig.from_yaml(
            Path(__file__).resolve().parents[1] / "configs/coupler.yaml")
        self.setup, model = load_case(self.cfg)
        self.solver = SimpleNamespace(
            M=model,
            last_prepared_polys=[np.asarray([*p.vertices, p.vertices[0]])
                                 for p in self.cfg.initial_metal],
            sample_fields=Mock(side_effect=lambda points, **_: (
                np.ones((3, len(points)), complex), np.ones((3, len(points)), complex))),
            solve_backward=Mock(),
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        (self.output / "iter_000").mkdir()
        self.problem = CstAdjointLevelSetProblem(
            self.cfg, self.setup, self.solver, .05, 1., self.output,
            log=lambda _: None, boundary_sampling="cst-points", adjoint_source="unit")
        self.fwd = SimpleNamespace(extra={"eval_id": 0, "output_power_w": .1})

    def boundary(self, points, normals):
        return als.Boundary(np.array(points, float), np.array(normals, float),
                            np.ones(len(points)), np.ones(len(points), bool))

    def test_new_fixed_contact_is_excluded_but_nearby_edge_is_sampled(self):
        boundary = self.boundary([[12., -2.5328218], [11.9864, -1.8965]],
                                 [[.4687767, -.8833167], [0., -1.]])
        gradient = self.problem.boundary_gradient(None, self.fwd, boundary)
        np.testing.assert_array_equal(boundary.active, [False, True])
        self.assertEqual(gradient[0], 0.)
        self.assertGreater(gradient[1], 0.)
        self.assertEqual(self.solver.sample_fields.call_count, 2)
        with np.load(self.output / "iter_000/boundary_fields.npz") as saved:
            np.testing.assert_array_equal(saved["boundary_indices"], [1])
            np.testing.assert_array_equal(saved["excluded_contact_indices"], [0])
            np.testing.assert_array_equal(saved["active"], boundary.active)

    def test_frozen_initial_metal_outside_smaller_design_box_is_contact(self):
        self.cfg.design_region.box.x = (.5, 11.5)
        boundary = self.boundary([[.5, -1.6]], [[-.6, .8]])
        gradient = self.problem.boundary_gradient(None, self.fwd, boundary)
        np.testing.assert_array_equal(boundary.active, [False])
        np.testing.assert_array_equal(gradient, [0.])
        self.solver.sample_fields.assert_not_called()
        self.solver.solve_backward.assert_not_called()

    def test_noncontact_collisions_still_fail_and_save_diagnostics(self):
        for point, normal, reason in (
                ([11.99, -2.2], [.6, -.8], "in_pec"),
                ([20., 0.], [1., 0.], "outside_substrate")):
            with self.subTest(reason=reason):
                boundary = self.boundary([point], [normal])
                with self.assertRaisesRegex(ValueError, reason):
                    self.problem.boundary_gradient(None, self.fwd, boundary)
                self.assertTrue(boundary.active[0])
                diagnostic = json.loads((self.output / "iter_000/invalid_boundary_samples.json")
                                        .read_text(encoding="utf-8"))
                self.assertEqual(diagnostic["eval_id"], 0)
                self.assertEqual(diagnostic["excluded_contact_indices"], [])
                self.assertIn(f"'{reason}': True", diagnostic["error"])
                self.assertIn("sample_mm", diagnostic["error"])
        self.solver.sample_fields.assert_not_called()
        self.solver.solve_backward.assert_not_called()


if __name__ == "__main__":
    unittest.main()
