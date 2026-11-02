"""Sampling and E/H contributions from the same saved real fields; no fitting."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import adjoint_levelset as a
import run_qtem_checks as em
from run_directional_fd_checks import boundary_and_directions
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case

def bundle_at_z(folder,z0):
    fields=[]
    for kind in ('Efield','Hfield'):
        with np.load(folder/f'{kind}.npz') as d:
            axes=[d['origin'][j]+d['spacing'][j]*np.arange(d['data'].shape[j]) for j in range(3)]
            z=axes[2]
            hi=int(np.searchsorted(z,z0,side='right'))
            assert 0<hi<len(z)
            t=(z0-z[hi-1])/(z[hi]-z[hi-1])
            values=(1-t)*d['data'][:,:,hi-1,:]+t*d['data'][:,:,hi,:]
            fields.append(np.moveaxis(values,-1,0))
    return a.FieldBundle(axes[0],axes[1],*fields)

report={}
for case in ('divider','coupler'):
    root=em.OUT/case/'fd_fine/base'
    if not (root/'bwd/Hfield.npz').exists():
        continue
    cfg=CaseConfig.from_yaml(em.ROOT/f'configs/{case}.yaml')
    setup,_=load_case(cfg)
    boundary,directions=boundary_and_directions(cfg)
    step=setup.resolve_export_steps(cfg.field_export_step_mm)[0]
    prefactor=-2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w
    rows=[]
    for z in (-.1,0.,.1):
        fwd,adj=(bundle_at_z(root/tag,z) for tag in ('fwd','bwd'))
        for offset in (.5*step,step,1.5*step):
            ge=np.real(a.shape_derivative_complex(boundary,fwd,adj,prefactor,offset,a.EPS0*setup.eps_r,0))
            gh=np.real(a.shape_derivative_complex(boundary,fwd,adj,prefactor,offset,0,a.MU0))
            full=np.real(a.shape_derivative_complex(boundary,fwd,adj,prefactor,offset,a.EPS0*setup.eps_r,a.MU0))
            assert np.allclose(ge+gh,full,rtol=1e-12,atol=1e-5)
            rows.append({'z_mm':z,'outward_offset_mm':offset,
                'directional':{name:{'E_term':float(np.sum(ge*u*boundary.weights)),
                    'H_term':float(np.sum(gh*u*boundary.weights)),
                    'total':float(np.sum(full*u*boundary.weights))} for name,u in directions.items()}})
    report[case]=rows
(em.OUT/'gradient_sampling.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
