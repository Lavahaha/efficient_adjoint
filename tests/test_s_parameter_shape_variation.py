"""Physical units, source phases and objective chain rules without a solver."""
import unittest
from types import SimpleNamespace

import numpy as np

import adjoint_levelset as als
from eaopt.runner import CstAdjointLevelSetProblem


class SParameterVariationTests(unittest.TestCase):
    def test_full_thickness_density_matches_exact_polynomial_integral(self):
        thickness = .035
        nodes, weights = np.polynomial.legendre.leggauss(2)
        z, wz = (nodes+1)*thickness/2, weights*thickness/2
        normals = np.eye(2)
        coeff = np.array([[1+2j, 3-1j], [2-1j, -2+3j],
                          [2+.5j, 1-2j], [-1+2j, .7+1j]])
        factors = np.array([1., 1.7])
        profiles = [(a+b*z)[None, :]*factors[:, None] for a, b in coeff]
        ef, hf, eb, hb = [np.zeros((3, 2, 2), complex) for _ in range(4)]
        ef[:2] = normals.T[:, :, None]*profiles[0]
        eb[:2] = normals.T[:, :, None]*profiles[2]
        hf[2], hb[2] = profiles[1], profiles[3]
        af, ab = 2*np.exp(.3j), .7*np.exp(-.2j)
        omega = 2*np.pi*5e9
        actual = als.s_parameter_sidewall_density(normals, wz, af*ef, af*hf, ab*eb, ab*hb,
                                                  omega, af, ab)
        def integral(u, v):
            a, b = u
            c, d = v
            return a*c*thickness+(a*d+b*c)*thickness**2/2+b*d*thickness**3/3
        expected = -1j*omega/4*1e-9*factors**2*(als.EPS0*integral(coeff[0], coeff[2])+
                                                        als.MU0*integral(coeff[1], coeff[3]))
        np.testing.assert_allclose(actual, expected, rtol=2e-13, atol=0)
        ds, displacement = np.array([.7, 1.1]), np.array([.002, -.003])
        area = (ds[:, None]*wz[None, :]*1e-6).ravel()
        surface = als.s_parameter_shape_variation(np.repeat(normals, 2, axis=0), area,
            np.repeat(displacement*1e-3, 2), *(v.reshape(3, -1) for v in (ef, hf, eb, hb)),
            omega, 1., 1.)
        np.testing.assert_allclose(np.sum(actual*ds*displacement), surface, rtol=2e-13, atol=0)

    def test_invalid_sidewall_quadrature_fails(self):
        fields = [np.ones((3, 2, 2), complex)]*4
        data = dict(normals_xy=np.eye(2), z_weights_mm=[.1, .1],
                    Ef=fields[0], Hf=fields[1], Eb=fields[2], Hb=fields[3], omega=1.,
                    incident_amplitude_fwd=1., incident_amplitude_adj=1.)
        for changes in (dict(z_weights_mm=[.1, -.1]), dict(z_weights_mm=[]),
                        dict(normals_xy=np.ones((2, 3))), dict(Ef=np.ones((3, 4))),
                        dict(eps=np.ones(2)), dict(incident_amplitude_adj=0)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                als.s_parameter_sidewall_density(**(data | changes))

    def setUp(self):
        self.normals = np.array([[1., 0., 0.], [0., 0., 1.]])
        self.fields = [np.array(a, complex).T for a in (
            [[1+2j, 3, 4], [2, 4-1j, 3]],
            [[2, 1-1j, 4+1j], [1j, 2, 3]],
            [[2-1j, 1, 2], [4, 1j, 1-2j]],
            [[3, 2+1j, 1j], [3, 1-1j, 2]])]
        self.area = np.array([2e-6, 3e-6])
        self.displacement = np.array([1e-5, -2e-5])

    def evaluate(self, **kw):
        data = dict(normals=self.normals, area_weights_m2=self.area,
                    normal_displacement_m=self.displacement,
                    Ef=self.fields[0], Hf=self.fields[1], Eb=self.fields[2], Hb=self.fields[3],
                    omega=2*np.pi*5e9, incident_amplitude_fwd=np.sqrt(.5),
                    incident_amplitude_adj=np.sqrt(.7))
        data.update(kw)
        return als.s_parameter_shape_variation(**data)

    def test_source_amplitude_phase_and_channel_exchange(self):
        reference = self.evaluate()
        a, b = 2*np.exp(.37j), .3*np.exp(-.81j)
        actual = self.evaluate(Ef=a*self.fields[0], Hf=a*self.fields[1],
                               Eb=b*self.fields[2], Hb=b*self.fields[3],
                               incident_amplitude_fwd=a*np.sqrt(.5), incident_amplitude_adj=b*np.sqrt(.7))
        np.testing.assert_allclose(actual, reference, rtol=1e-13)
        exchanged = self.evaluate(Ef=self.fields[2], Hf=self.fields[3], Eb=self.fields[0], Hb=self.fields[1],
                                  incident_amplitude_fwd=np.sqrt(.7), incident_amplitude_adj=np.sqrt(.5))
        np.testing.assert_allclose(exchanged, reference, rtol=1e-13)

    def test_rigid_rotation_and_displacement_linearity(self):
        rotation = np.array([[0., 1., 0.], [0., 0., 1.], [1., 0., 0.]])
        rotated = dict(zip(('Ef', 'Hf', 'Eb', 'Hb'), [rotation@f for f in self.fields]))
        np.testing.assert_allclose(self.evaluate(normals=self.normals@rotation.T, **rotated), self.evaluate(), rtol=1e-13)
        np.testing.assert_allclose(self.evaluate(normal_displacement_m=-3*self.displacement), -3*self.evaluate(), rtol=1e-13)
        self.assertEqual(self.evaluate(normal_displacement_m=np.zeros(2)), 0j)

    def test_units_and_material_arrays(self):
        np.testing.assert_allclose(self.evaluate(area_weights_m2=self.area*1e6,
                                                normal_displacement_m=self.displacement*1e3),
                                   self.evaluate()*1e9, rtol=1e-13)
        np.testing.assert_allclose(self.evaluate(eps=np.full(2, als.EPS0), mu=np.full(2, als.MU0)), self.evaluate())

    def test_invalid_physical_inputs_fail(self):
        for kw in (dict(normals=2*self.normals), dict(area_weights_m2=[1., -1.]),
                   dict(normal_displacement_m=[1.]), dict(incident_amplitude_adj=0j),
                   dict(incident_amplitude_fwd=complex(np.nan)), dict(omega=0.),
                   dict(eps=[1., 2., 3.]), dict(Ef=np.zeros((2, 2)))):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.evaluate(**kw)

    def test_all_objective_weights_against_independent_real_differences(self):
        s, ds, h = .3+.4j, -.7+.2j, 1e-6
        values = dict(real=lambda z:z.real, imag=lambda z:z.imag, abs=abs,
                      abs2=lambda z:abs(z)**2, db=lambda z:20*np.log10(abs(z)))
        for kind, value in values.items():
            with self.subTest(kind=kind):
                f, weight = als.fom_value_and_weight(s, kind)
                self.assertAlmostEqual(f, value(s))
                fd = (value(s+h*ds)-value(s-h*ds))/(2*h)
                np.testing.assert_allclose(als.real_gradient(ds, weight), fd, rtol=1e-9, atol=1e-10)

    def test_zero_and_nonfinite_objectives_are_explicit(self):
        self.assertEqual(als.fom_value_and_weight(0j, 'abs2'), (0., 0j))
        for kind in ('abs', 'db'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                als.fom_value_and_weight(0j, kind)
        for value in (complex(np.nan), complex(np.inf)):
            with self.assertRaises(ValueError):
                als.fom_value_and_weight(value)

    def test_runner_uses_the_selected_objective_weight_once(self):
        problem = object.__new__(CstAdjointLevelSetProblem)
        problem.adjoint_source = 'objective'
        problem.omega = 7.
        problem._write_eval = lambda row: None
        s = .3+.4j
        fwd = SimpleNamespace(s=s, extra={'eval_id':0, 'output_power_w':.125})
        expected = {'abs':s.conjugate()/abs(s), 'abs2':2*s.conjugate(),
                    'real':1., 'imag':-1j, 'db':20/np.log(10)*s.conjugate()/abs(s)**2}
        for kind, weight in expected.items():
            problem.fom_kind = kind
            with self.subTest(kind=kind):
                np.testing.assert_allclose(problem._source_amplitude(fwd), -7j*weight)


if __name__ == '__main__':
    unittest.main()
