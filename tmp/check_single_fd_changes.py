"""Offline checks of FD setup, single-point spectra, and physical sampling positions."""
from pathlib import Path
import sys
import numpy as np
from shapely.geometry import Polygon,box
from shapely.ops import unary_union
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import adjoint_levelset as a
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.solver import cst_model as m
from eaopt.solver import cst_results as r
from eaopt.geometry.levelset import make_level_set
from eaopt.geometry.design import compose_metal_regions,connection_boundary_mask
from eaopt.runner import boundary_sample_points,_movement_constraints
from unittest.mock import patch

for case in ('coupler','divider','antenna'):
    cfg=CaseConfig.from_yaml(f'configs/{case}.yaml')
    setup,model=load_case(cfg)
    code=dict(model.template_blocks('check',setup.stimulus('fwd',cfg.objective),solver_type='fd'))['Solver']
    assert '.SetMethod "Tetrahedral", "Discrete samples only"' in code
    assert f'.AddSampleInterval "{setup.frequency_ghz:g}", "{setup.frequency_ghz:g}"' in code
    assert '.Stimulation "1", "1"' in code
    old=make_level_set(cfg)
    grid=a.Grid2D(old.nx,old.ny,old.dx,old.xs[0],old.ys[0])
    ls=a.LevelSet(grid,old.phi)
    mask,*_=_movement_constraints(old,cfg)
    mask[connection_boundary_mask(old,cfg,model.fixed_metal_polygons())]=0
    boundary=a.extract_boundary(ls,mask)
    regions=compose_metal_regions(cfg,old.phi)
    metal=unary_union([Polygon(v.outer,v.holes) for v in regions]+[Polygon(v) for v in model.fixed_metal_polygons()])
    substrate=unary_union([Polygon(p) for k,p,_ in model.layout_view()['shapes'] if k=='substrate'])
    points,eps,indices=boundary_sample_points(boundary,cfg.sampling.intersection_offset_mm,
        cfg.sampling.field_z_mm,setup,metal,substrate,model.AIR_H)
    assert eps==a.EPS0 and len(indices)>0
    print('SAMPLING',case,len(indices),cfg.sampling.field_z_mm,flush=True)
    _,eps,_=boundary_sample_points(boundary,.05,-.1,setup,metal,substrate,model.AIR_H)
    assert np.isclose(eps,a.EPS0*setup.eps_r,rtol=1e-14,atol=0)
    for z,delta in [(0,.05),(-setup.substrate_h_mm,.05),(model.AIR_H,.05),(.0175,-.05)]:
        try: boundary_sample_points(boundary,delta,z,setup,metal,substrate,model.AIR_H)
        except ValueError: pass
        else: raise AssertionError((z,delta))
    # Put an actual PEC obstruction at the requested points to exercise rejection.
    try: boundary_sample_points(boundary,.05,cfg.sampling.field_z_mm,setup,substrate,substrate,model.AIR_H)
    except ValueError: pass
    else: raise AssertionError('PEC collision accepted')

cfg=CaseConfig.from_yaml('configs/divider_multifreq.yaml')
_,model=load_case(cfg)
try: model.template_blocks('check',1,frequencies_ghz=cfg.objective.frequencies_ghz,solver_type='fd')
except ValueError: pass
else: raise AssertionError('Multifrequency FD accepted')

# A pure curve fixture tests the reader; it does not represent an EM solve.
class Curve:
    def __init__(self,x,y): self.x,self.y=x,y
    def get_xdata(self): return self.x
    def get_ydata(self): return self.y
class Curves:
    def __init__(self,x,y): self.curve=Curve(x,y)
    def get_result_item(self,path): return self.curve
assert r._read_curve(Curves([5],[.3+.4j]),'test',5,[],exact_frequency=True)==.3+.4j
assert np.isclose(r._read_curve(Curves([4,6],[.1+.2j,.3+.4j]),'test',5,[]),.2+.3j)
for xs,ys,f in [([4],[1],5),([4,6],[1,1],5),([5,5],[1,1],5),([5],[complex(float('nan'),0)],5)]:
    try: r._read_curve(Curves(xs,ys),'test',f,[],exact_frequency=True)
    except RuntimeError: pass
    else: raise AssertionError('Invalid/exact-frequency-missing curve accepted')
print('Single-frequency FD offline checks passed')

# Only the list of tree paths is a fixture; no CST solve is simulated.
paths=[r'2D/3D Results\E-Field\e-field (f=5) [1]',
       r'2D/3D Results\E-Field\e-field (f=5) [3]']
with patch.object(r,'_tree_items',return_value=paths):
    assert r.resolve_field_item(None,'Efield',5.,stimulus_port=3,log=lambda *_:None)==paths[1]
    for port in (None,2):
        try:r.resolve_field_item(None,'Efield',5.,stimulus_port=port,log=lambda *_:None)
        except RuntimeError:pass
        else:raise AssertionError('Ambiguous/missing excitation accepted')
print('Field excitation selection and ambiguity guards passed')

from eaopt.solver.cst import CstSolver
cfg=CaseConfig.from_yaml('configs/coupler.yaml')
solver=CstSolver(cfg,point_sampling=True)
for tag,exception in [('fwd',RuntimeError),('unknown',ValueError)]:
    try:solver.sample_fields([[0,0,.0175]],tag=tag,freq_ghz=5)
    except exception:pass
    else:raise AssertionError('Unsolved/invalid project was queried')
assert solver.de is None
try:r.export_field_plane(None,'Efield',5.,0.,'unused.txt',cfg.design_region.box,.0175)
except ValueError:pass
else:raise AssertionError('Invalid plane step accepted')
print('Stale-solution and invalid-plane guards passed without connecting to CST')
