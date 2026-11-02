"""Check complex Maxwell curl signs in source-free air using saved CST fields."""
from pathlib import Path
import json,sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import adjoint_levelset as a

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/adjoint_checks_2026-11-01'
report={}
for case in ('divider','coupler'):
    report[case]={}
    for variant in ('modes2','fd_fine/base'):
        folder=OUT/case/variant/'fwd'
        if not (folder/'Hfield.npz').exists():
            continue
        with np.load(folder/'Efield.npz') as e,np.load(folder/'Hfield.npz') as h:
            assert np.array_equal(e['origin'],h['origin']) and np.array_equal(e['spacing'],h['spacing'])
            axes=[e['origin'][j]+e['spacing'][j]*np.arange(e['data'].shape[j]) for j in range(3)]
            select=[np.flatnonzero((v>=v[0]+.6)&(v<=v[-1]-.6)) if j<2 else
                np.flatnonzero((v>=.5)&(v<=2.0)) for j,v in enumerate(axes)]
            assert all(len(v)>4 for v in select)
            # Crop first to avoid allocating derivatives over the whole export.
            ix=np.ix_(*select)
            E=e['data'][ix]; H=h['data'][ix]
            spacing=e['spacing']*1e-3
        def curl(F):
            return np.stack([np.gradient(F[...,2],spacing[1],axis=1)-np.gradient(F[...,1],spacing[2],axis=2),
                np.gradient(F[...,0],spacing[2],axis=2)-np.gradient(F[...,2],spacing[0],axis=0),
                np.gradient(F[...,1],spacing[0],axis=0)-np.gradient(F[...,0],spacing[1],axis=1)],axis=-1)
        inner=(slice(1,-1),)*3
        ce=curl(E)[inner].ravel(); ch=curl(H)[inner].ravel()
        ev=E[inner].ravel(); hv=H[inner].ravel()
        omega=2*np.pi*5e9
        row={}
        for name,c,target in [('curlE',ce,-1j*omega*a.MU0*hv),('curlH',ch,1j*omega*a.EPS0*ev)]:
            fit=np.vdot(target,c)/np.vdot(target,target)
            row[name]={'paper_sign_relative_residual':float(np.linalg.norm(c-target)/np.linalg.norm(target)),
                'opposite_sign_relative_residual':float(np.linalg.norm(c+target)/np.linalg.norm(target)),
                'complex_fit_to_paper_sign':[float(fit.real),float(fit.imag)]}
        report[case][variant]=row
(OUT/'field_phasor_checks.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
