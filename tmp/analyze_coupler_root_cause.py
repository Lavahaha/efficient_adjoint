"""Analyze real native reference controls and replay their fixed-dt updates."""
from pathlib import Path
import contextlib,io,json,runpy,sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
with contextlib.redirect_stdout(io.StringIO()):
    s=runpy.run_path('tmp/audit_coupler_disconnect.py')
a,cfg,grid,old,b=[s[k] for k in ('a','cfg','grid','old','b')]
from eaopt.geometry.levelset import LevelSet2D
from eaopt.geometry.design import compose_metal_regions,region_rings
from eaopt.optimize.constraints import apply_min_gap,apply_min_feature
from shapely.geometry import Polygon
from shapely.ops import unary_union

root=Path('results/fd_single_2026-11-02')
completed=[p for p in (root/'coupler_port_reference_control').rglob('execution.json')
           if json.loads(p.read_text(encoding='utf-8')).get('status')=='complete']
if not completed:raise RuntimeError('Native reference-control job has not completed')
path=sorted(completed)[-1];control=path.parent
run=json.loads(path.read_text(encoding='utf-8'))
rows={r['name']:r for r in run['runs']}
assert len(rows)==4 and all(r['pin_w']==.5 for r in rows.values())
assert all(rows[label+'_fwd']['mesh_sha256']==rows[label+'_bwd']['mesh_sha256']
           for label in ('forward','reverse'))
fields={k:np.load(control/k/'fields.npz') for k in rows}
ix=s['d']['boundary_indices']
boundary=a.Boundary(b.pts[ix],b.normals[ix],b.weights[ix],b.active[ix])
prefactor=-2j*2*np.pi*5e9/.5
gradients={}
metrics=[]
for label in ('forward','reverse'):
    ef,hf=[fields[label+'_fwd'][k] for k in ('E','H')]
    eb,hb=[fields[label+'_bwd'][k] for k in ('E','H')]
    g=np.zeros(len(b.pts))
    g[ix]=np.real(a.shape_derivative_from_fields(boundary,ef,hf,eb,hb,prefactor,a.EPS0,a.MU0))
    gradients[label]=g
    velocity=a.boundary_to_grid_velocity(a.LevelSet(grid,old.phi),b,g,s['mask'],10,smooth_sigma_cells=1.5)
    ls=a.LevelSet(grid,a.advect_levelset(old.phi.copy(),velocity,1e-8,grid.dx,.5))
    a.apply_constraints(ls,s['fm'],s['fa'],s['anchor'])
    candidate=LevelSet2D(old.box,old.dx);candidate.phi=ls.phi.copy()
    candidate.phi[s['pinned']]=old.phi[s['pinned']]
    apply_min_gap(candidate,cfg)
    apply_min_feature(candidate,cfg.constraints.min_feature_mm,force_air=s['fa'],force_metal=s['fm'])
    candidate.phi[s['pinned']]=old.phi[s['pinned']]
    ls.phi=candidate.phi.copy();ls.reinit();ls.phi[s['pinned']]=old.phi[s['pinned']]
    regions=compose_metal_regions(cfg,ls.phi)
    geometry=unary_union([Polygon(r.outer,r.holes) for r in regions])
    folder=control/(label+'_update');folder.mkdir(exist_ok=True)
    np.savez_compressed(folder/'state.npz',phi=ls.phi,V=velocity,g=g,x=grid.x,y=grid.y)
    (folder/'shape.json').write_text(json.dumps({'unit':'mm','polygons':[p.tolist() for p in region_rings(regions)]},indent=2),encoding='utf-8')
    metrics.append(dict(label=label,predicted_displacement_mm=float(1e-8*abs(velocity).max()),**s['metrics'](geometry)))
    metrics[-1]['through_line_gap_mm']=geometry.distance(s['fixed'][0])

fits={}
for tag in ('fwd','bwd'):
    for kind in ('E','H'):
        u,v=[fields[label+'_'+tag][kind] for label in ('forward','reverse')]
        alpha=np.vdot(u,v)/np.vdot(u,u)
        fits[tag+'_'+kind]=dict(complex_ratio=[float(alpha.real),float(alpha.imag)],
            relative_fit_residual=float(np.linalg.norm(v-alpha*u)/np.linalg.norm(v)))
sp=[complex(*rows[label+'_fwd']['s_params']['S31']) for label in ('forward','reverse')]
mag_difference=float(abs(abs(sp[0])-abs(sp[1])))
g1,g2=[gradients[label][ix] for label in ('forward','reverse')]
gradient_sum=float(np.linalg.norm(g1+g2)/np.linalg.norm(g1))
all_meshes_equal=all(rows[k]['mesh_sha256']==rows['forward_fwd']['mesh_sha256'] for k in rows)
reference_report=json.loads((root/'coupler_td_fd_field_reference/status.json').read_text(encoding='utf-8'))
td=[np.load(root/'coupler_td_fd_field_reference'/name/'boundary.npz') for name in ('td_fwd','td_bwd')]
gtd=np.real(a.shape_derivative_from_fields(boundary,td[0]['E'].T,td[0]['H'].T,td[1]['E'].T,td[1]['H'].T,
                                         prefactor,a.EPS0,a.MU0))
