"""Native single-frequency FD baseline probe; no synthetic EM data."""
from pathlib import Path
from dataclasses import replace
import json
import sys
import traceback
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import make_level_set
from eaopt.geometry.design import compose_metal_regions,region_rings
from eaopt.solver.case import load_case
from eaopt.solver.cst import CstSolver
from eaopt.solver import cst_results as R
from eaopt.runner import _run_output_dir

case=sys.argv[1] if len(sys.argv)>1 else 'coupler'
cfg=CaseConfig.from_yaml(f'configs/{case}.yaml')
setup,model=load_case(cfg)
setup=replace(setup,solver_type='fd')
out=_run_output_dir(Path('results/fd_single_2026-11-02')/f'{case}_probe')
cfg.output.dir=str(out.resolve())
solver=CstSolver(cfg,setup=setup,model=model,auto_init=True,attach=True,save_fields=True)
try:
    ls=make_level_set(cfg)
    solver.begin_iteration(0)
    solver.build_model(region_rings(compose_metal_regions(cfg,ls.phi)))
    solution=solver.solve_forward()
    tree=R._tree_items(solver._open('fwd').model3d,root='')
    (out/'result_tree.json').write_text(json.dumps(tree,indent=2),encoding='utf-8')
    (out/'status.json').write_text(json.dumps({'status':'complete',
        's_params':[[i,j,v.real,v.imag] for (i,j),v in solution.s_params.items()]},indent=2))
    print('FD_PROBE_COMPLETE',case)
except BaseException:
    (out/'status.json').write_text(json.dumps({'status':'failed','traceback':traceback.format_exc()},indent=2))
    raise
finally:
    solver.close()
    if solver.de is not None:
        solver.de.set_quiet_mode(False)
