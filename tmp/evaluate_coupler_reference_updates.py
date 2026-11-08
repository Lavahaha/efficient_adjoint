"""Evaluate the two native reference-control updates; no production defaults change."""
from pathlib import Path
from dataclasses import replace
import json,sys,traceback
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver.cst import _connect,_open_or_reuse,_save,_update_design_region,_run_solver,_clear_results
from eaopt.solver import cst_results as R
from eaopt.geometry.contour import classify_metal_regions
root=Path('results/fd_single_2026-11-02')
summary=json.loads((root/'coupler_root_cause_summary.json').read_text(encoding='utf-8'))
control=Path(summary['control_path'])
cfg=CaseConfig.from_yaml('configs/coupler.yaml');setup,model=load_case(cfg)
setup=replace(setup,solver_type='fd')
de=_connect(True,False,print,quiet=True);project=None
report={}
try:
    for label in ('forward','reverse'):
        folder=control/(label+'_update')
        if (folder/'native_evaluation.json').exists():
            report[label]=json.loads((folder/'native_evaluation.json').read_text(encoding='utf-8'));continue
        shapes=json.loads((folder/'shape.json').read_text(encoding='utf-8'))['polygons']
        regions=classify_metal_regions([np.asarray(p) for p in shapes])
        project=_open_or_reuse(de,control/(label+'_fwd/model.cst'),print)
        _save(project,folder/'model.cst',print)
        _clear_results(project.model3d,what=label,log=print)
        if any(r.holes for r in regions):raise ValueError('This coupler control requires solid profiles')
        _update_design_region(project.model3d,[r.outer for r in regions],setup,model,what=label,log=print)
        seconds=_run_solver(project.model3d,print)
        _save(project,folder/'model.cst',print)
        sp=R.read_s_params((folder/'model.cst').resolve(),1,freq_ghz=5.,exact_frequency=True)
        pin=R.read_incident_power((folder/'model.cst').resolve(),1,5.)
        db={f'S{i}{j}':float(20*np.log10(abs(v))) for (i,j),v in sp.items()}
        row=dict(seconds=seconds,pin_w=pin,s_params={f'S{i}{j}':[v.real,v.imag] for (i,j),v in sp.items()},
                 s_params_db=db,directivity_db=db['S31']-db['S41'])
        (folder/'native_evaluation.json').write_text(json.dumps(row,indent=2)+'\n',encoding='utf-8')
        report[label]=row
        project.close();project=None
    (control/'updated_objectives.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    (control/'update_completion.json').write_text(json.dumps({'status':'complete','native_solver_jobs':2},indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
except BaseException:
    (control/'update_failure.json').write_text(json.dumps({'traceback':traceback.format_exc()},indent=2),encoding='utf-8')
    raise
finally:
    if project is not None:project.close()
    de.set_quiet_mode(False)
