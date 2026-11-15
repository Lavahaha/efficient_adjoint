"""Discrete direction, continuous collision checks, and the actual optimizer loop."""
import unittest

import numpy as np
from shapely.affinity import translate
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

import adjoint_levelset as als
from eaopt.geometry.evolution import CadBoundaryMesh, CadEvolution
from eaopt.geometry.state import CadGeometryState


class AreaProblem:
    fom_kind = 'real'

    def __init__(self, sign=1., density=1.):
        self.sign, self.density, self.evaluations = sign, density, []

    def forward(self, ls):
        ls.geometry_state.validate_phi(ls.phi)
        geometry = ls.geometry_state.geometry
        self.evaluations.append(geometry)
        return als.ForwardResult(self.sign*geometry.area, None,
                                 {'eval_id': len(self.evaluations)-1})

    def boundary_gradient(self, ls, fwd, boundary):
        return np.full(len(boundary.pts), self.sign*self.density)


class CadEvolutionTests(unittest.TestCase):
    def test_domain_boundaries_block_outward_but_allow_inward_motion(self):
        for smoothing in (0., 2.):
            ls = self.state(box(-3., -1., 1., 1.))
            cfg = als.OptimizerConfig(pseudo_time_step=.01, smooth_sigma_cells=smoothing)
            for sign in (1., -1.):
                p = AreaProblem(sign=sign)
                step = CadEvolution().prepare(ls, p, p.forward(ls), np.ones(ls.phi.shape), cfg)
                left = step.mesh.vertices[:, 0] == -3.
                self.assertTrue((step.velocity[left, 0] >= 0).all())
                if sign < 0:
                    self.assertGreater(float(step.velocity[left, 0].max()), 0.)
                self.assertGreater(step.signed_work, 0.)
                moved = step.advance(cfg.pseudo_time_step).geometry_state.geometry
                self.assertGreaterEqual(moved.bounds[0], -3.)

    def test_bounded_metric_preserves_work_and_unconstrained_results(self):
        mesh = CadBoundaryMesh.from_geometry(box(-1, -1, 1, 1), .2)
        _, _, weights, _ = mesh.quadrature()
        rng = np.random.default_rng(904)
        for smoothing in (0., .3):
            for _ in range(5):
                density = rng.normal(size=len(weights))
                mobility = rng.uniform(.1, 1., len(mesh.vertices))
                mobility[:2] = 0.
                original = mesh.gradient_velocity(density, mobility, smoothing_length=smoothing)
                wide = np.full_like(original, 100.)
                unchanged = mesh.gradient_velocity(density, mobility, smoothing_length=smoothing,
                                                   velocity_bounds=(-wide, wide))
                np.testing.assert_array_equal(original, unchanged)
                lower = -rng.uniform(0., .02, original.shape)
                upper = rng.uniform(0., .02, original.shape)
                lower[::3] = 0.
                bounded = mesh.gradient_velocity(density, mobility, smoothing_length=smoothing,
                                                 velocity_bounds=(lower, upper))
                self.assertTrue(((bounded >= lower) & (bounded <= upper)).all())
                self.assertGreaterEqual(float(np.sum(weights*density*mesh.normal_velocity(bounded))), 0.)
                np.testing.assert_array_equal(bounded[:2], 0.)

    def state(self, geometry=None):
        geometry = box(-1.037, -1.021, 1.037, 1.021) if geometry is None else geometry
        grid = als.Grid2D(61, 61, .1, -3., -3.)
        state = CadGeometryState(geometry, grid.x, grid.y)
        return als.LevelSet(grid, state.phi, geometry_state=state)

    def test_smoothed_operator_is_symmetric_positive_with_fixed_vertices(self):
        mesh = CadBoundaryMesh.from_geometry(Polygon([[-1, -1], [1, -1], [1, 1], [-.8, .7]]), .19)
        _, _, weights, _ = mesh.quadrature()
        rng = np.random.default_rng(71)
        mobility = rng.uniform(size=len(mesh.vertices))
        mobility[:3] = 0.
        for smoothing in (0., .15, .6):
            for _ in range(4):
                a, b = rng.normal(size=(2, len(weights)))
                ua = mesh.gradient_velocity(a, mobility, smoothing_length=smoothing)
                ub = mesh.gradient_velocity(b, mobility, smoothing_length=smoothing)
                va, vb = mesh.normal_velocity(ua), mesh.normal_velocity(ub)
                self.assertGreaterEqual(float(np.sum(weights*a*va)), -1e-12)
                self.assertAlmostEqual(float(np.sum(weights*a*vb)), float(np.sum(weights*b*va)), places=12)
                np.testing.assert_array_equal(ua[:3], 0.)

    def test_swept_contact_is_detected_when_endpoints_are_valid(self):
        original = unary_union([box(-2, -.5, -1, .5), box(1, -.5, 2, .5)])
        mesh = CadBoundaryMesh.from_geometry(original, 2.)
        velocity = np.zeros_like(mesh.vertices)
        velocity[mesh.vertices[:, 0] < 0, 0] = 5.
        with self.assertRaisesRegex(ValueError, 'topology event'):
            mesh.moved(velocity, 1.)
        # A common translation has overlapping swept bounding boxes but no
        # relative contact; the collision test must not reject it.
        velocity[:] = [5., .3]
        moved = mesh.moved(velocity, 1.)
        self.assertLess(moved.symmetric_difference(translate(original, xoff=5, yoff=.3)).area, 1e-12)

    def test_collapse_and_reexpansion_is_rejected(self):
        mesh = CadBoundaryMesh.from_geometry(box(-1, -1, 1, 1), 3.)
        with self.assertRaisesRegex(ValueError, 'collapses'):
            mesh.moved(-3*mesh.vertices, 1.)

    def test_shared_loop_maximize_minimize_and_fixed_step(self):
        outputs = []
        for maximize, sign in ((True, 1.), (False, -1.)):
            problem = AreaProblem(sign)
            ls = self.state()
            cfg = als.OptimizerConfig(maximize=maximize, pseudo_time_step=.01)
            opt = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape), cfg,
                                               geometry_evolution=CadEvolution())
            final = opt.run(ls, n_iter=3)
            self.assertEqual(len(problem.evaluations), 4)
            self.assertTrue(all(r['objective_gain'] > 0 for r in opt.history))
            self.assertTrue(all(r['pseudo_time_step_used'] == .01 and r['backtracks'] == 0 for r in opt.history))
            self.assertTrue(all(r['linearization_matches_final_geometry'] for r in opt.history))
            outputs.append(final.geometry_state.geometry)
        self.assertTrue(outputs[0].equals_exact(outputs[1], 0.))

    def test_density_scale_changes_displacement_without_step_normalization(self):
        ls = self.state()
        cfg = als.OptimizerConfig(pseudo_time_step=.01)
        evolution = CadEvolution()
        steps = []
        for scale in (1., 2.):
            p = AreaProblem(density=scale)
            steps.append(evolution.prepare(ls, p, p.forward(ls), np.ones(ls.phi.shape), cfg))
        np.testing.assert_allclose(steps[1].velocity, 2*steps[0].velocity, rtol=1e-13, atol=1e-14)
        self.assertAlmostEqual(steps[1].vmax, 2*steps[0].vmax, places=12)

    def test_inactive_edges_cannot_move_from_adjacent_active_gradients(self):
        ls = self.state(box(-1, -1, 1, 1))
        xx, _ = ls.grid.mesh()
        mask = (xx > -1+1e-10).astype(float)
        p = AreaProblem()
        step = CadEvolution().prepare(ls, p, p.forward(ls), mask, als.OptimizerConfig())
        fixed = abs(step.mesh.vertices[:, 0]+1) < 1e-12
        np.testing.assert_array_equal(step.velocity[fixed], 0.)

    def test_projection_changes_are_recorded_without_claiming_the_old_prediction(self):
        ls, problem = self.state(), AreaProblem()
        def project(trial):
            state, _ = trial.geometry_state.apply_delta(removed=box(-.5, -.5, .5, .5))
            trial.geometry_state, trial.phi = state, state.phi
        cfg = als.OptimizerConfig(pseudo_time_step=.01)
        opt = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape), cfg,
            geometry_evolution=CadEvolution(), geometry_projector=project)
        final = opt.run(ls, n_iter=1)
        row = opt.history[0]
        self.assertGreater(row['linearized_signed_work'], 0.)
        self.assertLess(row['delta_f'], 0.)
        self.assertFalse(row['linearization_matches_final_geometry'])
        self.assertAlmostEqual(row['projection_removed_area'], 1.)
        self.assertEqual(len(final.geometry_state.geometry.interiors), 1)
        self.assertTrue(row['updated'])

    def test_unsynchronized_projector_stops_before_trial_forward(self):
        ls, problem = self.state(), AreaProblem()
        def project(trial):
            trial.phi += .001
        cfg = als.OptimizerConfig(pseudo_time_step=.01)
        opt = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape), cfg,
            geometry_evolution=CadEvolution(), geometry_projector=project)
        with self.assertRaisesRegex(ValueError, 'Auxiliary phi'):
            opt.run(ls, n_iter=1)
        self.assertEqual(len(problem.evaluations), 1)

    def test_empty_or_zero_gradient_stops_without_a_trial(self):
        for geometry in (Polygon(), box(-1, -1, 1, 1)):
            ls, problem = self.state(geometry), AreaProblem(density=0.)
            opt = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape),
                                               geometry_evolution=CadEvolution())
            opt.run(ls, n_iter=3)
            self.assertEqual(opt.stop_reason, 'converged_zero_velocity')
            self.assertEqual(len(problem.evaluations), 1)

    def test_repeated_zero_transport_does_not_double_vertex_counts(self):
        geometry = box(-10, -10, 10, 10)
        sizes = []
        for _ in range(4):
            mesh = CadBoundaryMesh.from_geometry(geometry, .4)
            sizes.append(len(mesh.vertices))
            geometry = mesh.moved(np.zeros_like(mesh.vertices), 0.)
        self.assertEqual(sizes, [200]*4)

    def test_mask_and_anchor_projections_are_explicit_and_keep_frozen_outside(self):
        ls = self.state()
        force_air, force_metal = np.zeros(ls.phi.shape, bool), np.zeros(ls.phi.shape, bool)
        force_air[30, 30], force_metal[50, 50] = True, True
        evolution = CadEvolution()
        evolution.apply_constraints(ls, force_metal, force_air)
        self.assertGreater(ls.phi[30, 30], 0.)
        self.assertLess(ls.phi[50, 50], 0.)
        ls.geometry_state.validate_phi(ls.phi)
        original = unary_union([box(-1, -1, 0, 0), box(2.5, -.5, 3.5, .5)])
        ls = self.state(original)
        anchor = np.zeros(ls.phi.shape, bool)
        anchor[25, 25] = True
        evolution.apply_constraints(ls, anchor=anchor)
        expected = unary_union([box(-1, -1, 0, 0), box(3, -.5, 3.5, .5)])
        self.assertTrue(ls.geometry_state.geometry.equals(expected))

    def test_legacy_hj_fixed_total_time_and_acceptance_are_unchanged(self):
        grid = als.Grid2D(41, 11, .1, -2., -.5)
        xx, _ = grid.mesh()
        ls = als.LevelSet(grid, xx-.037)
        def interface(candidate):
            row = candidate.phi[:, 5]
            i = np.flatnonzero((row[:-1] < 0) & (row[1:] > 0))[0]
            return grid.x[i]-grid.dx*row[i]/(row[i+1]-row[i])
        class Problem:
            fom_kind = 'real'
            def forward(self, candidate):
                return als.ForwardResult(-interface(candidate), None, {})
        cfg = als.OptimizerConfig(pseudo_time_step=1e-8, cfl=.4)
        opt = als.AdjointLevelSetOptimizer(Problem(), np.ones(ls.phi.shape), cfg)
        opt.velocity = lambda candidate, fwd: (np.full(ls.phi.shape, 2e7), fwd.s, None, None)
        final = opt.run(ls, n_iter=3)
        self.assertAlmostEqual(interface(final), .037+3*.2, places=11)
        self.assertTrue(all(r['updated'] and r['delta_f'] < 0 for r in opt.history))
        self.assertTrue(all(r['pseudo_time_step_used'] == 1e-8 and r['backtracks'] == 0 for r in opt.history))
        self.assertTrue(all('geometry_update' not in r for r in opt.history))


if __name__ == '__main__':
    unittest.main()
