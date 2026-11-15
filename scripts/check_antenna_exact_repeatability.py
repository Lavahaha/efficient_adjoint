"""Repeat one literal antenna contour to separate solve and update variability."""
import json
import hashlib
import sys
from pathlib import Path
from datetime import datetime,timezone
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst_setup import CstSetup
from eaopt.solver.cst import CstSolver,_run_solver,_save
from eaopt.solver import cst_results as results
from eaopt.cli import make_log,safe_console


def contour_hash(rings):
    digest=hashlib.sha256()
    for ring in rings:
        data=np.asarray(ring,dtype='<f8')
        digest.update(str(data.shape).encode())
        digest.update(data.tobytes())
    return digest.hexdigest()


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): digest.update(chunk)
    return digest.hexdigest()


def main(shape_path):
    safe_console()
    shape_path=Path(shape_path).resolve()
    source=shape_path.parent.parent
    frozen=json.loads((source/'run_config.json').read_text())
    cfg=CaseConfig.from_dict(frozen['config'])
    if cfg.name!='antenna': raise ValueError('Antenna archive required')
    target=[np.asarray(p,float) for p in json.loads(shape_path.read_text())['polygons']]
    baseline=[np.asarray(p,float) for p in json.loads((source/'iter_000/shape.json').read_text())['polygons']]
    setup=CstSetup(**frozen['setup'])
    if setup.solver_type!='fd': raise ValueError('FD archive required')
    _,model=load_case(cfg)
    out=ROOT/'results'/('antenna_exact_repeat_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    out.mkdir()
    cfg.output.dir=str(out)
    log=make_log(out/'run.log')
    solver=CstSolver(cfg,setup=setup,model=model,force_new=True,auto_init=True,point_sampling=True,log=log)
    report=dict(status='running',source=str(shape_path),target_input_sha256=contour_hash(target),
        scope='literal archived geometry; baseline-target-target-baseline-target; no optimizer, field sampling or step changes',
        evaluations=[])
    def save(): (out/'results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    prepared_hash=None
    try:
        for i,(name,rings) in enumerate((('baseline',baseline),('target',target),('target',target),('baseline',baseline),('target',target))):
            solver.begin_iteration(i)
            solver.build_model(rings)
            actual_hash=contour_hash(solver.last_prepared_polys)
            if name=='target':
                if prepared_hash is None: prepared_hash=actual_hash
                if prepared_hash!=actual_hash: raise AssertionError('Prepared target changed across repetitions')
            project=solver.projects['fwd']
            path=solver.project_path('fwd')
            elapsed=_run_solver(project.model3d,log)
            _save(project,path,log)
            if project.model3d.get_solver_run_info()['state']!='SUCCESS':
                raise RuntimeError('Repeatability solve failed')
            s=results.read_s_params(path,1,response_ports=(1,),freq_ghz=setup.frequency_ghz,exact_frequency=True,log=log)[(1,1)]
            mesh=path.with_suffix('')/'Result'
            hashes={p.name:file_hash(p) for p in (mesh/'moegeom.tet',mesh/'topot.tet',mesh/'bc3d.tet') if p.exists()}
            report['evaluations'].append(dict(index=i,geometry=name,input_sha256=contour_hash(rings),
                prepared_sha256=actual_hash,real=s.real,imag=s.imag,magnitude=abs(s),solver_seconds=elapsed,
                mesh_file_hashes=hashes))
            save()
            log(f'[exact repeat] {name} {i}: |S11|={abs(s):.10g}')
        report['status']='complete'
        report['magnitude_ranges']={name:float(np.ptp([r['magnitude'] for r in report['evaluations'] if r['geometry']==name])) for name in ('baseline','target')}
    except BaseException as error:
        report.update(status='failed',error=repr(error))
        raise
    finally:
        save()
        solver.close()
    print(out)


if __name__=='__main__': main(sys.argv[1])
