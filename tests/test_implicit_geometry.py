"""Independent area, topology and edge-sharing checks for implicit extraction."""
import unittest
import numpy as np
from shapely.geometry import box, Point

from eaopt.geometry.implicit import reconstruct_triangular_levelset
from scripts.check_hj_bilinear_area import triangle_fan_area
import adjoint_levelset as als


class ImplicitGeometryTests(unittest.TestCase):
    def test_affine_interface_and_uniform_fields(self):
        x,y=np.linspace(-2,2,21),np.linspace(-1,1,11)
        xx,yy=np.meshgrid(x,y,indexing='ij')
        g=reconstruct_triangular_levelset(x,y,xx-.037)
        self.assertLess(g.symmetric_difference(box(-2,-1,.037,1)).area,1e-12)
        self.assertTrue(reconstruct_triangular_levelset(x,y,np.ones(xx.shape)).is_empty)
        self.assertTrue(reconstruct_triangular_levelset(x,y,np.zeros(xx.shape)).is_empty)
        self.assertTrue(reconstruct_triangular_levelset(x,y,-np.ones(xx.shape)).equals(box(-2,-1,2,1)))

    def test_area_matches_independent_clipped_triangle_formula(self):
        x=np.linspace(-1,1,9)
        rng=np.random.default_rng(812)
        for _ in range(8):
            phi=rng.normal(size=(9,9))
            phi[rng.uniform(size=phi.shape)<.1]=0.
            g=reconstruct_triangular_levelset(x,x,phi)
            self.assertAlmostEqual(g.area,triangle_fan_area(phi,.25),places=12)
            self.assertTrue(g.is_valid)
            self.assertTrue(g.equals(reconstruct_triangular_levelset(x,x,phi)))

    def test_hole_and_disconnected_parts(self):
        x=np.linspace(-3,3,61)
        xx,yy=np.meshgrid(x,x,indexing='ij')
        annulus=np.maximum(np.hypot(xx,yy)-1.7,.6-np.hypot(xx,yy))
        g=reconstruct_triangular_levelset(x,x,annulus)
        self.assertEqual(g.geom_type,'Polygon')
        self.assertEqual(len(g.interiors),1)
        self.assertFalse(g.covers(Point(0,0)))
        separate=np.minimum(np.hypot(xx-1.5,yy)-.6,np.hypot(xx+1.5,yy)-.6)
        g=reconstruct_triangular_levelset(x,x,separate)
        self.assertEqual(g.geom_type,'MultiPolygon')
        self.assertEqual(len(g.geoms),2)

    def test_invalid_input_fails(self):
        with self.assertRaises(ValueError): reconstruct_triangular_levelset([0,0],[0,1],np.zeros((2,2)))
        with self.assertRaises(ValueError): reconstruct_triangular_levelset([0,1],[0,1],[[0,1],[1,np.nan]])

    def test_matching_reinitialization_preserves_all_fan_crossings(self):
        grid=als.Grid2D(61,61,.1,-3.,-3.)
        x,y=grid.mesh()
        phi=(np.hypot(x+.037,y-.021)-1.6)*(1+.2*np.cos(x+2*y))
        before=reconstruct_triangular_levelset(grid.x,grid.y,phi)
        ls=als.LevelSet(grid,phi)
        for _ in range(4):
            ls.reinit(preserve_cell_fans=True)
            after=reconstruct_triangular_levelset(grid.x,grid.y,ls.phi)
            self.assertLess(before.symmetric_difference(after).area,1e-12)
            self.assertLess(before.boundary.hausdorff_distance(after.boundary),1e-12)
        np.testing.assert_array_equal(np.sign(phi),np.sign(ls.phi))

    def test_hj_evolution_can_merge_components_without_tracking_old_edges(self):
        grid=als.Grid2D(81,61,.05,-2.,-1.5)
        x,y=grid.mesh()
        phi=np.minimum(np.hypot(x-.8,y)-.5,np.hypot(x+.8,y)-.5)
        first=reconstruct_triangular_levelset(grid.x,grid.y,phi)
        self.assertEqual(first.geom_type,'MultiPolygon')
        evolved=als.advect_levelset(phi,np.ones_like(phi),.4,grid.dx,cfl=.4)
        final=reconstruct_triangular_levelset(grid.x,grid.y,evolved)
        self.assertEqual(final.geom_type,'Polygon')
        self.assertTrue(final.contains(Point(0.,0.)))
        self.assertTrue(final.is_valid)


if __name__=='__main__': unittest.main()
