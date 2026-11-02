"""Divider: sign trace, exact-point sampling and independent real CAD tests."""
from pathlib import Path
import sys,json,hashlib,traceback
import numpy as np
from scipy.interpolate import RegularGridInterpolator
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import adjoint_levelset as a
import run_qtem_checks as em
from check_iteration_algorithm import load_bundle
from run_directional_fd_checks import goal
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import make_level_set
from eaopt.geometry.design import connection_boundary_mask
from eaopt.runner import _movement_constraints
from eaopt.solver.case import load_case
from eaopt.solver.cst import _connect,_open_or_reuse
from eaopt.solver import cst_results as R

RUN=em.OUT/'divider/sign_sampling_2026-11-02'
OUT=RUN/'threads2' if '--threads2' in sys.argv else RUN
BASE=em.OUT/'divider/fd_fine/base'

def boundary(cfg):
    original=orient(unary_union([Polygon(p.vertices) for p in cfg.initial_metal]),sign=1)
    vertices=np.asarray(original.exterior.coords); pts=[]
    for p,q in zip(vertices[:-1],vertices[1:]):
        n=max(1,int(np.ceil(np.linalg.norm(q-p)/.2)))
        pts.extend(p+(q-p)*t/n for t in range(n))
    pts=np.asarray(pts)
    assert Polygon(pts).symmetric_difference(original).area<1e-8
    prev=pts-np.roll(pts,1,axis=0); nxt=np.roll(pts,-1,axis=0)-pts
    lp=np.linalg.norm(prev,axis=1); ln=np.linalg.norm(nxt,axis=1)
    tangent=prev/lp[:,None]+nxt/ln[:,None]
    normals=np.column_stack([tangent[:,1],-tangent[:,0]])
    normals/=np.linalg.norm(normals,axis=1)[:,None]
    return a.Boundary(pts,normals,(lp+ln)/2,np.ones(len(pts),bool))

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    meta=json.loads((BASE/'fwd/config.json').read_text(encoding='utf-8'))
    cfg=CaseConfig.from_dict(meta['config']); setup,model=load_case(cfg)
    b=boundary(cfg)
    original=make_level_set(cfg)
    mask,*_=_movement_constraints(original,cfg)
    pinned=connection_boundary_mask(original,cfg,model.fixed_metal_polygons()); mask[pinned]=0
    sampler=RegularGridInterpolator((original.xs,original.ys),mask,bounds_error=False,fill_value=0)
    b.active=sampler(b.pts)>.05
    prefactor=-2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w
    # At z=0 retain the audited substrate convention; positive-z samples are in vacuum.
    cases=[(offset,z,eps) for offset in (.2,.1,.05) for z,eps in ((0.,3.),(.0175,1.),(-.1,3.))]
    xyz=np.round(np.vstack([np.column_stack([b.pts+offset*b.normals,np.full(len(b.pts),z)])
        for offset,z,eps in cases]),9)
    np.savez_compressed(OUT/'sample_geometry.npz',points=xyz,boundary=b.pts,normals=b.normals,weights=b.weights,active=b.active)
    values={}; de=None
    try:
        attach='--attach' in sys.argv
        de=_connect(attach,not attach,em.log,quiet=True)
        for tag in ('fwd','bwd'):
            dest=RUN/f'{tag}_samples.npz'
            if dest.exists():
                d=np.load(dest); assert np.array_equal(d['points'],xyz)
                values[tag]=(d['E'],d['H']); continue
            project=_open_or_reuse(de,BASE/tag/'model.cst',em.log)
            try:
                fields=[R.export_field_points(project.model3d,kind,5.,xyz,OUT/f'{tag}_{kind}.txt',log=em.log)
                    for kind in ('Efield','Hfield')]
                values[tag]=tuple(f.reshape(len(cases),len(b.pts),3).transpose(0,2,1) for f in fields)
                np.savez_compressed(dest,points=xyz,E=values[tag][0],H=values[tag][1])
            finally:
                project.close()
        ef,hf=values['fwd']; eb,hb=values['bwd']
        gcomplex=[]
        for i,(offset,z,eps) in enumerate(cases):
            gcomplex.append(a.shape_derivative_from_fields(b,ef[i],hf[i],eb[i],hb[i],prefactor,a.EPS0*eps,a.MU0))
        g0=np.real(gcomplex[0]); g0[~b.active]=0
        ls=a.LevelSet(a.Grid2D(original.nx,original.ny,original.dx,original.xs[0],original.ys[0]),original.phi.copy())
        V=a.boundary_to_grid_velocity(ls,b,g0,mask,cfg.level_set.extension_band_mm/original.dx,smooth_sigma_cells=1.5)
        vb=RegularGridInterpolator((original.xs,original.ys),V,bounds_error=False,fill_value=0)(b.pts)
        # Keep all contact edges fixed and measure a physical direction, independent of dt.
        vb[~b.active]=0; direction=vb/np.max(abs(vb))
        x=(b.pts[:,0]-15.)/2.5; y=b.pts[:,1]/1.5
        holdout=np.where((abs(x)<1)&(abs(y)<1),np.cos(np.pi*x/2)**2*np.cos(np.pi*y/2)**2,0)
        holdout[~b.active]=0; holdout/=holdout.max()
        directions={'velocity':direction,'input_width':holdout}
        np.savez_compressed(OUT/'directions.npz',pts=b.pts,normals=b.normals,weights=b.weights,g_raw=g0,V=V,velocity=direction,input_width=holdout)
        assert np.sum(g0*direction*b.weights)>0
        report={'sampling':[],'rows':[],'dt_production_unchanged':cfg.optimizer.pseudo_time_step}
        for i,(offset,z,eps) in enumerate(cases):
            report['sampling'].append({'offset_mm':offset,'z_mm':z,'eps_r_sample':eps,
                'directions':{name:[float(q.real),float(q.imag)] for name,u in directions.items()
                    for q in [np.sum(gcomplex[i]*u*b.weights)]}})
        em.write(OUT/'checks.json',report)
        mesh_blocks=[tuple(row) for row in meta['blocks'] if row[0]=='FD fixed mesh controls']
        blocks=mesh_blocks+[('TD precision','With Solver\n .SteadyStateLimit "-60"\nEnd With')]
        if '--threads2' in sys.argv:
            blocks.append(('Memory control','With Solver\n .MaximumNumberOfThreads "2"\nEnd With'))
        base_mesh=(BASE/'fwd/Model.msh').read_bytes()
        for name,u in directions.items():
            for h in ((.04,.08) if name=='velocity' else (.04,)):
                result={}
                for sign in (1,-1):
                    pts=b.pts+sign*h*u[:,None]*b.normals
                    shape=Polygon(pts)
                    assert shape.is_valid and shape.geom_type=='Polygon'
                    folder=OUT/f'{name}_{h:.2f}_{"plus" if sign>0 else "minus"}'
                    em.run_project(de,cfg,setup,model,folder,2,'fwd',polygons=[np.vstack([pts,pts[0]])],
                        export_fields=False,extra_blocks=blocks,clone_from=BASE/'fwd/model.cst')
                    assert (folder/'Model.msh').read_bytes()==base_mesh,'Actual FD grid changed'
                    result[sign]=goal(folder,cfg)
                row={'direction':name,'h_mm':h,'plus':result[1],'minus':result[-1],
                    'FD_per_mm':(result[1]-result[-1])/(2*h),
                    'prediction_raw':float(np.sum(g0*u*b.weights))}
                report['rows'].append(row); em.write(OUT/'checks.json',report); em.log(f'SIGN_FD {row}')
        em.write(OUT/'status.json',{'status':'complete','new_solves':6})
    except BaseException:
        em.write(OUT/'status.json',{'status':'failed','traceback':traceback.format_exc()}); raise
    finally:
        if de is not None and '--attach' not in sys.argv: de.close()

if __name__=='__main__': main()
