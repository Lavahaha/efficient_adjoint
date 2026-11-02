"""External interpolation convergence on a fixed solved CST field (no EM rerun)."""
from pathlib import Path
import json,sys,itertools
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import adjoint_levelset as a
import run_qtem_checks as em
from check_iteration_algorithm import load_bundle
from eaopt.solver import cst_results as R
from eaopt.solver.cst import _connect,_open_or_reuse

RUN=em.OUT/'divider/sign_sampling_2026-11-02'
BASE=em.OUT/'divider/fd_fine/base'

def main():
    target_z = -.1 if '--substrate' in sys.argv else (.1 if '--air' in sys.argv else 0.)
    prefix = 'substrate_' if target_z < 0 else ('air_' if target_z > 0 else '')
    geo=np.load(RUN/'sample_geometry.npz'); d=np.load(RUN/'directions.npz')
    b=a.Boundary(geo['boundary'],geo['normals'],geo['weights'],geo['active'])
    points=np.column_stack([b.pts+.2*b.normals,np.full(len(b.pts),target_z)])
    plans=[('legacy_xyz',(.2,.2,.1)),('xy_only',(.2,.2,None)),('z_only',(None,None,.1)),
        ('xyz_0.1',(.1,.1,.05)),('xyz_0.05',(.05,.05,.025)),('xyz_0.025',(.025,.025,.0125))]
    coords=[points]; weights=[]; spans=[]; start=len(points)
    for name,steps in plans:
        choices=[]
        for axis,step in enumerate(steps):
            if step is None:
                choices.append([(points[:,axis],np.ones(len(points)))])
            else:
                origin=(5.,-10.,-.7485)[axis]
                low=origin+np.floor((points[:,axis]-origin)/step)*step
                t=(points[:,axis]-low)/step
                choices.append([(low,1-t),(low+step,t)])
        corners=list(itertools.product(*choices))
        xyz=np.stack([np.column_stack([v[0] for v in corner]) for corner in corners],axis=1)
        w=np.stack([np.prod([v[1] for v in corner],axis=0) for corner in corners],axis=1)
        assert np.allclose(w.sum(axis=1),1)
        coords.append(xyz.reshape(-1,3)); weights.append(w)
        spans.append((start,start+xyz.size//3,len(corners))); start+=xyz.size//3
    allpoints=np.round(np.vstack(coords),9)
    sampled={}; de=_connect(True,False,em.log,quiet=True)
    try:
        for tag in ('fwd','bwd'):
            cache=RUN/f'{prefix}convergence_{tag}.npz'
            if cache.exists():
                z=np.load(cache); assert np.array_equal(z['points'],allpoints)
                sampled[tag]=(z['E'],z['H']); continue
            p=_open_or_reuse(de,BASE/tag/'model.cst',em.log)
            try:
                fields=[R.export_field_points(p.model3d,kind,5.,allpoints,
                    RUN/f'{prefix}convergence_{tag}_{kind}.txt',log=em.log) for kind in ('Efield','Hfield')]
                sampled[tag]=fields
                np.savez_compressed(cache,points=allpoints,E=fields[0],H=fields[1])
            finally: p.close()
    finally:
        pass  # The user-owned CST environment remains open.
    prefactor=-2j*2*np.pi*5e9/.5
    def kernel(fields):
        return a.shape_derivative_from_fields(b,*[v.T for v in fields],prefactor,a.EPS0*(3 if target_z<=0 else 1),a.MU0)
    direct=[sampled[tag][j][:len(points)] for tag in ('fwd','bwd') for j in (0,1)]
    reference=kernel(direct); mask=b.active
    rows=[]
    for (name,steps),w,(lo,hi,count) in zip(plans,weights,spans):
        fields=[np.sum(sampled[tag][j][lo:hi].reshape(len(points),count,3)*w[:,:,None],axis=1)
            for tag in ('fwd','bwd') for j in (0,1)]
        q=kernel(fields)
        if name=='legacy_xyz' and target_z==0:
            old=a.shape_derivative_complex(b,load_bundle(BASE/'fwd'),load_bundle(BASE/'bwd'),prefactor,.2,a.EPS0*3,a.MU0)
            assert np.linalg.norm(q-old)/np.linalg.norm(old)<1e-6
        rows.append({'plan':name,'step_mm':steps,
            'field_relative':[float(np.linalg.norm(x[mask]-y[mask])/np.linalg.norm(y[mask])) for x,y in zip(fields,direct)],
            'complex_sensitivity_relative':float(np.linalg.norm((q-reference)[mask])/np.linalg.norm(reference[mask])),
            'directions':{name:float(np.sum(np.real(q)*d[name]*b.weights)) for name in ('velocity','input_width')}})
    em.write(RUN/f'{prefix}sampling_convergence.json',{'z_mm':target_z,'reference':'native exact point values on the same solved mesh; not absolute continuum truth','rows':rows})
    print(json.dumps(rows,indent=2))

if __name__=='__main__': main()
