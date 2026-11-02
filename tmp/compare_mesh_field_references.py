"""Separate common phase changes from spatial changes in existing real fields."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_iteration_algorithm import load_bundle
from run_directional_fd_checks import boundary_and_directions,goal
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
import run_qtem_checks as em
import adjoint_levelset as a

report={}
for case in ('divider','coupler'):
    cfg=CaseConfig.from_yaml(em.ROOT/f'configs/{case}.yaml')
    setup,_=load_case(cfg)
    b,ds=boundary_and_directions(cfg)
    root=em.OUT/case
    ref={tag:load_bundle(root/'modes2'/tag) for tag in ('fwd','bwd')}
    rows={}
    for variant in ('modes2','fd/base','fd_fixed/base','fd_locked/base','fd_fine/base'):
        folder=root/variant
        if not all((folder/tag/'Efield.npz').exists() and (folder/tag/'Hfield.npz').exists() for tag in ('fwd','bwd')):
            continue
        bundles={tag:load_bundle(folder/tag) for tag in ('fwd','bwd')}
        row={'goal_abs':goal(folder/'fwd',cfg),'fields':{}}
        for tag in ('fwd','bwd'):
            for name in ('E','H'):
                x=getattr(ref[tag],name).ravel()
                y=getattr(bundles[tag],name).ravel()
                z=np.vdot(x,y)
                overlap=abs(z)/(np.linalg.norm(x)*np.linalg.norm(y))
                assert overlap<=1+1e-12
                row['fields'][tag+'_'+name]={'phase_deg':float(np.angle(z,deg=True)),
                    'normalized_overlap':float(overlap),'amplitude_ratio':float(np.linalg.norm(y)/np.linalg.norm(x))}
        g=np.real(a.shape_derivative_complex(b,bundles['fwd'],bundles['bwd'],
            -2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w,
            setup.resolve_export_steps(cfg.field_export_step_mm)[0],a.EPS0*setup.eps_r,a.MU0))
        row['raw_directional_predictions']={name:float(np.sum(g*u*b.weights)) for name,u in ds.items()}
        rows[variant]=row
    report[case]=rows
(em.OUT/'mesh_field_references.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
