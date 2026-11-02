"""Compare an experimental source factor on cached real coupler fields; no EM solve."""
from pathlib import Path
import json
import sys
import numpy as np
from scipy.spatial import cKDTree
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import adjoint_levelset as a
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from run_directional_fd_checks import boundary_and_directions
from check_iteration_algorithm import load_bundle

root=Path('results/adjoint_checks_2026-11-01/coupler/fd_fine')
cfg=CaseConfig.from_yaml('configs/coupler.yaml')
frozen=json.loads((root/'base/fwd/config.json').read_text(encoding='utf-8'))
assert json.loads(json.dumps(cfg.initial_metal,default=lambda x:x.__dict__))==frozen['config']['initial_metal']
setup,_=load_case(cfg)
b,directions=boundary_and_directions(cfg)
params=json.loads((root/'base/fwd/s_params.json').read_text(encoding='utf-8'))['coefficients']
s=complex(*next(r[4:] for r in params if r[:4]==[3,1,1,1]))
factor=np.conj(s)/abs(s)
prefactor=-2j*2*np.pi*setup.frequency_ghz*1e9/setup.port_power_w
offset=setup.resolve_export_steps(cfg.field_export_step_mm)[0]
xy=b.pts+offset*b.normals
points=np.column_stack([xy,np.zeros(len(xy))])
fields=[]
for tag in ('fwd','bwd'):
    for kind in ('E','H'):
        table=np.loadtxt(root/f'base/{tag}/direct_boundary_corners_{kind}.txt',skiprows=2)
        distance,index=cKDTree(table[:,:3]).query(points)
        assert max(distance)<1e-4
        fields.append((table[index,3::2]+1j*table[index,4::2]).T)
direct=a.shape_derivative_from_fields(b,*fields,prefactor,a.EPS0*setup.eps_r)
grid=a.shape_derivative_complex(b,load_bundle(root/'base/fwd'),load_bundle(root/'base/bwd'),
                                prefactor,offset,a.EPS0*setup.eps_r)
fd=json.loads((root/'directional_checks.json').read_text(encoding='utf-8'))['rows']
previous=json.loads((root.parents[1]/'direct_boundary_checks.json').read_text(encoding='utf-8'))['coupler']
expected=next(r['directional'] for r in previous['direct_predictions'] if r['z_mm']==0)
rows=[]
for name,u in directions.items():
    dg=np.sum(direct*u*b.weights)
    gg=np.sum(grid*u*b.weights)
    assert np.isclose(dg.real,expected[name],rtol=1e-6)
    rows.append({'direction':name,'grid_Q':[gg.real,gg.imag],'native_point_Q':[dg.real,dg.imag],
        'grid_paper_ReQ':gg.real,'grid_experimental_factor':float(np.real(factor*gg)),
        'point_paper_ReQ':dg.real,'point_experimental_factor':float(np.real(factor*dg)),
        'FD':{str(r['h_mm']):r['finite_difference_per_mm'] for r in fd if r['direction']==name}})
report={'scope':'New offline recomputation from previously solved real CST fields; no new EM solve or optimization',
    'S31':[s.real,s.imag],'experimental_factor':[factor.real,factor.imag],
    'unproven_assumption':'The complex boundary kernel Q equals the complex S derivative up to a known real scale',
    'rows':rows}
out=root.parent/'objective_source_cached_audit_2026-11-02.json'
out.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
