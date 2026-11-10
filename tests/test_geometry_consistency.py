"""Sub-cell geometry invariants and quadrature on the forward CAD."""
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from skimage.measure import find_contours

import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.geometry.design import compose_metal_regions
from eaopt.geometry.contour import close_open_contours
from eaopt.geometry.levelset import make_level_set
from eaopt.geometry.levelset import LevelSet2D
from eaopt.config import BoxSpec
from eaopt.optimize.constraints import apply_min_feature
from eaopt.runner import CstAdjointLevelSetProblem
from eaopt.solver.cst_model import polygon_extrude

ROOT = Path(__file__).resolve().parents[1]


class GeometryConsistencyTests(unittest.TestCase):
    def irregular(self):
        grid = als.Grid2D(71, 71, .1, -3.5, -3.5)
        x, y = grid.mesh()
        phi = np.hypot(x + .037, y - .021) - (1.6 + .1 * np.sin(3 * x + y))
        return als.LevelSet(grid, phi * (1 + .2 * np.cos(x + 2 * y)))

    def assert_same_contours(self, before, after):
        first, second = find_contours(before, 0), find_contours(after, 0)
        self.assertEqual(len(first), len(second))
        for a, b in zip(first, second):
            np.testing.assert_allclose(a, b, rtol=0, atol=2e-12)

    def test_reinit_preserves_zeros_over_repeated_calls(self):
        ls = self.irregular()
        before = ls.phi.copy()
        for _ in range(10):
            ls.reinit()
            self.assert_same_contours(before, ls.phi)
        np.testing.assert_array_equal(np.sign(before), np.sign(ls.phi))

    def test_reinit_restores_distance_away_from_cut_cells(self):
        grid = als.Grid2D(81, 41, .1, -4., -2.)
        x, _ = grid.mesh()
        ls = als.LevelSet(grid, 5 * (x - .037))
        ls.reinit()
        self.assert_same_contours(x - .037, ls.phi)
        np.testing.assert_allclose(ls.phi, x - .037, rtol=0, atol=1e-12)

    def test_exact_zero_nodes_holes_and_edt_fallback(self):
        grid = als.Grid2D(51, 51, .1, -2.5, -2.5)
        x, y = grid.mesh()
        phi = np.maximum(np.hypot(x, y) - 1.5, .5 - np.hypot(x, y))
        for fallback in (False, True):
            ls = als.LevelSet(grid, phi)
            with self.subTest(fallback=fallback):
                if fallback:
                    with patch.dict('sys.modules', {'skfmm': None}):
                        ls.reinit()
                else:
                    ls.reinit()
                self.assert_same_contours(phi, ls.phi)
                np.testing.assert_array_equal(ls.phi[phi == 0], 0.)

    def test_antenna_zero_update_preserves_exported_cad(self):
        cfg = CaseConfig.from_yaml(ROOT / 'configs/antenna.yaml')
        initial = make_level_set(cfg)
        grid = als.Grid2D(initial.nx, initial.ny, initial.dx, initial.xs[0], initial.ys[0])
        for phi in (initial.phi, initial.phi - .037):
            with self.subTest(shifted=not np.array_equal(phi, initial.phi)):
                ls = als.LevelSet(grid, phi)
                shape = unary_union([Polygon(r.outer, r.holes) for r in compose_metal_regions(cfg, phi)])
                for _ in range(4):
                    als.apply_constraints(ls)
                updated = unary_union([Polygon(r.outer, r.holes) for r in compose_metal_regions(cfg, ls.phi)])
                self.assertLess(shape.boundary.hausdorff_distance(updated.boundary), 1e-10)

    def test_antenna_never_relaxes_geometry_to_meet_point_limit(self):
        cfg = CaseConfig.from_yaml(ROOT / 'configs/antenna.yaml')
        initial = make_level_set(cfg)
        x, y = np.meshgrid(initial.xs, initial.ys, indexing='ij')
        with patch('eaopt.geometry.design.contour_point_limit', return_value=8):
            with self.assertRaisesRegex(ValueError, 'CST'):
                compose_metal_regions(cfg, np.hypot(x, y) - 8.137)

    def test_cad_quadrature_normals_weights_and_mask_including_hole(self):
        outer = np.array([[-2., -2.], [-2., 2.], [2., 2.], [2., -2.], [-2., -2.]])
        hole = np.array([[-.5, -.5], [.5, -.5], [.5, .5], [-.5, .5], [-.5, -.5]])
        shape = Polygon(outer, [hole])
        problem = object.__new__(CstAdjointLevelSetProblem)
        ls = als.LevelSet(als.Grid2D(21, 21, .2, -2., -2.), np.ones((21, 21)))
        mask = ls.grid.mesh()[0] >= 0
        fwd = SimpleNamespace(extra={'cad_polygons': [outer, hole]})
        bnd = als.problem_boundary(problem, ls, fwd, mask)
        self.assertAlmostEqual(bnd.weights.sum(), shape.length)
        np.testing.assert_allclose(np.linalg.norm(bnd.normals, axis=1), 1.)
        self.assertTrue(bnd.active.any())
        self.assertFalse(bnd.active.all())
        for p, normal in zip(bnd.pts, bnd.normals):
            self.assertLess(shape.boundary.distance(Point(p)), 1e-12)
            self.assertFalse(shape.covers(Point(p + .01 * normal)))
            self.assertTrue(shape.contains(Point(p - .01 * normal)))

    def test_polygon_command_roundtrips_cad_coordinates(self):
        xy = np.array([[1.23456789012345, -19.87654321098765], [2., 0.], [0., 0.],
                       [1.23456789012345, -19.87654321098765]])
        command = polygon_extrude('test', 'design', 'PEC', xy, .018)
        parsed = np.array(re.findall(r'\.(?:Point|LineTo) "([^"]+)", "([^"]+)"', command), float)
        np.testing.assert_array_equal(parsed, xy)

    def test_local_feature_repair_preserves_remote_subcell_boundary(self):
        ls = LevelSet2D(BoxSpec(x=(-3., 3.), y=(-3., 3.)), .2)
        ls.init_from_polygons([np.array([[-1.037, -1.037], [1.037, -1.037],
                                       [1.037, 1.037], [-1.037, 1.037]])])
        ls.phi[3, 3] = -.01
        before = ls.phi.copy()
        apply_min_feature(ls, .6)
        self.assertGreater(ls.phi[3, 3], 0.)
        np.testing.assert_array_equal(ls.phi[10:21, 10:21], before[10:21, 10:21])

    def test_feature_closing_preserves_valid_metal_at_design_edge(self):
        ls = LevelSet2D(BoxSpec(x=(-3., 3.), y=(-3., 3.)), .2)
        ls.phi[:] = 1.
        ls.phi[:16, 8:23] = -.037
        before = ls.phi.copy()
        apply_min_feature(ls, .6)
        np.testing.assert_array_equal(ls.phi, before)

    def test_cst_contours_canonicalize_interface_roundoff(self):
        polygon = np.array([[-.037, 0.], [1.23456789012345, 0.],
                            [1.23456789012345, 1.237], [-.037, 1.237], [-.037, 0.]])
        perturbed = polygon + np.spacing(np.abs(polygon))
        box = BoxSpec(x=(-2., 2.), y=(-2., 2.))
        first = close_open_contours([polygon], box)[0]
        second = close_open_contours([perturbed], box)[0]
        np.testing.assert_array_equal(first, second)
        self.assertLessEqual(np.max(np.abs(first-polygon)), 5.1e-13)


if __name__ == '__main__':
    unittest.main()
