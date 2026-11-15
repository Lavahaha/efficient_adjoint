"""Whole-iteration displacement caps must not alter velocity or default steps."""
import unittest
from types import SimpleNamespace

import numpy as np

import adjoint_levelset as als


class PlaneProblem:
    def __init__(self):
        self.solves = 0

    def forward(self, ls):
        self.solves += 1
        return SimpleNamespace(extra={})

    def objective_value(self, fwd):
        return 1.0  # Equal FoM must still be committed: no acceptance filtering.


class PrescribedVelocity(als.AdjointLevelSetOptimizer):
    def __init__(self, problem, grid, config, speeds):
        super().__init__(problem, np.ones((grid.nx, grid.ny)), config)
        self.speeds = iter(speeds)

    def velocity(self, ls, fwd):
        return np.full(ls.phi.shape, next(self.speeds)), 1.0, None, None


class IterationDisplacementTests(unittest.TestCase):
    def run_plane(self, cap):
        grid = als.Grid2D(31, 11, .2, -3., -1.)
        x, _ = grid.mesh()
        problem = PlaneProblem()
        optimizer = PrescribedVelocity(problem, grid,
            als.OptimizerConfig(pseudo_time_step=1e-8, max_step_mm=cap),
            [1e8, 2e8, 1e6])
        final = optimizer.run(als.LevelSet(grid, x), n_iter=3)
        return x, final, optimizer, problem

    def test_cap_preserves_direction_and_recovers_base_time_when_speed_falls(self):
        x, final, optimizer, problem = self.run_plane(.1)
        records = optimizer.history
        np.testing.assert_allclose([r['pseudo_time_step_used'] for r in records],
                                   [1e-9, .5e-9, 1e-8])
        np.testing.assert_allclose([r['predicted_displacement'] for r in records],
                                   [.1, .1, .01])
        # A plane moving at constant normal speed has phi=x-total_displacement.
        np.testing.assert_allclose(final.phi[10:20], (x-.21)[10:20], atol=1e-12)
        self.assertTrue(all(r['updated'] for r in records))
        self.assertEqual(problem.solves, 4)

    def test_default_retains_configured_fixed_time(self):
        _, _, optimizer, _ = self.run_plane(None)
        self.assertTrue(all(r['pseudo_time_step_used']==1e-8 for r in optimizer.history))
        np.testing.assert_allclose([r['predicted_displacement'] for r in optimizer.history],
                                   [1., 2., .01])

    def test_invalid_cap_fails_before_forward_solve(self):
        grid = als.Grid2D(5, 5, .2)
        x, _ = grid.mesh()
        for value in (0., -1., np.nan, np.inf):
            with self.subTest(value=value):
                problem = PlaneProblem()
                optimizer = PrescribedVelocity(problem, grid,
                    als.OptimizerConfig(max_step_mm=value), [1.])
                with self.assertRaises(ValueError):
                    optimizer.run(als.LevelSet(grid, x))
                self.assertEqual(problem.solves, 0)


if __name__ == '__main__':
    unittest.main()
