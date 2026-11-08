"""Frozen real-field replay controls: PDE resolution, extension and end geometry."""
from pathlib import Path
import contextlib
import io
import json
import runpy
import copy
import sys
import numpy as np
from scipy.interpolate import RegularGridInterpolator
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely import contains_xy,distance,points

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
with contextlib.redirect_stdout(io.StringIO()):
    s=runpy.run_path('tmp/audit_coupler_disconnect.py')
a,cfg,old,V,grid,b,g=[s[k] for k in ('a','cfg','old','velocity','grid','b','g')]
from eaopt.geometry.design import compose_metal_regions
from eaopt.geometry.levelset import LevelSet2D

def geometry(phi,dx):
    config=copy.deepcopy(cfg)
    config.design_region.grid_step_mm=dx
    return unary_union([Polygon(r.outer,r.holes) for r in compose_metal_regions(config,phi)])

def row(name,phi,dx):
    return dict(name=name,dx_mm=dx,**s['metrics'](geometry(phi,dx)))

rows=[]
for cfl in (.5,.25,.1):
    phi=a.advect_levelset(old.phi.copy(),V,1e-8,grid.dx,cfl)
    rows.append(row(f'original_cfl_{cfl:g}',phi,grid.dx))
original_rhs=a._hj_rhs
def godunov_max(phi,v,dx):
    p=np.pad(phi,1,mode='edge')
    xm=(p[1:-1,1:-1]-p[:-2,1:-1])/dx
    xp=(p[2:,1:-1]-p[1:-1,1:-1])/dx
    ym=(p[1:-1,1:-1]-p[1:-1,:-2])/dx
    yp=(p[1:-1,2:]-p[1:-1,1:-1])/dx
    plus=np.sqrt(np.maximum(np.maximum(xm,0)**2,np.minimum(xp,0)**2)+np.maximum(np.maximum(ym,0)**2,np.minimum(yp,0)**2))
    minus=np.sqrt(np.maximum(np.minimum(xm,0)**2,np.maximum(xp,0)**2)+np.maximum(np.minimum(ym,0)**2,np.maximum(yp,0)**2))
    return -(np.maximum(v,0)*plus+np.minimum(v,0)*minus)
try:
    for method in ('original','max_per_axis'):
        a._hj_rhs=original_rhs if method=='original' else godunov_max
        for dx in (.1,.05,.025):
            target=LevelSet2D(old.box,dx)
            xx,yy=np.meshgrid(target.xs,target.ys,indexing='ij')
            query=np.column_stack([xx.ravel(),yy.ravel()])
            query=np.clip(query,[grid.x[0],grid.y[0]],[grid.x[-1],grid.y[-1]])
            phi=RegularGridInterpolator((grid.x,grid.y),old.phi)(query).reshape(xx.shape)
            v=RegularGridInterpolator((grid.x,grid.y),V)(query).reshape(xx.shape)
            final=a.advect_levelset(phi,v,1e-8,dx,.25)
            rows.append(row(f'{method}_dx_{dx:g}',final,dx))
finally:
    a._hj_rhs=original_rhs
for nearest,sigma in ((1,0),(4,0),(1,1.5),(4,1.5)):
    v=a.boundary_to_grid_velocity(a.LevelSet(grid,old.phi),b,g,s['mask'],10,
        k_nearest=nearest,smooth_sigma_cells=sigma)
    phi=a.advect_levelset(old.phi.copy(),v,1e-8,grid.dx,.25)
    rows.append(dict(**row(f'k_{nearest}_sigma_{sigma}',phi,grid.dx),predicted_displacement_mm=float(1e-8*abs(v).max())))
# The actual CAD has no metal/air interface at the cut against a fixed leg.
actual=unary_union([Polygon(r.outer,r.holes) for r in compose_metal_regions(cfg,old.phi)]+s['fixed'])
xy=np.column_stack([grid.mesh()[0].ravel(),grid.mesh()[1].ravel()])
dist=distance(actual.boundary,points(xy))
physical_phi=np.where(contains_xy(actual,xy[:,0],xy[:,1])|(dist<1e-9),-dist,dist).reshape(old.phi.shape)
for label,initial,v in [('full_cad_sdf_same_velocity',physical_phi,V),
                       ('reverse_mode_sign_control',old.phi,-V)]:
    final=a.advect_levelset(initial.copy(),v,1e-8,grid.dx,.25)
    rows.append(row(label,final,grid.dx))
end_rows=[]
for x in (0.,.1,.2,11.8,11.9,12.):
    i=int(round(x/grid.dx)); j=int(round((-.8-grid.y0)/grid.dx))
    end_rows.append(dict(x_mm=x,y_mm=-.8,stored_phi_mm=float(old.phi[i,j]),full_cad_phi_mm=float(physical_phi[i,j])))
physical_normals=a.LevelSet(grid,physical_phi).normals_grid()
normals=np.column_stack([RegularGridInterpolator((grid.x,grid.y),n)(b.pts) for n in physical_normals])
normals/=np.linalg.norm(normals,axis=1)[:,None]+1e-30
angles=np.degrees(np.arccos(np.clip(np.sum(normals*b.normals,axis=1),-1,1)))
diagnostic=dict(physical_cad_end_sdf=end_rows,active_normal_angle_max_deg=float(angles[b.active].max()),
    active_normal_angle_gt_5_count=int(np.sum(angles[b.active]>5)),
    scalar_gradient_range=[float(g[b.active].min()),float(g[b.active].max())],
    reference_scope='Same frozen velocity, same outer dt; fine-grid interpolation is a PDE control, not a new EM solve',
    controls=rows)
out=Path('results/fd_single_2026-11-02/coupler_update_contract.json')
out.write_text(json.dumps(diagnostic,indent=2)+'\n',encoding='utf-8')
print(json.dumps(diagnostic,indent=2))
