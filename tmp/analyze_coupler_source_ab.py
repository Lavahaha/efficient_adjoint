"""Validate the two native coupler runs, then compare target and device metrics."""
from pathlib import Path
import hashlib
import json
import re
import sys
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case

root=Path('results/coupler_source_factor_ab_2026-11-02')
def read(path):
    return json.loads(path.read_text(encoding='utf-8'))
def jsonlines(path):
    return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()]
cfg=CaseConfig.from_yaml('configs/coupler.yaml')
_,model=load_case(cfg)
fixed=[Polygon(v) for v in model.fixed_metal_polygons()]
through=next(p for p in fixed if p.bounds[1]>=.9)
legs=[p for p in fixed if p is not through]
report={'scope':'Two updates per arm with native CST; not global convergence or a proof of Eq45 reinterpretation',
        'arms':{},'successful_native_solves':0}
configs={}
for mode in ('unit','objective'):
    run=root/mode
    summary=read(run/'adjoint_levelset_summary.json')
    history=jsonlines(run/'adjoint_levelset_history.jsonl')
    trace=jsonlines(run/'cst_evaluations.jsonl')
    configs[mode]=read(run/'run_config.json')
    assert summary['adjoint_source']==mode
    assert summary['boundary_sampling']=='cst-points'
    assert summary['sample_offset_mm']==.1
    assert len(history)==2 and all(r['updated'] for r in history)
    assert all(r['pseudo_time_step_used']==1e-8 and r['pseudo_time_step_next']==1e-8 and r['backtracks']==0 for r in history)
    assert len([r for r in trace if r['kind']=='forward'])==3
    assert len([r for r in trace if r['kind']=='adjoint'])==2
    sources=[r for r in trace if r['kind']=='adjoint_source']
    assert len(sources)==2 and all(r['mode']==mode for r in sources)
    rows=[]
    for i in range(3):
        folder=run/f'iter_{i:03d}'
        sp={(r[0],r[1]):complex(*r[2:]) for r in read(folder/'s_params_fwd.json')}
        shape=unary_union([Polygon(v) for v in read(folder/'shape.json')['polygons']])
        assert shape.is_valid
        connected=unary_union([shape,*legs])
        sdb={f'S{k}1':float(20*np.log10(abs(sp[k,1]))) for k in (1,2,3,4)}
        row={'state':i,'fom_abs':abs(sp[3,1]),'s_db':sdb,
             'directivity_db':sdb['S31']-sdb['S41'],
             'gap_to_through_mm':shape.distance(through),'area_mm2':shape.area,
             'design_components':1 if shape.geom_type=='Polygon' else len(shape.geoms),
             'arm_and_legs_connected':connected.geom_type=='Polygon',
             's31_complex':[sp[3,1].real,sp[3,1].imag], 'mesh_cells':{}}
        for tag in (('fwd','bwd') if i<2 else ('fwd',)):
            meta=read(folder/'meta.json')['tags'][tag]
            assert meta['pin_w']>0 and meta['s_params']
            report['successful_native_solves']+=1
            for kind in ('e','h'):
                assert np.isfinite(np.load(folder/f'fields_{tag}_{kind}.npz')['data']).all()
            log=(folder/f'cst_{tag}_Model.log').read_text(encoding='utf-8',errors='replace')
            assert 'Solver finished at:' in log and 'Steady state energy criterion met' in log
            row['mesh_cells'][tag]=int(re.search(r'Number of mesh cells:\s*(\d+)',log)[1])
        if i<2:
            source=next(r for r in sources if r['eval_id']==i)
            amplitude=complex(source['amplitude_real'],source['amplitude_imag'])
            expected=np.conj(sp[3,1])/abs(sp[3,1]) if mode=='objective' else 1+0j
            assert np.isclose(amplitude,expected)
            row['source_factor']=[amplitude.real,amplitude.imag]
            fp=run/'cst_work'/f'points_{i}_fwd_5GHz/Efield.points.txt'
            bp=run/'cst_work'/f'points_{i}_bwd_5GHz/Efield.points.txt'
            assert np.array_equal(np.loadtxt(fp),np.loadtxt(bp))
        rows.append(row)
    report['arms'][mode]={'summary':summary,'history':history,'rows':rows}

unit,objective=[configs[m] for m in ('unit','objective')]
for conf in (unit,objective):
    conf['config']['output']['dir']='same-output-for-comparison'
assert unit['config']==objective['config'] and unit['setup']==objective['setup']
assert read(root/'unit/iter_000/shape.json')==read(root/'objective/iter_000/shape.json')
for name in ('phi','x','y'):
    assert np.array_equal(np.load(root/'unit/iter_000/levelset.npz')[name],np.load(root/'objective/iter_000/levelset.npz')[name])
for tag in ('fwd','bwd'):
    meshes=[(root/m/f'iter_000/cst_{tag}_Model.msh').read_bytes() for m in ('unit','objective')]
    assert meshes[0]==meshes[1]
    report[f'initial_{tag}_mesh_sha256']=hashlib.sha256(meshes[0]).hexdigest()
report['initial_point_field_relative_differences']={}
for tag in ('fwd','bwd'):
    for kind in ('Efield','Hfield'):
        tables=[np.loadtxt(root/m/'cst_work'/f'points_0_{tag}_5GHz'/f'{kind}.txt',skiprows=2)
                for m in ('unit','objective')]
        assert np.array_equal(tables[0][:,:3],tables[1][:,:3])
        fields=[t[:,3::2]+1j*t[:,4::2] for t in tables]
        report['initial_point_field_relative_differences'][f'{tag}_{kind}']=float(
            np.linalg.norm(fields[1]-fields[0])/np.linalg.norm(fields[0]))
f0=[report['arms'][m]['rows'][0]['fom_abs'] for m in ('unit','objective')]
assert np.isclose(*f0,rtol=1e-5,atol=1e-8)
report['equal_initial_geometry_mesh_and_settings']=True
report['config_diff_limited_to_source_and_output']=True
report['final_objective_minus_unit_db']={key:report['arms']['objective']['rows'][-1]['s_db'][key]-report['arms']['unit']['rows'][-1]['s_db'][key] for key in ('S11','S21','S31','S41')}
(root/'analysis.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')

fig,axs=plt.subplots(2,2,figsize=(10,7))
for ax,key,title in zip(axs.flat,('S31','S11','S21','D'),('Target coupling S31','Reflection S11','Through S21','Directivity S31-S41')):
    for mode,label in [('unit','Unit reverse field'),('objective','Experimental complex factor')]:
        rows=report['arms'][mode]['rows']
        values=[r['directivity_db'] if key=='D' else r['s_db'][key] for r in rows]
        ax.plot(range(3),values,'o-',label=label)
    ax.set_title(title);ax.set_xticks(range(3));ax.set_xlabel('Committed updates');ax.set_ylabel('dB');ax.grid(alpha=.25)
axs[0,0].legend(fontsize=9)
fig.suptitle('Coupler: same point sampling, geometry constraints and fixed dt=1e-8')
fig.tight_layout();fig.savefig(root/'comparison.png',dpi=180);plt.close(fig)
print(json.dumps({'native_solves':report['successful_native_solves'],
    'rows':{m:report['arms'][m]['rows'] for m in report['arms']},
    'difference':report['final_objective_minus_unit_db']},indent=2))
