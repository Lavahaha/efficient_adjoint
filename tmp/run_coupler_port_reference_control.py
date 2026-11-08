"""Native phase-reference-only control on cloned, unchanged coupler CAD."""
from pathlib import Path
import hashlib,json,sys,traceback
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eaopt.solver.cst import _connect,_open_or_reuse,_save,_apply_blocks,_run_solver
from eaopt.solver.cst_model import frequency_domain_solver_setup
from eaopt.solver import cst_results as R
from eaopt.runner import _run_output_dir

root=Path('results/fd_single_2026-11-02')
source=(root/'coupler_probe/run_20261102T091050988480Z/cst/coupler_fwd.cst').resolve()
if '--hex' in sys.argv:
    raise ValueError('CST 2024 reports port impedance/calibration unsupported for hex meshes; historical hex jobs are excluded')
out=_run_output_dir(root/'coupler_port_reference_control')
cached=np.load(root/'coupler_planar_smoke/iter_000/boundary_fields.npz')
points=cached['points_mm']
de=_connect(True,False,print,quiet=True)
report={'scope':'Port 3 calibration-line direction only, frozen CAD and fixed dt; no source factor or connectivity constraint',
        'runs':[],'native_solver_jobs':0,'mesh_type':'tet'}
project=None
try:
    for reverse in (False,True):
        for tag,stim in [('fwd',1),('bwd',3)]:
            folder=out/f'{"reverse" if reverse else "forward"}_{tag}'
            folder.mkdir(parents=True)
            project=_open_or_reuse(de,source,print)
            _save(project,folder/'model.cst',print)
            model=project.model3d
            code=f'''With Port
 .LoadContentForModify "3"
 .PortImpedanceAndCalibration "True"
 .AddModeLineByPoint "1", "-0.8", "-7", "-0.762", "-0.8", "-7", "0", "{str(reverse)}"
 .AddModeLine "1", "1", "1", "1"
 .Modify
End With'''
            solver_code=frequency_domain_solver_setup(stim,5.)
            _apply_blocks(model,[('explicit Port 3 calibration reference',code),
                ('single-frequency source',solver_code)],what=folder.name,log=print)
            seconds=_run_solver(model,print)
            report['native_solver_jobs']+=1
            _save(project,folder/'model.cst',print)
            sp=R.read_s_params((folder/'model.cst').resolve(),stim,freq_ghz=5.,exact_frequency=True)
            pin=R.read_incident_power((folder/'model.cst').resolve(),stim,5.)
            fields=[R.export_field_points(model,kind,5.,points,folder/f'{kind}.txt',stimulus_port=stim)
                    for kind in ('Efield','Hfield')]
            np.savez_compressed(folder/'fields.npz',points_mm=points,E=fields[0].T,H=fields[1].T)
            mesh=folder/'model/Result'
            mesh_names=('moegeom.tet','topot.tet','bc3d.tet')
            hashes={name:hashlib.sha256((mesh/name).read_bytes()).hexdigest() for name in mesh_names}
            row=dict(name=folder.name,reverse=reverse,tag=tag,stimulus_port=stim,pin_w=pin,seconds=seconds,
                s_params={f'S{i}{j}':[v.real,v.imag] for (i,j),v in sp.items()},mesh_sha256=hashes)
            report['runs'].append(row)
            (out/'execution.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
            project.close();project=None
    report['status']='complete'
    (out/'execution.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
except BaseException:
    (out/'failure.json').write_text(json.dumps({'traceback':traceback.format_exc()},indent=2),encoding='utf-8')
    raise
finally:
    if project is not None:project.close()
    de.set_quiet_mode(False)
