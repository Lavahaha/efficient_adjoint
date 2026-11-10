"""Replay saved antenna velocities and optionally solve independent CST controls."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.geometry.design import compose_metal_regions, connection_boundary_mask, region_rings
from eaopt.geometry.levelset import LevelSet2D, make_level_set
from eaopt.optimize.constraints import apply_min_feature, apply_min_gap
from eaopt.runner import CstAdjointLevelSetProblem, _movement_constraints
from eaopt.solver.case import load_case

SOURCE = ROOT / 'results/antenna_oscillation_2026-11-10/local_diagnostics/stages'
OUT = ROOT / 'results/antenna_geometry_fix_2026-11-10'
CFG = CaseConfig.from_yaml(ROOT / 'configs/antenna.yaml')
SETUP, MODEL = load_case(CFG)
INITIAL = make_level_set(CFG)
GRID = als.Grid2D(INITIAL.nx, INITIAL.ny, INITIAL.dx, INITIAL.xs[0], INITIAL.ys[0])
MASK, FM, FA, ANCHOR = _movement_constraints(INITIAL, CFG)
PINNED = connection_boundary_mask(INITIAL, CFG, MODEL.fixed_metal_polygons())
MASK[PINNED] = 0.


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def memory():
    class Status(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in ('total', 'available', 'page_total',
                'page_available', 'virtual_total', 'virtual_available', 'extended')]
    value = Status()
    value.length = ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise ctypes.WinError()
    return {'available_mb': value.available / 2**20, 'total_mb': value.total / 2**20}


def state(index):
    with np.load(SOURCE / f'iter_{index:03d}_velocity.npz') as data:
        return {key: data[key].copy() for key in data.files}


def regions(phi):
    return compose_metal_regions(CFG, phi)


def shape(phi):
    return unary_union([Polygon(r.outer, r.holes) for r in regions(phi)])


def stages(phi, velocity, displacement):
    ls = als.LevelSet(GRID, phi)
    dt = displacement / abs(velocity).max()
    ls.phi = als.advect_levelset(ls.phi, velocity, dt, GRID.dx, cfl=.5)
    result = {'advection': ls.phi.copy()}
    als.apply_constraints(ls, FM, FA, ANCHOR)
    result['constraints'] = ls.phi.copy()
    candidate = LevelSet2D(INITIAL.box, INITIAL.dx)
    candidate.phi = ls.phi.copy()
    candidate.phi[PINNED] = INITIAL.phi[PINNED]
    apply_min_gap(candidate, CFG)
    apply_min_feature(candidate, CFG.constraints.min_feature_mm, force_air=FA, force_metal=FM)
    candidate.phi[PINNED] = INITIAL.phi[PINNED]
    ls.phi = candidate.phi.copy()
    ls.reinit()
    ls.phi[PINNED] = INITIAL.phi[PINNED]
    result['full'] = ls.phi.copy()
    return result


def geometry_delta(a, b):
    return {'hausdorff_mm': a.boundary.hausdorff_distance(b.boundary),
            'symdiff_mm2': a.symmetric_difference(b).area}


def offline():
    OUT.mkdir(parents=True, exist_ok=True)
    records = []
    for index in (0, 1, 2):
        data = state(index)
        base = shape(data['phi'])
        first_zero = stages(data['phi'], data['velocity'], 0.)['full']
        first_delta = geometry_delta(shape(first_zero), base)
        if index in (0, 2):
            assert first_delta['hausdorff_mm'] < 1e-9, first_delta
        repeated = first_zero.copy()
        for _ in range(20):
            repeated = stages(repeated, data['velocity'], 0.)['full']
        zero = geometry_delta(shape(repeated), shape(first_zero))
        assert zero['hausdorff_mm'] < 1e-9, zero
        records.append({'state': index, 'first_constraint_projection': first_delta,
                        'zero_update_repetitions_after_first_projection': 20, **zero})
        for step in (.01, .05):
            chain = stages(data['phi'], data['velocity'], step)
            drift = geometry_delta(shape(chain['full']), shape(chain['advection']))
            # State 0/1 candidates can require actual feature/contact repairs.
            if index == 2:
                assert drift['hausdorff_mm'] < 1e-9, drift
            records.append({'state': index, 'requested_displacement_mm': step,
                            'postprocessing_drift': drift,
                            'from_base': geometry_delta(shape(chain['full']), base),
                            'vertices': sum(len(r) for r in region_rings(regions(chain['full'])))})
        if index == 2:
            np.savez_compressed(OUT / 'fixed_stage_controls.npz', base=data['phi'],
                                **stages(data['phi'], data['velocity'], .05))
            scan = []
            previous = None
            for step in np.linspace(-.06, .06, 25):
                phi = als.advect_levelset(data['phi'], data['velocity'],
                    step / abs(data['velocity']).max(), GRID.dx, cfl=.5)
                current = shape(phi)
                # 1e-9 mm simplification removes numerical collinearity only.
                from skimage.measure import find_contours
                from shapely.geometry import LineString
                contour = unary_union([LineString(np.column_stack((
                    GRID.x0 + c[:, 0] * GRID.dx, GRID.y0 + c[:, 1] * GRID.dx)))
                    for c in find_contours(phi, 0.)])
                # Remove the design-box closure: only compare extracted free edges.
                raw_error = max(point_distance(current.boundary, p)
                                for line in ([contour] if contour.geom_type == 'LineString' else contour.geoms)
                                for p in line.coords)
                assert raw_error < 2e-9, raw_error
                scan.append({'step_mm': float(step), 'raw_to_cad_max_vertex_distance_mm': raw_error,
                    'cad_move_previous_mm': (geometry_delta(current, previous)['hausdorff_mm']
                                             if previous is not None else None)})
                previous = current
    corner_scan = []
    initial_shape = shape(INITIAL.phi)
    for inward in (1e-2, 1e-3, 1e-4, 1e-5, 1e-6):
        actual = shape(INITIAL.phi + inward)
        expected = initial_shape.buffer(-inward)
        corner_scan.append({'inward_shift_mm': inward,
            'cad_from_initial': geometry_delta(actual, initial_shape),
            'error_against_exact_rectangle_erosion': geometry_delta(actual, expected),
            'area_directional_quotient_mm': (actual.area-initial_shape.area)/inward,
            'exact_area_directional_quotient_mm': (expected.area-initial_shape.area)/inward})
    report = {'zero_and_postprocessing_checks': records, 'scan': scan,
              'initial_corner_inward_scan': corner_scan,
              'production_hashes': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                for p in ('adjoint_levelset.py', 'eaopt/geometry/design.py',
                          'eaopt/runner.py', 'eaopt/solver/cst_model.py',
                          'eaopt/optimize/constraints.py', 'eaopt/solver/cst.py')},
              'step_control_modified': False}
    save(OUT / 'offline.json', report)
    print(json.dumps(report, indent=2), flush=True)


def point_distance(line, coordinate):
    from shapely.geometry import Point
    return line.distance(Point(coordinate))


def rectangle(h=0., direction='length', subdivided=False):
    xy = np.array([[-20., -10.], [0., -10.], [0., 10.], [-20., 10.], [-20., -10.]])
    if subdivided:
        xy = np.concatenate([a + (b-a)*np.linspace(0, 1, 21, endpoint=False)[:, None]
                             for a, b in zip(xy[:-1], xy[1:])])
        xy = np.vstack((xy, xy[0]))
    return [xy + h * displacement(xy, direction)]


def displacement(xy, direction):
    x, y = xy.T
    ramp = (x + 20.) / 20.
    if direction == 'length':
        return np.column_stack((ramp, np.zeros(len(x))))
    if direction == 'width':
        return np.column_stack((np.zeros(len(x)), ramp*y/10.))
    return np.column_stack((ramp*(1-(y/10.)**2), np.zeros(len(x))))


def live(mode, phase_error=None, de_pid=None, local_mesh_mm=None):
    from eaopt.solver import cst_model, cst_results
    from eaopt.solver.cst import CstSolver, _run_solver, _save
    from eaopt.cli import make_log, safe_console
    safe_console()
    dest = OUT / (mode + (f'_phase{phase_error:g}' if phase_error else '') +
                  (f'_local{local_mesh_mm:g}' if local_mesh_mm else ''))
    dest.mkdir(parents=True, exist_ok=False)
    result = {'evaluations': [], 'memory_before': memory(), 'step_control_modified': False}
    log = make_log(dest / 'run.log')
    original = cst_model.frequency_domain_solver_setup
    cst_model.frequency_domain_solver_setup = lambda p, f: original(p, f).replace(
        '.MaxCPUs "2"', '.MaxCPUs "1"')
    solver = CstSolver(CFG, setup=replace(SETUP, solver_type='fd'), model=MODEL,
                       auto_init=True, force_new=True, point_sampling=True, log=log)
    solver.de = None

    def evaluate(name, polys, sample=False):
        solver.close()
        CFG.output.dir = str(dest / name)
        index = len(result['evaluations'])
        solver.begin_iteration(index)
        solver.build_model(polys, None)
        project = solver.projects['fwd']
        if phase_error:
            project.model3d.add_to_history('diagnostic automatic phase-error refinement',
                'With MeshSettings\n .SetMeshType "Tet"\n' +
                f' .Set "PhaseErrorNear", "{phase_error:g}"\n' +
                f' .Set "PhaseErrorFar", "{phase_error:g}"\nEnd With\n')
        if local_mesh_mm:
            project.model3d.add_to_history('diagnostic patch local tetrahedral size',
                f'Solid.SetMeshStepwidthTet "design_region:antenna_metal_0", "{local_mesh_mm:g}"\n')
        path = solver.project_path('fwd')
        result['memory_before_solve'] = memory()
        save(dest / 'results.json', result)
        try:
            seconds = _run_solver(project.model3d, log)
        finally:
            model_log = path.with_suffix('') / 'Result/Model.log'
            if model_log.exists():
                shutil.copy2(model_log, dest / f'{name}_Model.log')
        _save(project, path, log)
        s = cst_results.read_s_params(path, 1, response_ports=(1,), freq_ghz=7.5,
                                     log=log, exact_frequency=True)[(1, 1)]
        text = model_log.read_text(encoding='utf-8', errors='replace')
        cells = re.findall(r'Number of mesh cells\s*:\s*(\d+)', text)
        row = {'name': name, 's_real': s.real, 's_imag': s.imag, 'magnitude': abs(s),
               'mesh_cells': int(cells[-1]) if cells else None,
               'solver_seconds': seconds, 'vertices': sum(map(len, polys)),
               'project_path': str(path)}
        result['evaluations'].append(row)
        save(dest / 'results.json', result)
        log('[geometry fix] ' + json.dumps(row))
        if sample:
            solver._solved_at['fwd'] = index
            problem = object.__new__(CstAdjointLevelSetProblem)
            fwd = type('Forward', (), {'extra': {'cad_polygons': solver.last_prepared_polys}})()
            bnd = problem.boundary(als.LevelSet(GRID, INITIAL.phi), fwd, MASK)
            # The contact is fixed even when a midpoint is close to its endpoint.
            bnd.active &= ~((abs(bnd.pts[:, 0] + 20.) < 1e-9) & (abs(bnd.pts[:, 1]) <= 2.5))
            selected = als.Boundary(bnd.pts[bnd.active], bnd.normals[bnd.active],
                                   bnd.weights[bnd.active], np.ones(bnd.active.sum(), bool))
            predictions = []
            for offset in (.025, .05, .1):
                xyz = np.column_stack((selected.pts + offset*selected.normals,
                                       np.full(len(selected.pts), .009)))
                e, h = solver.sample_fields(xyz, tag='fwd', freq_ghz=7.5)
                np.savez_compressed(dest / f'fields_offset{offset:g}.npz', points=xyz,
                    E=e, H=h, boundary=selected.pts, normals=selected.normals,
                    weights=selected.weights, S=s)
                ep, hp = als._boundary_field_products(selected, e, h, e, h)
                for direction in ('length', 'width', 'local'):
                    vn = np.sum(displacement(selected.pts, direction)*selected.normals, axis=1)
                    q = -1j*2*np.pi*7.5e9*np.sum((als.EPS0*ep+als.MU0*hp)*vn*selected.weights)
                    predictions.append({'direction': direction, 'offset_mm': offset,
                        'complex_slope': [q.real, q.imag],
                        'objective_slope': float(np.real(np.conj(s)/abs(s)*q))})
            result['predictions'] = predictions
            save(dest / 'results.json', result)
        return s

    try:
        import cst.interface
        solver.de = (cst.interface.DesignEnvironment.connect(de_pid) if de_pid is not None
                     else cst.interface.DesignEnvironment.new(options=['--hide']))
        solver.de.set_quiet_mode(True)
        result['environment_pid'] = solver.de.pid()
        result['owns_environment'] = de_pid is None
        save(dest / 'results.json', result)
        if mode == 'stages':
            data = state(2)
            chain = stages(data['phi'], data['velocity'], .05)
            zero = stages(data['phi'], data['velocity'], 0.)
            for name, phi in [('base', data['phi']), ('zero_full', zero['full']),
                              ('advection', chain['advection']), ('full', chain['full'])]:
                evaluate(name, region_rings(regions(phi)))
        else:
            base = evaluate('rectangle_base', rectangle(subdivided=True), sample=True)
            for direction in ('length', 'width', 'local'):
                for h in (.08, .04, .02):
                    plus = evaluate(f'{direction}_plus{h:g}', rectangle(h, direction, True))
                    minus = evaluate(f'{direction}_minus{h:g}', rectangle(-h, direction, True))
                    fd = (plus-minus)/(2*h)
                    row = {'direction': direction, 'h_mm': h, 'complex_fd': [fd.real, fd.imag],
                           'magnitude_fd': (abs(plus)-abs(minus))/(2*h),
                           'chain_fd': float(np.real(np.conj(base)/abs(base)*fd))}
                    result.setdefault('finite_differences', []).append(row)
                    save(dest / 'results.json', result)
            evaluate('rectangle_four_edges', rectangle())
        result['completed'] = True
    except Exception as error:
        result['completed'] = False
        result['error'] = repr(error)
        raise
    finally:
        cst_model.frequency_domain_solver_setup = original
        result['memory_after'] = memory()
        save(dest / 'results.json', result)
        try:
            solver.close()
            if de_pid is None and solver.de is not None and not solver.de.list_open_projects():
                solver.de.close()
        except Exception as error:
            result['cleanup_error'] = repr(error)
            save(dest / 'results.json', result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', choices=('stages', 'derivative'))
    parser.add_argument('--phase-error', type=float)
    parser.add_argument('--local-mesh-mm', type=float)
    parser.add_argument('--de-pid', type=int)
    args = parser.parse_args()
    for name in ('phase_error', 'local_mesh_mm'):
        value = getattr(args, name)
        if value is not None and (not np.isfinite(value) or value <= 0):
            parser.error(f'--{name.replace("_", "-")} must be finite and positive')
    if args.de_pid is not None and args.de_pid <= 0:
        parser.error('--de-pid must be a positive process ID')
    if args.live:
        live(args.live, args.phase_error, args.de_pid, args.local_mesh_mm)
    else:
        offline()
