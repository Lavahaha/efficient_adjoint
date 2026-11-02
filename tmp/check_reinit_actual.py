"""Measure reinitialization alone on saved real shapes; no optimizer or EM run."""
from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_iteration_algorithm import ROOT,OUT,geom,make_level_set,CaseConfig,a

result={}
for case in ('divider','coupler'):
    cfg=CaseConfig.from_yaml(ROOT/f'configs/{case}.yaml')
    original=make_level_set(cfg)
    grid=a.Grid2D(original.nx,original.ny,original.dx,original.xs[0],original.ys[0])
    result[case]={}
    for label,path in [('initial','phi_before_iter_000.npz'),('after_update2','phi_after_iter_001.npz')]:
        phi=np.load(ROOT/f'results/{case}_local_serial_smoke'/path)['phi']
        ls=a.LevelSet(grid,phi.copy())
        before=geom(cfg,phi)
        rows=[]
        for k in range(1,51):
            ls.reinit()
            assert np.isfinite(ls.phi).all()
            if k in (1,10,50):
                shape=geom(cfg,ls.phi)
                rows.append({'cycles':k,'hausdorff_mm':shape.boundary.hausdorff_distance(before.boundary),
                    'symmetric_difference_mm2':shape.symmetric_difference(before).area,
                    'area_change_mm2':shape.area-before.area})
        result[case][label]=rows
(OUT/'reinit_actual_shapes.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
