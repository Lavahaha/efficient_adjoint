"""Compare cached HJ direction work with committed geometry and FoM changes."""
import json
import sys
from pathlib import Path
import numpy as np
from scipy.interpolate import RegularGridInterpolator
from shapely import distance,points,contains_xy
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import make_level_set,LevelSet2D
from eaopt.geometry.design import compose_metal_regions,connection_boundary_mask
from eaopt.geometry.contour import classify_metal_regions
from eaopt.runner import _movement_constraints,_project_case_geometry
from eaopt.solver.case import load_case


def main(directory):
    directory=Path(directory)
    frozen=json.loads((directory/'run_config.json').read_text())
    cfg=CaseConfig.from_dict(frozen['config'])
    if cfg.name!='antenna' or frozen['arguments']['geometry_update']!='levelset':
        raise ValueError('This diagnostic currently supports the antenna HJ path')
    initial=make_level_set(cfg)
    setup,model=load_case(cfg)
    grid=als.Grid2D(initial.nx,initial.ny,initial.dx,initial.xs[0],initial.ys[0])
    mask,force_metal,force_air,anchor=_movement_constraints(initial,cfg)
    pinned=connection_boundary_mask(initial,cfg,model.fixed_metal_polygons())
    mask[pinned]=0.
    history=[json.loads(s) for s in (directory/'adjoint_levelset_history.jsonl').read_text().splitlines()]
    report=dict(scope='raw signed direction work; not SI-normalized FoM prediction; signed-distance displacement is only a first-order diagnostic',steps=[])
    for row in history:
        it=row['it']; before=row['eval_id_before']; after=row['eval_id_after']
        source=directory/f'iter_{before:03d}'
        data=np.load(source/'boundary_fields.npz')
        snapshot=np.load(directory/f'phi_before_iter_{it:03d}.npz')
        ls=als.LevelSet(grid,snapshot['phi'])
        boundary=als.Boundary(data['boundary'],data['normals'],data['weights'],data['active'])
        ids=data['boundary_indices']
        selected=als.Boundary(boundary.pts[ids],boundary.normals[ids],boundary.weights[ids],np.ones(len(ids),bool))
        meta=json.loads((source/'meta.json').read_text())['tags']['fwd']
        s=complex(*meta['s_params']['S1,1'])
        omega=2*np.pi*frozen['setup']['frequency_ghz']*1e9
        amplitude=-1j*omega*np.conj(s)/abs(s)
        signed=np.zeros(len(boundary.pts))
        signed[ids]=-als.shape_derivative_mine(selected,data['E_fwd'],data['H_fwd'],
             amplitude*data['E_bwd'],amplitude*data['H_bwd'],omega,float(data['eps_f_per_m']),als.MU0)
        signed*=frozen['arguments']['prefactor_scale']*cfg.optimizer.velocity_sign
        velocity=als.boundary_to_grid_velocity(ls,boundary,signed,mask,
            cfg.level_set.extension_band_mm/initial.dx,smooth_sigma_cells=1.5)
        dt=row['pseudo_time_step_used']
        np.testing.assert_allclose(dt*abs(velocity).max(),row['predicted_displacement'],rtol=1e-10,atol=1e-12)
        sampled=RegularGridInterpolator((grid.x,grid.y),velocity)(boundary.pts)
        moved=als.advect_levelset(ls.phi,velocity,dt,grid.dx,cfl=cfg.optimizer.cfl)
        raw=unary_union([Polygon(r.outer,r.holes) for r in compose_metal_regions(cfg,moved)])
        shape=json.loads((directory/f'iter_{after:03d}'/'shape.json').read_text())
        final=unary_union([Polygon(r.outer,r.holes) for r in classify_metal_regions(shape['polygons'])])
        def work(geometry):
            d=distance(geometry.boundary,points(boundary.pts))
            normal_displacement=np.where(contains_xy(geometry,boundary.pts[:,0],boundary.pts[:,1]),d,-d)
            return float(np.sum(boundary.weights*signed*normal_displacement))
        local=[]
        grid_rate=float(np.sum(boundary.weights*signed*sampled))
        for fraction in (1e-3,1e-4,1e-5):
            diagnostic_dt=grid.dx*fraction/abs(velocity).max()
            candidate=ls.copy()
            candidate.phi=als.advect_levelset(candidate.phi,velocity,diagnostic_dt,grid.dx,cfl=cfg.optimizer.cfl)
            small_raw=unary_union([Polygon(r.outer,r.holes) for r in compose_metal_regions(cfg,candidate.phi)])
            als.apply_constraints(candidate,force_metal,force_air,anchor)
            _project_case_geometry(candidate,LevelSet2D(cfg.design_region.box,initial.dx),cfg,initial.phi,
                pinned,force_air,force_metal,None,mask,lambda _:None)
            small_final=unary_union([Polygon(r.outer,r.holes) for r in compose_metal_regions(cfg,candidate.phi)])
            local.append(dict(maximum_grid_displacement_mm=grid.dx*fraction,
                raw_work_ratio=work(small_raw)/(diagnostic_dt*grid_rate),
                final_work_ratio=work(small_final)/(diagnostic_dt*grid_rate),
                projection_change_mm2=small_raw.symmetric_difference(small_final).area))
        report['steps'].append(dict(iteration=it,actual_objective_gain=row['objective_gain'],
            predicted_displacement_mm=row['predicted_displacement'],
            grid_velocity_signed_work=float(dt*np.sum(boundary.weights*signed*sampled)),
            raw_hj_geometry_signed_work=work(raw),final_geometry_signed_work=work(final),
            projection_change_area_mm2=raw.symmetric_difference(final).area,
            local_geometry_direction_scan=local))
    (directory/'direction_work_diagnostic.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__': main(sys.argv[1])
