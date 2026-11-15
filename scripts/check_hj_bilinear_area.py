"""Separate straight contour error from the fixed-grid bilinear phi model."""
import json
import sys
from pathlib import Path
import numpy as np
from scipy.integrate import quad
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eaopt.config import CaseConfig, PolygonSpec, BoxSpec
from eaopt.geometry.levelset import make_level_set
from skimage.measure import find_contours
from shapely.geometry import LineString, box
from shapely.ops import polygonize
from scipy.interpolate import RegularGridInterpolator


def bilinear_area(phi, dx):
    cells = np.stack((phi[:-1,:-1],phi[1:,:-1],phi[:-1,1:],phi[1:,1:]),axis=-1).reshape(-1,4)
    full = (cells <= 0).all(axis=1) & (cells < 0).any(axis=1)
    cut = (cells < 0).any(axis=1) & (cells > 0).any(axis=1)
    area = float(full.sum())
    for a0,b0,a1,b1 in cells[cut]:
        breaks=[0.,1.]
        for start,end in ((a0,a1),(b0,b1)):
            if start*end < 0:
                breaks.append(float(start/(start-end)))
        def fraction(y):
            a,b = a0+(a1-a0)*y,b0+(b1-b0)*y
            if a <= 0 and b <= 0: return 1.
            if a >= 0 and b >= 0: return 0.
            root = a/(a-b)
            return root if a < 0 else 1-root
        breaks=sorted(set(breaks))
        area += sum(quad(fraction,a,b,epsabs=1e-12,epsrel=1e-11)[0] for a,b in zip(breaks[:-1],breaks[1:]))
    return area*dx*dx


def linear_contour_area(ls):
    domain = box(ls.xs[0],ls.ys[0],ls.xs[-1],ls.ys[-1])
    lines=[domain.boundary]
    for c in find_contours(ls.phi,0):
        lines.append(LineString(np.column_stack((ls.xs[0]+ls.dx*c[:,0],ls.ys[0]+ls.dx*c[:,1]))))
    field=RegularGridInterpolator((ls.xs,ls.ys),ls.phi)
    faces=[p for p in polygonize(unary_union(lines)) if float(field([[p.representative_point().x,p.representative_point().y]])[0])<0]
    return unary_union(faces).area


def triangle_fan_area(phi, dx):
    # Research comparator: four linear triangles per cell, using the mean
    # nodal value at the center. It is a different interpolant, not an HJ fix.
    corners=np.stack((phi[:-1,:-1],phi[1:,:-1],phi[1:,1:],phi[:-1,1:]),axis=-1)
    center=corners.mean(axis=-1)
    area=0.
    for i in range(4):
        v=np.sort(np.stack((corners[...,i],corners[...,(i+1)%4],center),axis=-1),axis=-1)
        a,b,c=v[...,0],v[...,1],v[...,2]
        fraction=np.zeros(a.shape)
        fraction[(c<=0)&(a<0)]=1.
        one=(a<0)&(b>=0)&(c>0)
        fraction[one]=a[one]**2/((b[one]-a[one])*(c[one]-a[one]))
        two=(b<0)&(c>0)
        fraction[two]=1-c[two]**2/((c[two]-a[two])*(c[two]-b[two]))
        area+=fraction.sum()*dx*dx/4
    return float(area)


def main():
    xx=np.broadcast_to(np.linspace(-1,1,3)[:,None],(3,3))
    for integrator in (bilinear_area,triangle_fan_area):
        np.testing.assert_allclose(integrator(xx-.13,1.),2*1.13,rtol=0,atol=1e-12)
        np.testing.assert_allclose(integrator(np.full((3,3),-1.),1.),4.,rtol=0,atol=1e-12)
        assert integrator(np.ones((3,3)),1.)==0.
    rows=[]
    for h in (0.,.01,-.01,.001,-.001,1e-5,-1e-5,1e-7,-1e-7):
        cfg=CaseConfig.from_yaml(ROOT/'configs/antenna.yaml')
        cfg.initial_metal=[PolygonSpec(vertices=[[-20,-10],[h,-10],[h,10],[-20,10]])]
        ls=make_level_set(cfg)
        exact=400+20*h
        linear=linear_contour_area(ls)
        bilinear=bilinear_area(ls.phi,ls.dx)
        fan=triangle_fan_area(ls.phi,ls.dx)
        if h==0:
            assert abs(bilinear-400)<1e-9
        rows.append(dict(h_mm=h,exact_area_mm2=exact,linear_contour_area_mm2=linear,
            bilinear_area_mm2=bilinear,linear_error_mm2=linear-exact,bilinear_error_mm2=bilinear-exact,
            bilinear_difference_quotient=None if not h else (bilinear-400)/h,
            triangle_fan_area_mm2=fan,triangle_fan_difference_quotient=None if not h else (fan-400)/h))
    out=ROOT/'results/hj_geometry_consistency_2026-11-15';out.mkdir(exist_ok=True)
    report=dict(scope='exact signed-distance samples of perturbed rectangles; no HJ advection or manufacturing projection',
                exact_length_area_derivative_mm=20,rows=rows)
    refinement=[]
    # Smaller diagnostic domain keeps this resolution check inexpensive while
    # retaining the same rectangle and its left-domain boundary contact.
    for dx in (.2,.1,.05,.025):
        for h in (1e-5,-1e-5):
            cfg=CaseConfig.from_yaml(ROOT/'configs/antenna.yaml')
            cfg.design_region.box=BoxSpec(x=(-20,1),y=(-11,11))
            cfg.design_region.grid_step_mm=dx
            cfg.initial_metal=[PolygonSpec(vertices=[[-20,-10],[h,-10],[h,10],[-20,10]])]
            ls=make_level_set(cfg)
            quotient=(triangle_fan_area(ls.phi,dx)-400)/h
            refinement.append(dict(dx_mm=dx,h_mm=h,triangle_fan_area_quotient_mm=quotient,
                                   relative_derivative_error=abs(quotient-20)/20))
    report['triangle_fan_refinement']=refinement
    (out/'bilinear_area.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
