"""Waveguide port placement, including internal coaxial ports."""
import re
import unittest

from eaopt.solver.cst_model import waveguide_port
from eaopt.solver import cst_model_antenna as antenna


class WaveguidePortTests(unittest.TestCase):
    def test_ranges_follow_the_port_normal_on_all_six_faces(self):
        for face in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax"):
            with self.subTest(face=face):
                command = waveguide_port(1, "feed", face, -7, -3, 4, -2, 5)
                ranges = {axis.lower(): (float(a), float(b)) for axis, a, b in
                          re.findall(r'\.([XYZ])range "([^"]+)", "([^"]+)"', command)}
                self.assertEqual(ranges[face[0]], (-7, -7))
                self.assertEqual(ranges["y" if face[0] == "x" else "x"], (-3, 4))
                self.assertEqual(ranges["y" if face[0] == "z" else "z"], (-2, 5))
                self.assertIn('.PortOnBound "True"', command)

    def test_coax_port_stays_on_feed_end_with_open_boundaries(self):
        command = antenna._coax_port()
        self.assertIn('.Orientation "zmin"', command)
        self.assertIn('.PortOnBound "False"', command)
        self.assertIn(f'.Zrange "{antenna.COAX_PORT_Z:g}", "{antenna.COAX_PORT_Z:g}"', command)
        self.assertLess(antenna.COAX_PORT_Z, antenna.GROUND_Z)
        self.assertIn('.NumberOfModes "1"', command)
        self.assertNotIn('DiscretePort', ''.join(antenna.model_blocks()))

    def test_invalid_face_is_rejected(self):
        with self.assertRaises(ValueError):
            waveguide_port(1, "feed", "positive", 0, -1, 1, -1, 1)


if __name__ == "__main__":
    unittest.main()
