"""Inspect offsets shorter than the level-set/CAD boundary discrepancy."""
import unittest

from shapely.geometry import Point, box

from scripts.check_boundary_sample_failure import inspect_ray


class BoundaryDiagnosticTests(unittest.TestCase):
    def test_small_offset_can_remain_inside_movable_metal(self):
        # Reported point/normal; the rectangle demonstrates a 0.01 mm CAD mismatch.
        # This is a controlled geometry, not the unavailable failed server CAD.
        movable = box(-20., -10., 0., 10.)
        fixed = box(-25., -2.5, -20., 2.5)
        result = inspect_ray(movable.union(fixed), [-19.99, -2.54], [-1., 0.],
                             .001, ray_length=.1)
        self.assertFalse(fixed.covers(Point(-19.991, -2.54)))
        self.assertTrue(result["boundary_inside_cad"])
        self.assertAlmostEqual(result["signed_boundary_to_cad_mm"], -.01)
        self.assertAlmostEqual(result["ray_metal_intervals_mm"][0][0], 0.)
        self.assertAlmostEqual(result["ray_metal_intervals_mm"][0][1], .01)
        self.assertTrue(all(row["in_metal_xy"] for row in result["probes"]))

    def test_ray_must_include_sample_and_have_finite_length(self):
        for length in (.0005, float("nan"), float("inf")):
            with self.subTest(length=length):
                with self.assertRaisesRegex(ValueError, "Ray length"):
                    inspect_ray(box(-20., -10., 0., 10.), [-19.99, -2.54], [-1., 0.],
                                .001, ray_length=length)


if __name__ == "__main__":
    unittest.main()
