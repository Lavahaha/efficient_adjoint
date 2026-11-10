"""Preserve sub-cell motion while enforcing discrete feature constraints."""
import unittest

import numpy as np

from eaopt.config import BoxSpec
from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import apply_min_feature


class MinFeatureTests(unittest.TestCase):
    def setUp(self):
        self.ls = LevelSet2D(BoxSpec(x=(-3., 3.), y=(-3., 3.)), .2)
        self.ls.init_from_polygons([np.array([
            [-1.04, -1.04], [1.04, -1.04], [1.04, 1.04], [-1.04, 1.04]])])

    def test_valid_shape_preserves_subcell_phi(self):
        self.ls.phi -= .01
        incoming = self.ls.phi.copy()
        apply_min_feature(self.ls, .6)
        np.testing.assert_array_equal(self.ls.phi, incoming)

    def test_small_updates_accumulate_across_a_grid_node(self):
        initial = self.ls.phi.copy()
        # Each 0.01 mm update is much smaller than the 0.2 mm grid spacing.
        for _ in range(20):
            self.ls.phi -= .01
            apply_min_feature(self.ls, .6)
        np.testing.assert_allclose(self.ls.phi, initial - .2, rtol=0, atol=1e-14)
        self.assertTrue(np.any((self.ls.phi < 0) != (initial < 0)))

    def test_actual_feature_and_forced_mask_repairs_still_apply(self):
        # An isolated metal pixel must still be removed.
        self.ls.phi[3, 3] = -.01
        force_air = np.zeros_like(self.ls.phi, dtype=bool)
        force_metal = np.zeros_like(force_air)
        force_air[15, 15] = True
        force_metal[26, 26] = True
        apply_min_feature(self.ls, .6, force_air=force_air, force_metal=force_metal)
        self.assertGreater(self.ls.phi[3, 3], 0)
        self.assertGreater(self.ls.phi[15, 15], 0)
        self.assertLess(self.ls.phi[26, 26], 0)

    def test_removing_all_metal_still_fails(self):
        self.ls.phi[:] = 1.
        self.ls.phi[15, 15] = -.01
        with self.assertRaisesRegex(ValueError, "min_feature_mm"):
            apply_min_feature(self.ls, .6)


if __name__ == "__main__":
    unittest.main()
