"""Offline gates for |S_mn|, geometry consistency and velocity directions."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from shapely.affinity import scale
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from eaopt.config import BoxSpec, CaseConfig, PolygonSpec
from eaopt.geometry.design import compose_metal_regions
from eaopt.geometry.levelset import make_level_set
from check_cad_normal_transport import cases

OUT = ROOT/'results/algorithm_validation_2026-11-13'


def write(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def geometry_gate():
    rows = []
    shapes = cases()
    shapes['two_components'] = unary_union([Polygon([[-3, -1], [-1, -1], [-1, 1], [-3, 1]]),
                                           Polygon([[1, -1], [3, -1], [3, 1], [1, 1]])])
    for name, original in shapes.items():
        if original.bounds[2]-original.bounds[0] > 8:
            original = scale(original, .3, .3, origin=(0, 0))
        cfg = copy.deepcopy(CaseConfig.from_yaml(ROOT/'configs/antenna.yaml'))
        cfg.design_region.box = BoxSpec(x=(-8., 8.), y=(-8., 8.))
        cfg.design_region.grid_step_mm = .2
        if original.geom_type == 'MultiPolygon':
            pieces = list(original.geoms)
        elif original.interiors:
            # Decompose a frame into valid solid initial polygons, as the config
            # format describes unions rather than interpreting holes as metal.
            from shapely.ops import triangulate
            pieces = [p.intersection(original) for p in triangulate(original) if original.covers(p)]
        else:
            pieces = [original]
        cfg.initial_metal = [PolygonSpec(vertices=np.asarray(p.exterior.coords).tolist()) for p in pieces]
        initial = make_level_set(cfg)
        baseline = unary_union([Polygon(r.outer, r.holes) for r in compose_metal_regions(cfg, initial.phi)])
        np.testing.assert_allclose(baseline.area, original.area, atol=1e-12)
        scan = []
        for h in (0., .04, -.04, .01, -.01, .001, -.001, 1e-6, -1e-6):
            try:
                actual = unary_union([Polygon(r.outer, r.holes) for r in compose_metal_regions(cfg, initial.phi-h)])
                expected = original.buffer(h, quad_segs=64) if h else original
                scan.append(dict(h_mm=h, hausdorff_mm=actual.boundary.hausdorff_distance(expected.boundary),
                                 symmetric_difference_mm2=actual.symmetric_difference(expected).area,
                                 area_mm2=actual.area, area_quotient_mm=(actual.area-original.area)/h if h else None,
                                 expected_area_quotient_mm=(expected.area-original.area)/h if h else None,
                                 components=len(actual.geoms) if actual.geom_type == 'MultiPolygon' else 1,
                                 holes=sum(len(p.interiors) for p in actual.geoms) if actual.geom_type == 'MultiPolygon' else len(actual.interiors)))
            except ValueError as error:
                scan.append(dict(h_mm=h, error=str(error)))
        small = [r for r in scan if abs(r['h_mm']) == 1e-6]
        rows.append(dict(case=name, perimeter_mm=original.length, scan=scan,
                         smallest_step_continuity_passed=all('error' not in r and r['hausdorff_mm'] < 1e-3 for r in small)))
    return dict(passed=all(r['smallest_step_continuity_passed'] for r in rows), cases=rows,
                scope='Exact initial CAD and phi-offset -> CAD; fixed grid; not a test of the electromagnetic solver')


def velocity_gate():
    theta = np.sort(np.random.default_rng(0).uniform(0, 2*np.pi, 48))
    points = 1.5*np.column_stack((np.cos(theta), np.sin(theta)))
    gaps = np.diff(np.r_[theta, theta[0]+2*np.pi])
    weights = .75*(gaps+np.roll(gaps, 1))
    grid = als.Grid2D(61, 61, .1, -3., -3.)
    x, y = grid.mesh()
    ls = als.LevelSet(grid, np.hypot(x, y)-1.5)
    boundary = als.Boundary(points, points/1.5, weights, np.ones(48, bool))
    def work(g, velocity):
        sampled = RegularGridInterpolator((grid.x, grid.y), velocity)(boundary.pts)
        return float(np.sum(boundary.weights*g*sampled))
    first, second = np.zeros(48), np.zeros(48)
    first[35], second[36] = .85, .5
    vfirst = als.boundary_to_grid_velocity(ls, boundary, first)
    vsecond = als.boundary_to_grid_velocity(ls, boundary, second)
    # Nonnegative inputs cannot trigger the repair. Their linear combination
    # reproduces the original operator without storing a second implementation.
    old = vfirst-vsecond
    actual = als.boundary_to_grid_velocity(ls, boundary, first-second)
    combined = als.boundary_to_grid_velocity(ls, boundary, first+second)
    compatibility_error = float(np.max(abs(combined-vfirst-vsecond)))
    unchanged = compatibility_error < 1e-14
    np.savez_compressed(OUT/'velocity_reproducer.npz',points=points,weights=weights,gradient=first-second,
                        old_velocity=old,repaired_velocity=actual)
    return dict(passed=work(first-second, old) < 0 and work(first-second, actual) > 0 and unchanged,
                raw_work=float(np.sum(boundary.weights*(first-second)**2)),
                old_work=work(first-second, old), repaired_work=work(first-second, actual),
                valid_direction_unchanged_within_roundoff=unchanged,
                valid_direction_linearity_max_error=compatibility_error,
                scope='velocity extension only; HJ and constraint stages remain unverified')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    analytical = json.loads((ROOT/'results/pec_cylinder_smatrix_2026-11-12/results.json').read_text())
    geometry = geometry_gate()
    velocity = velocity_gate()
    abs_error = max(c['perturbations'][-1]['objective_fd_relative_errors']['abs'] for c in analytical['cases'])
    g1 = analytical['max_entry_relative_error'] < 1e-10 and abs_error < 1e-5
    report = dict(checks_completed=True, required_objective='abs(S_mn)', excluded_acceptance_metrics=['real(S)', 'imag(S)', 'abs(S)^2', 'dB(S)'],
                  stage1=dict(passed=g1, max_entry_complex_derivative_error=analytical['max_entry_relative_error'],
                              max_abs_fd_relative_error=abs_error,
                              matrix_entries_checked=sum(len(c['S'])**2 for c in analytical['cases']),
                              scope='Exact Maxwell reference; the full production geometry/field adapter is not certified'),
                  stage2=geometry, stage3=velocity,
                  stage4=dict(status='not_accepted', reason='geometry first-order consistency gate has not passed'),
                  stage5=dict(status='not_started', reason='prioritise algorithm; no CST runs in this round'),
                  production_step_controls_changed=False, full_goal_achieved=False,
                  source_hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                                 ('adjoint_levelset.py','eaopt/runner.py','eaopt/geometry/design.py','configs/antenna.yaml','configs/coupler.yaml','configs/divider.yaml')})
    write(OUT/'results.json', report)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3))
    for case in geometry['cases'][:4]:
        scan = [r for r in case['scan'] if r['h_mm'] < 0 and 'error' not in r]
        axes[0].loglog([-r['h_mm'] for r in scan], [r['hausdorff_mm'] for r in scan], 'o-', label=case['case'])
    axes[0].set(xlabel='Magnitude of inward displacement (mm)', ylabel='Boundary error vs exact offset (mm)',
                title='Geometry error remains as displacement vanishes')
    axes[0].grid(True, which='both', alpha=.25)
    axes[0].legend(fontsize=8)
    axes[1].bar(['Original extension', 'Direction repair'], [velocity['old_work'], velocity['repaired_work']], color=['#c85546', '#31876f'])
    axes[1].axhline(0, color='black', linewidth=.8)
    axes[1].set(ylabel='Boundary directional derivative (ascent)', title='Same exact boundary gradient')
    axes[1].grid(True, axis='y', alpha=.25)
    fig.tight_layout()
    fig.savefig(OUT/'comparison.png', dpi=170)
    plt.close(fig)
    print(json.dumps({k:v for k,v in report.items() if k not in ('stage2', 'source_hashes')}, indent=2))
    print('Geometry cases passed:', sum(c['smallest_step_continuity_passed'] for c in geometry['cases']), '/', len(geometry['cases']))


if __name__ == '__main__':
    main()
