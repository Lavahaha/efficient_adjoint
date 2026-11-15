"""One solved antenna, multiple heights: test the single-plane density approximation."""
import json
import sys
from pathlib import Path
from datetime import datetime,timezone
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst_setup import CstSetup
from eaopt.solver.cst import CstSolver,_run_solver,_save
from eaopt.solver import cst_results as results
from eaopt.cli import make_log,safe_console


def main(source):
    safe_console()
    source=Path(source).resolve()
    frozen=json.loads((source.parent/'run_config.json').read_text())
    cfg=CaseConfig.from_dict(frozen['config'])
    setup=CstSetup(**frozen['setup'])
    _,model=load_case(cfg)
    saved=np.load(source/'boundary_fields.npz')
    ids=saved['boundary_indices']
    xy=saved['boundary'][ids];normals=saved['normals'][ids];weights=saved['weights'][ids]
    rings=[np.asarray(p,float) for p in json.loads((source/'shape.json').read_text())['polygons']]
    out=ROOT/'results'/('antenna_thickness_density_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    out.mkdir()
    cfg.output.dir=str(out)
    log=make_log(out/'run.log')
    solver=CstSolver(cfg,setup=setup,model=model,force_new=True,auto_init=True,point_sampling=True,log=log)
    report=dict(status='running',source=str(source),scope='single forward solve, S11 self-adjoint fields, normalized sidewall density; no optimization changes',samples=[])
    def save(): (out/'results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        solver.begin_iteration(0);solver.build_model(rings)
        project=solver.projects['fwd'];path=solver.project_path('fwd')
        _run_solver(project.model3d,log);_save(project,path,log)
        if project.model3d.get_solver_run_info()['state']!='SUCCESS': raise RuntimeError('Field solve failed')
        s=results.read_s_params(path,1,response_ports=(1,),freq_ghz=setup.frequency_ghz,exact_frequency=True,log=log)[(1,1)]
        pin=results.read_incident_power(path,1,setup.frequency_ghz,log=log)
        solver._solved_at['fwd']=0
        report.update(S11=[s.real,s.imag],magnitude=abs(s),incident_power_w=pin)
        omega=2*np.pi*setup.frequency_ghz*1e9
        directions={'length':(xy[:,0]+20)/20*normals[:,0],
                    'width':(xy[:,0]+20)/20*(xy[:,1]/10)*normals[:,1]}
        for offset,order in ((.05,3),(.05,5),(.025,5),(.0125,5)):
            z,w=np.polynomial.legendre.leggauss(order)
            z=(z+1)*setup.metal_thickness_mm/2;w=w*setup.metal_thickness_mm/2
            pxy=xy+offset*normals
            xyz=np.column_stack((np.repeat(pxy,order,axis=0),np.tile(z,len(xy))))
            ef,hf=solver.sample_fields(xyz,tag='fwd',freq_ghz=setup.frequency_ghz)
            ef=ef.reshape(3,len(xy),order);hf=hf.reshape(3,len(xy),order)
            integrated=als.s_parameter_sidewall_density(normals,w,ef,hf,ef,hf,omega,np.sqrt(pin),np.sqrt(pin))
            mid=order//2
            midpoint=als.s_parameter_sidewall_density(normals,[setup.metal_thickness_mm],ef[:,:,mid:mid+1],hf[:,:,mid:mid+1],ef[:,:,mid:mid+1],hf[:,:,mid:mid+1],omega,np.sqrt(pin),np.sqrt(pin))
            real_integral=np.real(np.conj(s)/abs(s)*integrated)
            real_midpoint=np.real(np.conj(s)/abs(s)*midpoint)
            disagreement=(real_integral*real_midpoint)<0
            row=dict(offset_mm=offset,height_order=order,
                relative_density_l2_error=float(np.sqrt(np.sum(weights*(real_midpoint-real_integral)**2)/np.sum(weights*real_integral**2))),
                sign_disagreement_count=int(disagreement.sum()),
                sign_disagreement_absolute_weight_fraction=float(np.sum(weights[disagreement]*abs(real_integral[disagreement]))/np.sum(weights*abs(real_integral))),
                directions={name:dict(integrated_absS_derivative_per_mm=float(np.sum(weights*real_integral*velocity)),
                                     midpoint_absS_derivative_per_mm=float(np.sum(weights*real_midpoint*velocity))) for name,velocity in directions.items()})
            report['samples'].append(row)
            np.savez_compressed(out/f'offset_{offset:g}_order_{order}.npz',boundary=xy,normals=normals,weights=weights,z_mm=z,z_weights_mm=w,
                E=ef,H=hf,complex_density=integrated,midpoint_complex_density=midpoint)
            save();log(f'[thickness density] {json.dumps(row)}')
        report['status']='complete'
    except BaseException as error:
        report.update(status='failed',error=repr(error));raise
    finally: save();solver.close()
    print(out)


if __name__=='__main__': main(sys.argv[1])
