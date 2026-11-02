"""Validate the completed native FD and one-update run from saved artifacts."""
from pathlib import Path
import hashlib
import json
import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

root = Path('results/adjoint_checks_2026-11-01/divider/sign_sampling_2026-11-02')
run = Path('results/divider_objective_source_points_smoke_2026-11-02')
def read(path):
    return json.loads(path.read_text(encoding='utf-8'))
def lines(path):
    return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
summary = read(run/'adjoint_levelset_summary.json')
history = lines(run/'adjoint_levelset_history.jsonl')
trace = lines(run/'cst_evaluations.jsonl')
assert len(history)==1 and history[0]['updated']
assert len([r for r in trace if r['kind']=='forward'])==2
assert len([r for r in trace if r['kind']=='adjoint'])==1
config = read(run/'run_config.json')['config']
assert config['optimizer']['pseudo_time_step']==1e-8
assert config['optimizer']['velocity_sign']==1
assert summary['boundary_sampling']=='cst-points'
source = next(r for r in trace if r['kind']=='adjoint_source')
initial = next(r for r in trace if r['kind']=='forward')
s = complex(initial['s_real'], initial['s_imag'])
assert np.isclose(complex(source['amplitude_real'],source['amplitude_imag']),np.conj(s)/abs(s))
rows=[]
for i in (0,1):
    folder=run/f'iter_{i:03d}'
    sp={(r[0],r[1]):complex(*r[2:]) for r in read(folder/'s_params_fwd.json')}
    shape=unary_union([Polygon(v) for v in read(folder/'shape.json')['polygons']])
    assert shape.is_valid and shape.geom_type=='Polygon'
    for tag in (('fwd','bwd') if i==0 else ('fwd',)):
        for kind in ('e','h'):
            assert np.isfinite(np.load(folder/f'fields_{tag}_{kind}.npz')['data']).all()
    rows.append({'state':i,'fom_abs':abs(sp[2,1]),
        's_db':{f'S{k}1':float(20*np.log10(abs(sp[k,1]))) for k in (1,2,3)},
        'power_ratio':abs(sp[2,1]/sp[3,1])**2,'area_mm2':shape.area,
        'upper_area_mm2':shape.intersection(box(25.2,0,33.7,9)).area,
        'lower_area_mm2':shape.intersection(box(25.2,-9,33.7,0)).area})
assert rows[1]['fom_abs']>rows[0]['fom_abs']
bridges=[s for s in (run/'run.log').read_text(encoding='utf-8').splitlines() if '桥接' in s]
assert not bridges
assert read(root/'threads2/status.json')['status']=='complete'
base_mesh=(root.parent/'fd_fine/base/fwd/Model.msh').read_bytes()
folders=[root/'threads2'/f'{name}_{h:.2f}_{sign}'
    for name,h in [('velocity',.04),('velocity',.08),('input_width',.04)]
    for sign in ('plus','minus')]
assert all((p/'complete.json').exists() and (p/'Model.msh').read_bytes()==base_mesh for p in folders)
report={'status':'passed','native_new_FD_solves':6,'native_fixed_dt_smoke_solves':3,
    'FD_actual_mesh_identical':True,'FD_mesh_sha256':hashlib.sha256(base_mesh).hexdigest(),
    'fixed_dt':1e-8,'velocity_sign':1,'rows':rows,'source':source,'bridges':bridges,
    'scope':'One successful update and direction signs; no global convergence or quantitative gradient certification',
    'optimizer_summary':summary,'optimizer_history':history,
    'algebra_selfcheck':read(root/'objective_source_selfcheck.json')}
report['sampling_studies']={}
for prefix in ('','substrate_','air_'):
    study=read(root/f'{prefix}sampling_convergence.json')
    assert len(study['rows'])==6
    assert all(np.isfinite(r['complex_sensitivity_relative']) for r in study['rows'])
    report['sampling_studies'][prefix or 'interface']=study
for tag in ('fwd','bwd'):
    direct=np.load(root/f'{tag}_samples.npz')
    repeated=np.load(root/f'convergence_{tag}.npz')
    for kind in ('E','H'):
        assert np.array_equal(repeated[kind][:394],direct[kind][0].T)
        substrate=np.load(root/f'substrate_convergence_{tag}.npz')
        assert np.array_equal(substrate[kind][:394],direct[kind][2].T)
(root/'execution_summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'rows':rows,'history':history},indent=2))
