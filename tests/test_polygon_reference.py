"""Independent identities for the diagnostic Maxwell representation."""
import sys
import unittest
import tempfile
import json
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

import numpy as np
from scipy.special import hankel2, h2vp, gammaln
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from pec_polygon_reference import PolygonReference, PolygonSolution, SourceReference, boundary_quadrature, project_edge_density, als


class PolygonReferenceTests(unittest.TestCase):
    def test_shared_sidewall_interface_preserves_reference_channel_axes(self):
        rng = np.random.default_rng(381)
        electric, magnetic = [rng.normal(size=(5, 3, 2))+1j*rng.normal(size=(5, 3, 2)) for _ in range(2)]
        theta = np.arange(5)*.7
        normals = np.column_stack((np.cos(theta), np.sin(theta)))
        reference = SimpleNamespace(omega=2*np.pi*5e9, length=.01)
        sol = PolygonSolution(reference, None, None, None, 0., 0., 0., 0.)
        with patch.object(sol, 'fields', return_value=(electric, magnetic)):
            actual = sol.all_densities(np.zeros((5, 2)), normals)
        expected = np.empty((5, 2, 2), complex)
        for i in range(5):
            normal = np.r_[normals[i], 0.]
            for m in range(2):
                for n in range(2):
                    en = (electric[i, :, m]@normal)*(electric[i, :, n]@normal)
                    ht = magnetic[i, :, m]@magnetic[i, :, n]-(magnetic[i, :, m]@normal)*(magnetic[i, :, n]@normal)
                    expected[i, m, n] = -1j*reference.omega/4*reference.length*1e-6*(als.EPS0*en+als.MU0*ht)
        np.testing.assert_allclose(actual, expected, rtol=2e-13, atol=0)

    def test_long_run_retains_last_committed_state_when_forward_fails(self):
        import check_polygon_optimization as check
        from eaopt.geometry.state import CadGeometryState
        class FailingProblem:
            fom_kind = 'abs'
            def __init__(self, *args, **kwargs):
                self.solves = []
            def forward(self, ls):
                self.solves.append({'attempt': len(self.solves)})
                if len(self.solves) == 2:
                    raise ValueError('deliberate reference accuracy failure')
                return check.als.ForwardResult(.8+0j, None)
            def boundary_gradient(self, ls, fwd, boundary):
                return np.full(len(boundary.pts), .001)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            axis = np.arange(-5., 6.)
            state = CadGeometryState(Polygon([(-2,-2),(2,-2),(2,2),(-2,2)]), axis, axis)
            (folder/'S11_geometry.json').write_text(json.dumps(state.to_dict()), encoding='utf-8')
            with patch.object(check, 'OUT', folder), patch.object(check, 'MaxwellProblem', FailingProblem), patch('builtins.print'):
                report = check.continue_optimization(n_iter=3)
                self.assertEqual(report['status'], 'failed_check')
                self.assertEqual(len(report['states']), 1)
                self.assertEqual(len(report['solve_checks']), 2)
                self.assertFalse(report['convergence_verified'])
                saved = json.loads((folder/'long_S11/results.json').read_text())
                self.assertIn('deliberate', saved['error'])
                self.assertTrue((folder/'long_S11/geometry_000.json').exists())
                with self.assertRaises(ValueError):
                    check.continue_optimization(n_iter=3)

    def test_projection_preserves_work_for_arbitrary_vertex_displacements(self):
        from eaopt.geometry.evolution import CadBoundaryMesh
        polygon = Polygon([(0, 0), (2, 0), (3, 2), (0, 3)])
        mesh = CadBoundaryMesh.from_geometry(polygon, 10.)
        p, n, w, t = boundary_quadrature(polygon, 5, graded=True, with_parameter=True)
        rng = np.random.default_rng(714)
        density = rng.normal(size=(len(p), 2, 2))+1j*rng.normal(size=(len(p), 2, 2))
        edges = len(mesh.edges)
        projected = project_edge_density(density.reshape(edges, -1, 2, 2),
                                         w.reshape(edges, -1), t.reshape(edges, -1))
        _, _, coarse_w, _ = mesh.quadrature()
        for _ in range(12):
            velocity = rng.normal(size=mesh.vertices.shape)
            a = np.repeat(velocity[mesh.edges[:, 0]], len(p)//edges, axis=0)
            b = np.repeat(velocity[mesh.edges[:, 1]], len(p)//edges, axis=0)
            vn = np.sum(((1-t[:, None])*a+t[:, None]*b)*n, axis=1)
            fine_work = np.sum(density*(w*vn)[:, None, None], axis=0)
            coarse_work = np.sum(projected*(coarse_w*mesh.normal_velocity(velocity))[:, None, None], axis=0)
            np.testing.assert_allclose(coarse_work, fine_work, rtol=1e-12, atol=1e-13)
        with self.assertRaises(ValueError):
            project_edge_density(density.reshape(edges, -1, 2, 2), -w.reshape(edges, -1), t.reshape(edges, -1))

    def test_scaled_hankel_matches_direct_special_function(self):
        z = np.array([.8, 1., 1.3])
        value, derivative = PolygonReference.scaled_hankel(z, 40)
        n = np.arange(41)
        scale = np.ones((len(z), len(n)))
        scale[:, 2:] = np.exp(n[2:]*np.log(z[:, None]/2)-gammaln(n[2:]))
        np.testing.assert_allclose(value, hankel2(n, z[:, None])*scale, rtol=3e-13)
        np.testing.assert_allclose(derivative, h2vp(n, z[:, None])*scale, rtol=3e-13)

    def test_graf_port_projection_reconstructs_point_source_fields(self):
        theta = np.arange(16)*2*np.pi/16
        polygon = Polygon(10*np.column_stack((np.cos(theta), np.sin(theta))))
        theta = np.arange(51)*2*np.pi/51
        points = 30*np.column_stack((np.cos(theta), np.sin(theta)))
        for fraction, ratio, sigma in ((.6, .4, None), (.8, .5, None), (.8, .5, 4.)):
            ref = SourceReference(polygon, cluster_count=2, interior_fraction=fraction, cluster_ratio=ratio,
                                  taper_sigma=sigma)
            basis = PolygonReference.wave(ref, points, incoming=False)
            physical = ref.output_matrix()/ref.inverse_scale[:, None]
            direct = ref.wave(points, incoming=False)
            for modal, source in zip(basis, direct):
                np.testing.assert_allclose(modal@physical, source, rtol=2e-10, atol=2e-9)
        with self.assertRaises(ValueError):
            SourceReference(polygon, interior_fraction=1.)
        with self.assertRaises(ValueError):
            SourceReference(polygon, cluster_ratio=0.)
        with self.assertRaises(ValueError):
            SourceReference(polygon, taper_sigma=-1.)

    def test_graded_edge_rule_preserves_linear_vertex_moments(self):
        polygon = Polygon([(0, 0), (2, 0), (2, 3), (0, 3)])
        p, n, w, t = boundary_quadrature(polygon, 5, graded=True, with_parameter=True)
        length = np.array([2., 3., 2., 3.])
        np.testing.assert_allclose(w.reshape(4, -1).sum(axis=1), length)
        np.testing.assert_allclose((w*t).reshape(4, -1).sum(axis=1), length/2)
        np.testing.assert_allclose(np.sum(n*w[:, None], axis=0), 0, atol=1e-14)


if __name__ == '__main__':
    unittest.main()
