"""Native fixed-CAD three-pass mesh/point-field check, separate from optimization."""
from pathlib import Path
import sys,json,traceback,shutil
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eaopt.solver.cst import _connect,_open_or_reuse,_apply_blocks,_save,_run_solver
from eaopt.solver import cst_results as R
from eaopt.runner import _run_output_dir

root=Path('results/fd_single_2026-11-02')
source=root/'coupler_probe/run_20261102T091050988480Z/cst/coupler_fwd.cst'
out=_run_output_dir(root/'coupler_mesh_sampling')
cached=np.load(root/'coupler_smoke/iter_000/boundary_fields.npz')
indices=cached['boundary_indices']; boundary=cached['boundary'][indices];normals=cached['normals'][indices]
plans=[(delta,z) for delta in (.025,.05,.1) for z in (.00875,.0175,.02625)]
points=np.vstack([np.column_stack([boundary+delta*normals,np.full(len(indices),z)]) for delta,z in plans])
de=_connect(True,False,print,quiet=True);project=None
try:
    project=_open_or_reuse(de,source,print)
    _save(project,out/'model.cst',print)
    block='''With MeshAdaption3D
 .SetType "HighFrequencyTet"
 .MinPasses "3"
 .MaxPasses "3"
 .MaxDeltaS "0.001"
 .CellIncreaseFactor "0.5"
End With
With FDSolver
 .MeshAdaptionTet "True"
 .ResetSampleIntervals "all"
 .AddSampleInterval "5", "5", "1", "Single", "True"
End With'''
    _apply_blocks(project.model3d,[('fixed CAD three-pass refinement',block)],what='FD mesh diagnostic',log=print)
    seconds=_run_solver(project.model3d,print)
    _save(project,out/'model.cst',print)
    sp=R.read_s_params(out/'model.cst',1,freq_ghz=5.,exact_frequency=True)
    arrays=[R.export_field_points(project.model3d,kind,5.,points,out/f'{kind}.txt',stimulus_port=1)
            for kind in ('Efield','Hfield')]
    np.savez_compressed(out/'samples.npz',points_mm=points,E=arrays[0],H=arrays[1])
    k=plans.index((.05,.0175));start=k*len(indices);end=start+len(indices)
    changes={kind:float(np.linalg.norm(array[start:end].T-cached[key])/np.linalg.norm(cached[key]))
             for kind,key,array in zip(('E','H'),('E_fwd','H_fwd'),arrays)}
    result={'status':'complete','scope':'Three mesh passes at frozen CAD; not asymptotic gradient certification',
            'seconds':seconds,'plans':plans,'same_point_field_change_from_coarse':changes,
            's_params':[[i,j,v.real,v.imag] for (i,j),v in sp.items()]}
    import cst.results as c
    rm=c.ProjectFile(str((out/'model.cst').resolve()),allow_interactive=True).get_3d()
    tree=rm.get_tree_items();(out/'result_tree.json').write_text(json.dumps(tree,indent=2))
    curves={}
    for name in tree:
        if 'adapt' in name.lower() and ('delta' in name.lower() or 'S3,1' in name):
            try:
                item=rm.get_result_item(name);curves[name]={'x':list(item.get_xdata()),'y':[[complex(v).real,complex(v).imag] for v in item.get_ydata()]}
            except RuntimeError: pass
    result['adaptation_curves']=curves
    (out/'analysis.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
except BaseException:
    (out/'failure.json').write_text(json.dumps({'traceback':traceback.format_exc()},indent=2));raise
finally:
    if project is not None: project.close()
    de.set_quiet_mode(False)
