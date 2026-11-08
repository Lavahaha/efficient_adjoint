"""Replay the saved native-CST point fields through each geometry stage."""
from pathlib import Path
import json
import sys
import numpy as np
from shapely.geometry import Polygon, LineString
from shapely.ops import unary_union, polygonize
from scipy.interpolate import RegularGridInterpolator
from skimage.measure import find_contours

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import adjoint_levelset as a
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import make_level_set,LevelSet2D
from eaopt.geometry.design import compose_metal_regions,connection_boundary_mask,design_box
from eaopt.optimize.constraints import apply_min_gap,apply_min_feature
from eaopt.runner import _movement_constraints
from eaopt.solver.case import load_case

root=Path('results/fd_single_2026-11-02/coupler_planar_smoke')
cfg=CaseConfig.from_dict(json.loads((root/'run_config.json').read_text(encoding='utf-8'))['config'])
setup,model=load_case(cfg)
old=make_level_set(cfg)
grid=a.Grid2D(old.nx,old.ny,old.dx,old.xs[0],old.ys[0])
ls=a.LevelSet(grid,old.phi)
mask,fm,fa,anchor=_movement_constraints(old,cfg)
pinned=connection_boundary_mask(old,cfg,model.fixed_metal_polygons())
mask[pinned]=0
d=np.load(root/'iter_000/boundary_fields.npz')
b=a.extract_boundary(ls,mask)
assert np.array_equal(b.pts,d['boundary'])
ix=d['boundary_indices']
selected=a.Boundary(b.pts[ix],b.normals[ix],b.weights[ix],b.active[ix])
g=np.zeros(len(b.pts))
g[ix]=np.real(a.shape_derivative_from_fields(selected,d['E_fwd'],d['H_fwd'],d['E_bwd'],d['H_bwd'],
    -2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w,float(d['eps_f_per_m']),a.MU0))
velocity=a.boundary_to_grid_velocity(ls,b,g,mask,10,smooth_sigma_cells=1.5)
fixed=[Polygon(p) for p in model.fixed_metal_polygons()]
left,right=fixed[1:]
rows=[]

def raw_geometry(phi):
    domain=design_box(cfg)
    curves=[LineString(np.column_stack([grid.x0+c[:,0]*grid.dx,grid.y0+c[:,1]*grid.dx]))
            for c in find_contours(phi,0)]
    interp=RegularGridInterpolator((grid.x,grid.y),phi)
    faces=[p for p in polygonize(unary_union([domain.boundary,*curves]))
           if float(interp([[p.representative_point().x,p.representative_point().y]])[0])<0]
    return unary_union(faces)

def metrics(geometry):
    full=unary_union([geometry,*fixed])
    parts=list(full.geoms) if full.geom_type=='MultiPolygon' else [full]
    return dict(coupled_ports_connected=any(p.intersects(left) and p.intersects(right) for p in parts),
        full_metal_components=len(parts),left_gap_mm=geometry.distance(left),right_gap_mm=geometry.distance(right),
        left_contact_length_mm=geometry.intersection(left).length,right_contact_length_mm=geometry.intersection(right).length,
        moving_bounds=list(geometry.bounds))

def stage(name,phi,**extra):
    regions=compose_metal_regions(cfg,phi)
    geometry=unary_union([Polygon(r.outer,r.holes) for r in regions])
    row=dict(stage=name,**metrics(geometry),**extra)
    rows.append(row)
    return row

stage('initial',ls.phi)
count=int(np.ceil(1e-8*np.max(abs(velocity))/(.5*grid.dx)))
step=1e-8/count
phi=ls.phi.copy()
for k in range(count):
    first=phi+step*a._hj_rhs(phi,velocity,grid.dx)
    second=first+step*a._hj_rhs(first,velocity,grid.dx)
    phi=.5*(phi+second)
    state=stage(f'hj_{k+1:03}',phi,time_fraction=(k+1)/count)
    state['unsimplified_ports_connected']=metrics(raw_geometry(phi))['coupled_ports_connected']
    if k==0 or not state['coupled_ports_connected']:
        print(json.dumps(state),flush=True)
    if not state['coupled_ports_connected']:
        # Continue to reconstruct the exact committed endpoint.
        phi=a.advect_levelset(phi,velocity,step*(count-k-1),grid.dx,.5)
        break
stage('hj_final',phi)
ls.phi=phi
a.apply_constraints(ls,fm,fa,anchor)
stage('first_reinit',ls.phi)
candidate=LevelSet2D(old.box,old.dx)
candidate.phi=ls.phi.copy()
candidate.phi[pinned]=old.phi[pinned]
stage('restore_contacts',candidate.phi)
apply_min_gap(candidate,cfg)
apply_min_feature(candidate,cfg.constraints.min_feature_mm,force_air=fa,force_metal=fm)
candidate.phi[pinned]=old.phi[pinned]
stage('gap_feature_projection',candidate.phi)
ls.phi=candidate.phi.copy()
ls.reinit()
ls.phi[pinned]=old.phi[pinned]
stage('second_reinit_restore_contacts',ls.phi)
saved=np.load(root/'phi_after_iter_000.npz')['phi']
assert np.max(abs(saved-ls.phi))<1e-9
stage('saved_final',saved)
rows.append(dict(stage='unsimplified_contours',**metrics(raw_geometry(saved))))
assert not rows[-1]['coupled_ports_connected']
result=dict(source=str(root),require_connected=cfg.constraints.require_connected,pinned_nodes=int(pinned.sum()),
    cfl_substeps=count,predicted_displacement_mm=float(1e-8*np.max(abs(velocity))),
    exact_replay_max_phi_difference=float(np.max(abs(saved-ls.phi))),stages=rows)
out=Path('results/fd_single_2026-11-02/coupler_disconnect_audit.json')
out.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k!='stages'},indent=2))
print(json.dumps([s for s in rows if not s['stage'].startswith('hj_') or not s['coupled_ports_connected']],indent=2))
