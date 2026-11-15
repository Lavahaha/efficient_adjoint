"""CAD identity, copy/export, and explicit local topology-repair contracts."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

import adjoint_levelset as als
from eaopt.config import BoxSpec, CaseConfig, PolygonSpec
from eaopt.geometry.design import compose_metal_regions
from eaopt.geometry.levelset import LevelSet2D
from eaopt.geometry.state import CadGeometryState
from eaopt.optimize.constraints import apply_min_feature, ConnectivityGuard
from eaopt.runner import CstAdjointLevelSetProblem, _project_case_geometry

ROOT = Path(__file__).resolve().parents[1]


class CadGeometryStateTests(unittest.TestCase):
    def test_polygon_feature_filter_preserves_corners_and_resolves_subgrid_features(self):
        rectangle = box(-1.037, -1.023, 1.037, 1.023)
        state = self.state(rectangle)
        self.assertTrue(state.filter_features(.4).geometry.equals_exact(rectangle, 0.))
        spur = box(1., -.03, 1.8, .03)
        filtered = self.state(rectangle.union(spur)).filter_features(.4)
        self.assertLess(filtered.geometry.symmetric_difference(rectangle).area, 1e-12)
        slit = box(-.03, 0., .03, 1.1)
        filled = self.state(rectangle.difference(slit)).filter_features(.4)
        self.assertLess(filled.geometry.symmetric_difference(rectangle).area, 1e-12)
        outside = box(-3.6, -.04, -2.5, .04)
        initial = rectangle.union(outside)
        filtered = self.state(initial).filter_features(.4)
        domain = box(-3,-3,3,3)
        self.assertTrue(filtered.geometry.difference(domain).equals(initial.difference(domain)))
        with self.assertRaisesRegex(ValueError, 'removed all metal'):
            self.state(box(-.01,-.01,.01,.01)).filter_features(.4)

    def test_runner_cad_projection_keeps_noop_geometry_and_applies_feature_edits(self):
        geometry = unary_union([box(-1.5, -1., 1.5, 1.), box(1.5, -.06, 2.5, .06)])
        cfg = self.cfg(geometry)
        cfg.constraints.min_feature_mm = 0.
        state = self.state(geometry)
        trial = als.LevelSet(self.grid(), state.phi, geometry_state=state)
        candidate = LevelSet2D(cfg.design_region.box, .1)
        pinned = np.zeros(trial.phi.shape, bool)
        mask = np.ones(trial.phi.shape)
        _project_case_geometry(trial, candidate, cfg, state.phi, pinned, None, None, None, mask, lambda s:None)
        self.assertTrue(trial.geometry_state.geometry.equals_exact(geometry, 0.))
        cfg.constraints.min_feature_mm = .4
        _project_case_geometry(trial, candidate, cfg, state.phi, pinned, None, None, None, mask, lambda s:None)
        trial.geometry_state.validate_phi(trial.phi)
        self.assertTrue(trial.geometry_state.geometry.is_valid)
        self.assertLess(trial.geometry_state.geometry.area, geometry.area)
        self.assertTrue(self.exported(cfg, trial.geometry_state).equals(trial.geometry_state.geometry))

    def test_runner_legacy_projection_matches_previous_sequence(self):
        from eaopt.optimize.constraints import apply_min_gap
        geometry = box(-1.037, -1.023, 1.037, 1.023)
        cfg = self.cfg(geometry)
        state = self.state(geometry)
        trial = als.LevelSet(self.grid(), state.phi)
        expected = trial.copy()
        candidate = LevelSet2D(cfg.design_region.box, .1)
        candidate.phi = trial.phi.copy()
        apply_min_gap(candidate, cfg)
        apply_min_feature(candidate, cfg.constraints.min_feature_mm)
        expected.phi = candidate.phi.copy()
        expected.reinit()
        pinned = np.zeros(trial.phi.shape, bool)
        _project_case_geometry(trial, candidate, cfg, state.phi, pinned, None, None, None,
                               np.ones(trial.phi.shape), lambda s:None)
        np.testing.assert_array_equal(trial.phi, expected.phi)

    def grid(self):
        return als.Grid2D(61, 61, .1, -3., -3.)

    def cfg(self, geometry):
        cfg = copy.deepcopy(CaseConfig.from_yaml(ROOT/'configs/antenna.yaml'))
        cfg.design_region.box = BoxSpec(x=(-3., 3.), y=(-3., 3.))
        cfg.design_region.grid_step_mm = .1
        parts = [geometry] if geometry.geom_type == 'Polygon' else geometry.geoms
        cfg.initial_metal = [PolygonSpec(vertices=np.asarray(p.exterior.coords).tolist()) for p in parts]
        return cfg

    def state(self, geometry):
        grid = self.grid()
        return CadGeometryState(geometry, grid.x, grid.y)

    def exported(self, cfg, state, phi=None):
        return unary_union([Polygon(r.outer, r.holes) for r in compose_metal_regions(
            cfg, state.phi if phi is None else phi, geometry_state=state)])

    def test_copy_redistance_and_serialization_retain_holes_and_subcell_features(self):
        main = Polygon([[-1.037, -1.021], [1.037, -1.021], [1.037, 1.021], [-1.037, 1.021]],
                       [[[-.4, -.3], [-.4, .3], [.4, .3], [.4, -.3]]])
        # This isolated feature lies strictly between grid nodes.
        original = unary_union([main, box(2.031, 2.031, 2.049, 2.049)])
        state = self.state(original)
        ls = als.LevelSet(self.grid(), state.phi, geometry_state=state)
        clone = ls.copy()
        self.assertIsNot(clone.geometry_state, state)
        self.assertFalse(np.shares_memory(clone.phi, ls.phi))
        for _ in range(5):
            clone.reinit()
            clone.geometry_state.validate_phi(clone.phi)
        restored = CadGeometryState.from_dict(json.loads(json.dumps(state.to_dict())))
        self.assertTrue(original.equals_exact(restored.geometry, 0.))
        np.testing.assert_array_equal(restored.phi, state.phi)
        detached = state.phi
        detached[:] = 5.
        np.testing.assert_array_equal(state.phi, ls.phi)
        self.assertAlmostEqual(ls.area(), original.area, places=12)
        with self.assertRaises(AttributeError):
            state.geometry = box(0, 0, 1, 1)

    def test_rejects_unsynchronized_material_and_cut_edge_edits(self):
        state = self.state(box(-1.037, -1.021, 1.037, 1.021))
        state.validate_phi(3*state.phi)
        for altered in (-state.phi, state.phi+.001, np.full(state.phi.shape, np.nan)):
            with self.assertRaises(ValueError):
                state.validate_phi(altered)
        ls = als.LevelSet(self.grid(), state.phi, geometry_state=state)
        ls.phi += .001
        with self.assertRaises(ValueError):
            ls.copy()
        with self.assertRaises(ValueError):
            ls.area()

    def test_export_uses_actual_cad_at_arbitrarily_small_positive_negative_moves(self):
        initial = box(-1, -1, 1, 1)
        cfg = self.cfg(initial)
        for h in (0., .001, -.001, 1e-6, -1e-6):
            actual = box(-1, -1, 1+h, 1)
            state = self.state(actual)
            exported = self.exported(cfg, state)
            self.assertLess(exported.boundary.hausdorff_distance(actual.boundary), 1e-12)
            self.assertAlmostEqual(exported.area-initial.area, 2*h, places=12)
        with patch('eaopt.geometry.design.contour_point_limit', return_value=4):
            with self.assertRaisesRegex(ValueError, 'CST'):
                self.exported(cfg, state)

    def test_frozen_outside_grid_and_shape_grid_validation(self):
        original = box(-3.5, -.4, 1.037, .4)
        state = self.state(original)
        cfg = self.cfg(original)
        self.assertTrue(self.exported(cfg, state).equals(original))
        moved = self.state(box(-3.6, -.4, 1.037, .4))
        with self.assertRaisesRegex(ValueError, 'frozen'):
            self.exported(cfg, moved)
        with self.assertRaises(ValueError):
            CadGeometryState(original, [0, 1, 3], [0, 1, 2])
        with self.assertRaises(ValueError):
            CadGeometryState(Polygon([[0, 0], [1, 1], [0, 1], [1, 0]]), [0, 1], [0, 1])

    def test_boolean_edits_support_holes_split_merge_and_empty(self):
        original = box(-1.037, -1.021, 1.037, 1.021)
        state = self.state(original)
        hole = box(-.3, -.3, .3, .3)
        drilled, delta = state.apply_delta(removed=hole)
        self.assertEqual(len(drilled.geometry.interiors), 1)
        self.assertAlmostEqual(delta['removed_area_mm2'], .36)
        restored, delta = drilled.apply_delta(added=hole)
        self.assertTrue(restored.geometry.equals(original))
        split, _ = state.apply_delta(removed=box(-.1, -2, .1, 2))
        self.assertEqual(len(split.geometry.geoms), 2)
        merged, _ = split.apply_delta(added=box(-.2, -.2, .2, .2))
        self.assertEqual(merged.geometry.geom_type, 'Polygon')
        empty, _ = state.apply_delta(removed=original)
        self.assertTrue(empty.geometry.is_empty)
        self.assertTrue(np.isfinite(empty.phi).all() and (empty.phi > 0).all())
        restored, _ = empty.apply_delta(added=original)
        self.assertTrue(restored.geometry.equals(original))

    def test_actual_min_feature_mask_repair_preserves_unrelated_corners(self):
        main = box(-1.037, -1.021, 1.037, 1.021)
        original = unary_union([main, box(2.05, 2.05, 2.15, 2.15)])
        state = self.state(original)
        ls = LevelSet2D(BoxSpec(x=(-3., 3.), y=(-3., 3.)), .1)
        ls.phi = state.phi
        before = ls.phi.copy()
        xx, yy = np.meshgrid(state.x, state.y, indexing='ij')
        force_main = (abs(xx) < 1.037) & (abs(yy) < 1.021)
        apply_min_feature(ls, .4, force_metal=force_main)
        added = (before > 0) & (ls.phi < 0)
        removed = (before < 0) & (ls.phi > 0)
        self.assertTrue(removed.any())
        projected, delta = state.apply_node_edits(added, removed)
        self.assertLess(projected.geometry.symmetric_difference(main).area, 1e-12)
        self.assertAlmostEqual(delta['removed_area_mm2'], original.area-main.area)
        np.testing.assert_array_equal(projected.phi < 0, ls.phi < 0)
        zero = np.zeros(before.shape, dtype=bool)
        unchanged, delta = projected.apply_node_edits(zero, zero)
        self.assertEqual(delta['added_area_mm2']+delta['removed_area_mm2'], 0.)
        self.assertTrue(unchanged.geometry.equals(projected.geometry))
        with self.assertRaises(ValueError):
            projected.apply_node_edits(removed, removed)

    def test_connectivity_mask_repair_produces_connected_cad(self):
        for shift in (0., .003):
            with self.subTest(shift=shift):
                original = unary_union([box(-3.5, -.4, -.5+shift, .4), box(.5+shift, -.4, 1.5, .4)])
                state = self.state(original)
                cfg = self.cfg(original)
                cfg.constraints.require_connected = True
                ls = LevelSet2D(cfg.design_region.box, .1)
                ls.phi = state.phi
                before = ls.phi.copy()
                guard = ConnectivityGuard(ls, cfg, log=lambda _: None)
                self.assertTrue(guard.apply(ls, log=lambda _: None))
                if shift:
                    # A changed material mask alone misses bridge endpoint
                    # nodes that were already metal, leaving a physical gap.
                    incomplete, _ = state.apply_node_edits((before >= 0) & (ls.phi < 0),
                                                           np.zeros(before.shape, bool))
                    self.assertEqual(incomplete.geometry.geom_type, 'MultiPolygon')
                # The actual bridge footprint includes those endpoint nodes.
                projected, delta = state.apply_node_edits(guard.frozen, np.zeros(before.shape, bool))
                self.assertEqual(projected.geometry.geom_type, 'Polygon')
                self.assertGreater(delta['added_area_mm2'], 0.)
                outside = box(-4, -3, -3, 3)
                self.assertTrue(projected.geometry.intersection(outside).equals(original.intersection(outside)))

    def test_runner_builds_retained_cad_and_saves_recoverable_state_without_cst(self):
        original = box(-1, -1, 1, 1)
        state = self.state(box(-1, -1, 1-1e-6, 1))
        ls = als.LevelSet(self.grid(), state.phi, geometry_state=state)
        with tempfile.TemporaryDirectory(dir=ROOT/'results') as directory:
            problem = object.__new__(CstAdjointLevelSetProblem)
            problem.cfg = self.cfg(original)
            problem.output_dir = Path(directory)
            problem._next_id = lambda: 0
            problem.log = lambda _: None
            problem._save_geometry_failure = Mock()
            solver = SimpleNamespace(M=SimpleNamespace(SUPPORTS_NESTED_CONTOURS=True),
                                     begin_iteration=Mock(), last_prepared_polys=None)
            def build(contours, _):
                solver.last_prepared_polys = contours
            solver.build_model = Mock(side_effect=build)
            solver.solve_forward = Mock(side_effect=RuntimeError('stop before any EM solve'))
            problem.solver = solver
            with self.assertRaisesRegex(RuntimeError, 'stop before'):
                problem.forward(ls)
            actual = Polygon(solver.last_prepared_polys[0])
            self.assertTrue(actual.equals(state.geometry))
            saved = list(Path(directory).rglob('geometry_state.json'))
            self.assertEqual(len(saved), 1)
            restored = CadGeometryState.from_dict(json.loads(saved[0].read_text(encoding='utf-8')))
            self.assertTrue(restored.geometry.equals(state.geometry))
        # A dirty phi must fail before model building; a correct CAD file alone
        # must not excuse an inconsistent solver state.
        with tempfile.TemporaryDirectory(dir=ROOT/'results') as directory:
            problem.output_dir = Path(directory)
            solver.begin_iteration.reset_mock()
            solver.build_model.reset_mock()
            ls.phi += .001
            with self.assertRaisesRegex(ValueError, 'Auxiliary phi'):
                problem.forward(ls)
            solver.begin_iteration.assert_not_called()
            solver.build_model.assert_not_called()

    def test_phi_only_optimizer_and_gradient_check_fail_before_any_solve(self):
        state = self.state(box(-1.037, -1.021, 1.037, 1.021))
        ls = als.LevelSet(self.grid(), state.phi, geometry_state=state)
        problem = SimpleNamespace(forward=Mock())
        optimizer = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape))
        with self.assertRaisesRegex(ValueError, 'geometry-aware'):
            optimizer.run(ls, n_iter=1)
        with self.assertRaisesRegex(ValueError, 'explicit CAD perturbations'):
            als.gradient_check(problem, ls)
        problem.forward.assert_not_called()


if __name__ == '__main__':
    unittest.main()