gfd=s['g'][ix]
cached=s['d']
gauge_original=np.real(a.shape_derivative_from_fields(boundary,cached['E_fwd'],cached['H_fwd'],
    cached['E_bwd'],cached['H_bwd'],prefactor,a.EPS0,a.MU0))
gauge_reversed=np.real(a.shape_derivative_from_fields(boundary,cached['E_fwd'],cached['H_fwd'],
    -cached['E_bwd'],-cached['H_bwd'],prefactor,a.EPS0,a.MU0))
assert np.array_equal(gauge_reversed,-gauge_original)
base_meta=json.loads((root/'coupler_planar_smoke/iter_000/meta.json').read_text(encoding='utf-8'))
base_s31=complex(*base_meta['tags']['fwd']['s_params']['S3,1'])
result=dict(control_path=str(control),native_solver_jobs=run['native_solver_jobs'],
    all_reference_control_meshes_equal=all_meshes_equal,
    port_reference_voltage={k:v['port_voltage'] for k,v in reference_report.items() if v['port_voltage']},
    td_fd_gradient_cosine=float(np.dot(gtd,gfd)/np.linalg.norm(gtd)/np.linalg.norm(gfd)),
    forward_s31_magnitudes=[abs(v) for v in sp],forward_s31_magnitude_absolute_difference=mag_difference,
    forward_s31_phase_difference_deg=float(np.angle(sp[1]/sp[0],deg=True)),field_fits=fits,
    gradient_sign_flip_relative_residual=gradient_sum,
    fixed_dt_candidate_geometry=metrics,
    source_mode='unit',pseudo_dt=1e-8,connectivity_constraint=False,
    algebraic_mode_basis_sign_test=dict(scope='Exact basis-sign transformation of saved real CST fields and S31; no new EM solve',
        original_goal_abs_s31=float(abs(base_s31)),
        transformed_goal_abs_s31=float(abs(-base_s31)),
        maximum_gradient_sign_flip_residual=float(np.max(abs(gauge_original+gauge_reversed))),
        objective_unchanged=True,gradient_reversed=True,reference_invariance_failed=True))
result['reference_invariance_failure_confirmed_in_native_same_mesh_test']=bool(all_meshes_equal and mag_difference<1e-5 and gradient_sum<1e-4)
result['reference_invariance_failure_confirmed_algebraically']=True
result['native_control_supports_sign_dependence']=bool(gradient_sum<.1 and abs(abs(result['forward_s31_phase_difference_deg'])-180)<2)
objectives=control/'updated_objectives.json'
if objectives.exists():result['native_updated_objectives']=json.loads(objectives.read_text(encoding='utf-8'))
result['paper_reproduction_passed']=False
(root/'coupler_root_cause_summary.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,indent=2))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig,axes=plt.subplots(1,3,figsize=(14,5),sharex=True,sharey=True)
initial=unary_union([Polygon(r.outer,r.holes) for r in compose_metal_regions(cfg,old.phi)])
for ax,label in zip(axes,('initial','forward','reverse')):
    for fixed in s['fixed']:
        ax.fill(*fixed.exterior.xy,color='.75')
    geo=(initial if label=='initial' else unary_union([Polygon(r.outer,r.holes)
        for r in compose_metal_regions(cfg,np.load(control/(label+'_update/state.npz'))['phi'])]))
    polygons=list(geo.geoms) if geo.geom_type=='MultiPolygon' else [geo]
    for p in polygons:ax.fill(*p.exterior.xy,color='#287bb5',alpha=.9)
    title={'initial':'Initial','forward':'Reference A: connected','reverse':'Reference B: disconnected'}[label]
    if label!='initial' and objectives.exists():
        row=result['native_updated_objectives'][label]
        title+=f"\nS31={row['s_params_db']['S31']:.2f} dB, D={row['directivity_db']:.2f} dB"
    ax.set_title(title,fontsize=11);ax.set_aspect('equal');ax.set_xlim(-2,14);ax.set_ylim(-7.5,3)
    ax.set_xlabel('x (mm)');ax.grid(alpha=.15)
axes[0].set_ylabel('y (mm)')
fig.suptitle('Fixed dt = 1e-8, unit reverse source; A/B tetrahedral meshes differ',fontsize=12)
fig.tight_layout()
fig.savefig(root/'coupler_reference_geometry_control.png',dpi=150)
