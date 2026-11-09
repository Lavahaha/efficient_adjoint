"""Analytic field checks: python scripts/check_shape_derivative_mine.py (no CST)."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adjoint_levelset as als
from eaopt.runner import CstAdjointLevelSetProblem


class ShapeDerivativeMineChecks(unittest.TestCase):
    def setUp(self):
        # The second sample rotates the same fields to an oblique boundary.
        normals = np.array([[1., 0.], [.6, .8]])
        self.boundary = als.Boundary(
            np.zeros((2, 2)), normals, np.ones(2), np.ones(2, bool))
        n = np.vstack([normals.T, np.zeros(2)])
        t = np.vstack([-normals[:, 1], normals[:, 0], np.zeros(2)])
        z = np.array([[0.], [0.], [1.]])
        self.fields = [
            (1 + 2j) * n + 17 * t + 23 * z,
            99 * n + (2 + 1j) * t + (3 - 2j) * z,
            (3 - 1j) * n - 19 * t - 29 * z,
            -77 * n + (-1 + 4j) * t + (2 + 3j) * z,
        ]

    def evaluate(self, fields=None, omega=3., eps=2., mu=5., boundary=None):
        return als.shape_derivative_mine(
            self.boundary if boundary is None else boundary,
            *(self.fields if fields is None else fields), omega, eps, mu)

    def test_formula_projection_and_frequency_independence(self):
        # E product = 5+5j, tangential H product = 6+12j.
        np.testing.assert_allclose(self.evaluate(), [40., 40.])
        np.testing.assert_allclose(self.evaluate(mu=0.), [10., 10.])
        np.testing.assert_allclose(self.evaluate(eps=0.), [30., 30.])
        np.testing.assert_allclose(self.evaluate(omega=6.), [40., 40.])
        self.assertFalse(np.iscomplexobj(self.evaluate()))

    def test_bilinear_products_and_objective_source(self):
        ef, hf, eb, hb = self.fields
        np.testing.assert_allclose(
            self.evaluate([1j * ef, 1j * hf, 1j * eb, 1j * hb]), [-40., -40.])
        # Re[(.6-.8j) * (40+70j)] = 80; weight precedes Re[].
        weight = .6 - .8j
        np.testing.assert_allclose(
            self.evaluate([ef, hf, weight * eb, weight * hb]), [80., 80.])

    def test_original_kernel_is_unchanged(self):
        value = als.shape_derivative_from_fields(
            self.boundary, *self.fields, 2 - 3j, 2., 5.)
        np.testing.assert_allclose(value, [290 + 20j, 290 + 20j])

    def test_invalid_frequency_and_fields(self):
        for omega in (0., -1., np.nan, np.inf):
            with self.subTest(omega=omega), self.assertRaises(ValueError):
                self.evaluate(omega=omega)
        for index in range(4):
            for invalid in (np.zeros((2, 2)), np.full((3, 2), np.nan)):
                fields = list(self.fields)
                fields[index] = invalid
                with self.subTest(field=index), self.assertRaises(ValueError):
                    self.evaluate(fields)

    def test_empty_boundary(self):
        boundary = als.Boundary(np.empty((0, 2)), np.empty((0, 2)),
                                np.empty(0), np.empty(0, bool))
        result = self.evaluate([np.empty((3, 0))] * 4, boundary=boundary)
        self.assertEqual(result.shape, (0,))


class ExperimentalSourceChecks(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.problem = object.__new__(CstAdjointLevelSetProblem)
        self.problem.adjoint_source = "objective"
        self.problem.omega = 3.
        self.problem._write_eval = self.rows.append

    def source(self, s, pm):
        return self.problem._source_amplitude(
            als.ForwardResult(s=s, fields=None,
                              extra={"eval_id": 0, "output_power_w": pm}))

    def test_frequency_scaling_and_phase(self):
        s = .3 + .4j
        pm = abs(s) ** 2 * 2.0
        expected = -2.4 - 1.8j
        np.testing.assert_allclose(self.source(s, pm), expected)
        np.testing.assert_allclose(self.source(s, 4 * pm), expected)
        self.problem.omega = 6.
        np.testing.assert_allclose(self.source(s, pm), 2 * expected)
        self.assertAlmostEqual(abs(self.source(s, pm)), self.problem.omega)
        self.assertEqual(self.rows[0]["output_power_w"], pm)
        self.assertEqual(self.rows[0]["omega_rad_per_s"], 3.)
        self.assertAlmostEqual(self.rows[0]["amplitude_real"], expected.real)
        self.assertAlmostEqual(self.rows[0]["amplitude_imag"], expected.imag)
        # The target itself remains |S|; only the reverse field is rescaled.
        self.assertEqual(als.fom_value_and_weight(s, "abs")[0], .5)

    def test_undefined_s_phase(self):
        for s in (0j, complex(np.nan, 1), complex(1, np.inf)):
            with self.subTest(s=s), self.assertRaises(ValueError):
                self.source(s, 0.)

    def test_unit_source(self):
        self.problem.adjoint_source = "unit"
        self.assertEqual(self.source(.3 + .4j, .125), 1.0 + 0j)
        self.assertEqual(self.source(0j, 0.), 1.0 + 0j)

    def test_grid_gradient_applies_coefficient_before_real_part(self):
        def bundle(e, h):
            return als.FieldBundle(x=np.array([-1., 1.]), y=np.array([-1., 1.]),
                E=np.broadcast_to(np.asarray(e)[:, None, None], (3, 2, 2)),
                H=np.broadcast_to(np.asarray(h)[:, None, None], (3, 2, 2)))
        fwd = als.ForwardResult(s=.6 + .8j,
            fields=bundle([1 + 2j, 17, 23], [99, 2 + 1j, 3 - 2j]),
            extra={"eval_id": 0, "output_power_w": .5})
        backward = bundle([3 - 1j, -19, -29], [-77, -1 + 4j, 2 + 3j])
        boundary = als.Boundary(np.zeros((1, 2)), np.array([[1., 0.]]),
                                np.ones(1), np.ones(1, bool))
        self.problem.boundary_sampling = "grid"
        self.problem.frequencies = ()
        self.problem.adjoint = lambda ls, result: backward
        self.problem.sample_offset = .05
        self.problem.eps, self.problem.mu, self.problem.omega = 2., 5., 3.
        self.problem.prefactor_scale = 2.
        actual = self.problem.boundary_gradient(None, fwd, boundary)
        np.testing.assert_allclose(actual, [60.])


if __name__ == "__main__":
    unittest.main()
