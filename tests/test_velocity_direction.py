"""The actual velocity extension must retain the boundary ascent direction."""
import unittest

import numpy as np
from scipy.interpolate import RegularGridInterpolator

import adjoint_levelset as als


class VelocityDirectionTests(unittest.TestCase):
    def setUp(self):
        theta = np.sort(np.random.default_rng(0).uniform(0, 2*np.pi, 48))
        points = 1.5*np.column_stack((np.cos(theta), np.sin(theta)))
        gaps = np.diff(np.r_[theta, theta[0]+2*np.pi])
        weights = .75*(gaps+np.roll(gaps, 1))
        self.grid = als.Grid2D(61, 61, .1, -3., -3.)
        x, y = self.grid.mesh()
        self.ls = als.LevelSet(self.grid, np.hypot(x, y)-1.5)
        self.boundary = als.Boundary(points, points/1.5, weights, np.ones(48, bool))

    def sample(self, velocity):
        return RegularGridInterpolator((self.grid.x, self.grid.y), velocity)(self.boundary.pts)

    def work(self, gradient, velocity):
        return float(np.sum(self.boundary.weights*gradient*self.sample(velocity)))

    def velocity(self, gradient, **kwargs):
        return als.boundary_to_grid_velocity(self.ls, self.boundary, gradient, **kwargs)

    def test_known_opposite_gradients_no_longer_reverse_direction(self):
        positive, negative = np.zeros(48), np.zeros(48)
        positive[35], negative[36] = .85, .5
        gradient = positive-negative
        # Each nonnegative input uses the original linear extension. Their
        # difference reconstructs its old, invalid answer without copying it.
        old_answer = self.velocity(positive)-self.velocity(negative)
        self.assertLess(self.work(gradient, old_answer), -.01)
        repaired = self.velocity(gradient)
        self.assertGreater(self.work(gradient, repaired), 0.)
        np.testing.assert_allclose(self.velocity(-gradient), -repaired, atol=1e-14)

    def test_nonnegative_inputs_keep_original_linear_results(self):
        first, second = np.zeros(48), np.zeros(48)
        first[10], second[35] = 1., 2.
        np.testing.assert_array_equal(self.velocity(first+second), self.velocity(first)+self.velocity(second))

    def test_positive_extension_is_symmetric_in_boundary_quadrature(self):
        x, _ = self.grid.mesh()
        mask = (x >= 0).astype(float)
        band = abs(self.ls.phi) < 4*self.grid.dx
        for sigma in (0., 1.5):
            vectors = np.random.default_rng(3).normal(size=(4, 48))
            velocities = [als._positive_boundary_velocity(self.ls, self.boundary, g, mask, band, sigma) for g in vectors]
            for gradient, velocity in zip(vectors, velocities):
                self.assertGreaterEqual(self.work(gradient, velocity), -1e-12)
                np.testing.assert_array_equal(velocity[mask == 0], 0.)
            for i in range(4):
                for j in range(4):
                    self.assertAlmostEqual(self.work(vectors[i], velocities[j]), self.work(vectors[j], velocities[i]), places=12)


if __name__ == '__main__':
    unittest.main()
