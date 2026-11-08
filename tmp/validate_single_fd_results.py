"""Validate archived native CST runs; no solver or field values are simulated."""
from pathlib import Path
import hashlib
import json
import sys

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from eaopt.geometry.contour import classify_metal_regions
from eaopt.runner import boundary_sample_points
from types import SimpleNamespace

ROOT = Path('results/fd_single_2026-11-02')
rows = []
for name in ('coupler_smoke', 'divider_smoke', 'antenna_smoke', 'coupler_planar_smoke'):
    folder = ROOT / name
    summary = json.loads((folder/'adjoint_levelset_summary.json').read_text())
    history = [json.loads(line) for line in (folder/'adjoint_levelset_history.jsonl').read_text().splitlines()]
    assert summary['solver_type'] == 'fd' and summary['boundary_sampling'] == 'cst-points'
    assert summary['adjoint_source'] == 'unit' and summary['iterations_recorded'] == 1
    assert len(history) == 1 and history[0]['updated'] and history[0]['backtracks'] == 0
    assert history[0]['pseudo_time_step_used'] == history[0]['pseudo_time_step_next'] == 1e-8
    cfg = CaseConfig.from_yaml(f"configs/{summary['case']}.yaml")
    setup, model = load_case(cfg)
    fields = np.load(folder/'iter_000/boundary_fields.npz')
    indices = fields['boundary_indices']
    assert np.array_equal(indices, np.flatnonzero(fields['active']))
    assert np.allclose(fields['points_mm'][:,:2], fields['boundary'][indices]+fields['offset_mm']*fields['normals'][indices])
    assert np.all(fields['points_mm'][:,2] == cfg.sampling.field_z_mm)
    for key in ('E_fwd', 'H_fwd', 'E_bwd', 'H_bwd'):
        assert fields[key].shape == (3, len(indices)) and np.isfinite(fields[key]).all()
    shape = json.loads((folder/'iter_000/shape.json').read_text())
    regions = classify_metal_regions([np.asarray(p) for p in shape['polygons']])
    metal = unary_union([Polygon(r.outer,r.holes) for r in regions]+[Polygon(p) for p in model.fixed_metal_polygons()])
    substrate = unary_union([Polygon(p) for kind,p,_ in model.layout_view()['shapes'] if kind=='substrate'])
    boundary = SimpleNamespace(pts=fields['boundary'], normals=fields['normals'], active=fields['active'])
    # Use the public sampling helper against the exact saved CAD and sample points.
    points, eps, checked_indices = boundary_sample_points(boundary,float(fields['offset_mm']),
        float(fields['z_mm']),setup,metal,substrate,model.AIR_H)
    assert np.array_equal(checked_indices,indices)
    assert np.array_equal(points,fields['points_mm']) and eps==float(fields['eps_f_per_m'])
    solves = 0
    for iteration in (0,1):
        tags = json.loads((folder/f'iter_{iteration:03d}/meta.json').read_text())['tags']
        solves += len(tags)
        for tag,meta in tags.items():
            assert meta['pin_w'] == .5 and meta['solver_type'] == 'fd'
            engine = (folder/f'cst_work/solver_{tag}.txt').read_text(encoding='utf-8-sig').splitlines()
            assert engine[0].strip() == 'HF Frequency Domain'
            assert engine[1].strip() == str(setup.stimulus(tag,cfg.objective)) and engine[2].strip() == '1'
    reflection = summary['case'] == 'antenna'
    assert solves == (2 if reflection else 3)
    if reflection:
        assert np.array_equal(fields['E_fwd'],fields['E_bwd'])
    else:
        for mesh in ('moegeom.tet','topot.tet','bc3d.tet'):
            digests = [hashlib.sha256((folder/f'iter_000/cst_{tag}_{mesh}').read_bytes()).hexdigest() for tag in ('fwd','bwd')]
            assert digests[0] == digests[1]
    planar = name in ('antenna_smoke','coupler_planar_smoke')
    if planar:
        for iteration in (0,1):
            for kind in ('e','h'):
                grid = np.load(folder/f'iter_{iteration:03d}/fields_fwd_{kind}.npz')
                assert grid['data'].shape[2] == 1 and grid['origin'][2] == cfg.sampling.field_z_mm
    coupler_geometry = None
    if summary['case'] == 'coupler':
        fixed = [Polygon(p) for p in model.fixed_metal_polygons()]
        through,left,right = fixed
        coupler_geometry = []
        for iteration in (0,1):
            saved_shape = json.loads((folder/f'iter_{iteration:03d}/shape.json').read_text())
            saved_regions = classify_metal_regions([np.asarray(p) for p in saved_shape['polygons']])
            moving = unary_union([Polygon(r.outer,r.holes) for r in saved_regions])
            coupled = unary_union([moving,left,right])
            parts = list(coupled.geoms) if coupled.geom_type == 'MultiPolygon' else [coupled]
            connected = any(p.intersects(left) and p.intersects(right) for p in parts)
            gap = coupled.distance(through)
            coupler_geometry.append(dict(iteration=iteration,coupled_ports_connected=connected,
                right_leg_gap_mm=moving.distance(right),through_line_gap_mm=gap,
                geometry_acceptance_passed=connected and gap>=cfg.constraints.min_gap_mm-1e-9))
    rows.append(dict(run=name,case=summary['case'],native_solver_jobs=solves,
        active_points=len(indices),z_mm=float(fields['z_mm']),offset_mm=float(fields['offset_mm']),
        f_before=history[0]['f_before'],f_after=history[0]['f_after'],objective_gain=history[0]['objective_gain'],
        solver_io_checks_passed=True,planar_display_export=planar,coupler_geometry=coupler_geometry,
        paper_reproduction_passed=False))

plane_difference = abs(rows[0]['f_after']-rows[3]['f_after'])
assert plane_difference < 1e-5
old = np.load(ROOT/'coupler_smoke/phi_final.npz')
new = np.load(ROOT/'coupler_planar_smoke/phi_final.npz')
phi_difference = float(np.max(np.abs(old['phi']-new['phi'])))
geometries = [unary_union([Polygon(p) for p in json.loads((ROOT/name/'final_shape.json').read_text())['polygons']])
              for name in ('coupler_smoke','coupler_planar_smoke')]
hausdorff_mm = geometries[0].hausdorff_distance(geometries[1])
# Reinitialization changes the level-set values away from its zero contour.
# Compare physical CAD at 1/1000 of the 0.1 mm design grid instead.
assert hausdorff_mm < 1e-4
mesh = json.loads((ROOT/'coupler_mesh_sampling/analysis.json').read_text())
delta_s = next(v['y'][-1][0] for k,v in mesh['adaptation_curves'].items() if 'Delta' in k)
result = dict(status='paper_reproduction_failed',solver_io_checks_passed=True,runs=rows,
    planar_export_control=dict(final_fom_absolute_difference=plane_difference,final_phi_max_difference=phi_difference,
                               final_cad_hausdorff_mm=hausdorff_mm),
    mesh_check=dict(maximum_passes=3,target_delta_s=.001,final_delta_s=delta_s,
        spatial_convergence_passed=delta_s<.001,
        same_point_field_change_from_coarse=mesh['same_point_field_change_from_coarse']))
(ROOT/'validation_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
