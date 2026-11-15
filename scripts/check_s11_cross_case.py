"""Direct-CAD S11 directional checks with fixed FEM mesh controls; no optimization."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.solver.case import load_case
from check_antenna_field_sampling import read_slim, containing_cells

OUT = ROOT / 'results/s11_cross_case_2026-11-12'


def write(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def geometry(cfg, h):
    """Piecewise linear bump: exactly the same spatial velocity at every h."""
    a, b, y = (10., 22., 1.) if cfg.name == 'divider' else (3., 9., 0.)
    x = np.linspace(a, b, 25)
    u = np.sin(np.pi*(x-a)/(b-a))**2
    u[[0, -1]] = 0
    original = orient(unary_union([Polygon(p.vertices) for p in cfg.initial_metal]), sign=1)
    vertices = np.asarray(original.exterior.coords)
    points, matched = [], 0
    for p, q in zip(vertices[:-1], vertices[1:]):
        points.append(p)
        if abs(p[1]-y) < 1e-10 and abs(q[1]-y) < 1e-10 and p[0] >= b and q[0] <= a:
            points.extend(np.column_stack((x[::-1], y+h*u[::-1])))
            matched += 1
    assert matched == 1
    points = np.vstack([points, points[0]])
    polygon = Polygon(points)
    area_slope = float(np.sum(np.diff(x)*(u[:-1]+u[1:])/2))
    assert polygon.is_valid and len(points) <= 400
    np.testing.assert_allclose(polygon.area, original.area+h*area_slope, atol=1e-10, rtol=0)
    # Gaussian nodes integrate the piecewise-linear prescribed speed exactly.
    nodes, weights = np.polynomial.legendre.leggauss(4)
    t = (nodes+1)/2
    sample_x = (x[:-1, None]+np.diff(x)[:, None]*t).ravel()
    speed = (u[:-1, None]*(1-t)+u[1:, None]*t).ravel()
    line_weights = (np.diff(x)[:, None]*weights/2).ravel()
    np.testing.assert_allclose(np.sum(speed*line_weights), area_slope, atol=1e-12)
    return points, dict(area_mm2=polygon.area, perimeter_mm=polygon.length,
        bounds=list(polygon.bounds), area_slope_mm=area_slope, y_mm=y,
        x_range_mm=[a, b]), sample_x, speed, line_weights


def native_audit(project, path, metadata, thickness):
    code = '''Sub Main
Dim ok As Boolean, x0 As Double, x1 As Double, y0 As Double, y1 As Double, z0 As Double, z1 As Double
ok = Solid.GetLooseBoundingBoxOfShape("design_region:design_0", x0, x1, y0, y1, z0, z1)
If Not ok Then Err.Raise 1, , "Missing design body"
Open "OUTPUT" For Output As #1
Print #1, Solid.GetVolume("design_region:design_0")
Print #1, Solid.GetArea("design_region:design_0")
Print #1, x0 & "|" & x1 & "|" & y0 & "|" & y1 & "|" & z0 & "|" & z1
Close #1
End Sub'''.replace('OUTPUT', str(path))
    project.model3d._execute_vba_code(code)
    lines = path.read_text().splitlines()
    values = dict(volume_mm3=float(lines[0]), surface_area_mm2=float(lines[1]),
                  bbox=[float(v) for v in lines[2].split('|')])
    x0, y0, x1, y1 = metadata['bounds']
    np.testing.assert_allclose(values['bbox'], [x0, x1, y0, y1, 0, thickness], atol=1e-9, rtol=0)
    np.testing.assert_allclose(values['volume_mm3'], metadata['area_mm2']*thickness, atol=1e-8, rtol=0)
    np.testing.assert_allclose(values['surface_area_mm2'],
        2*metadata['area_mm2']+metadata['perimeter_mm']*thickness, atol=1e-7, rtol=0)
    return values


def sample_predictions(project, path, folder, cfg, setup, s, pin, mesh):
    from eaopt.solver import cst_results as R
    _, meta, x, speed, w = geometry(cfg, 0)
    offsets = [.05, .025, .0125]
    nodes, weights = np.polynomial.legendre.leggauss(3)
    z = (nodes+1)*setup.metal_thickness_mm/2
    wz = weights*setup.metal_thickness_mm/2
    n = len(x)
    points = np.concatenate([np.column_stack((x, np.full(n, meta['y_mm']+delta), np.full(n, height)))
        for delta in offsets for height in z])
    e, magnetic = [R.export_field_points(project.model3d, kind, setup.frequency_ghz,
        points, folder/f'{kind}.txt', stimulus_port=1).T for kind in ('Efield', 'Hfield')]
    xyz, cells, _ = mesh
    found, _, quality, _ = containing_cells(xyz, cells, points)
    assert np.all(found >= 0), 'A field sample is outside the linear mesh hull'
    normal = np.tile([0., 1.], (len(points), 1))
    boundary = als.Boundary(points[:, :2], normal, np.tile(w, len(offsets)*3), np.ones(len(points), bool))
    ep, hp = als._boundary_field_products(boundary, e, magnetic, e, magnetic)
    ke, kh = [v.reshape(len(offsets), 3, n) for v in (als.EPS0*ep, als.MU0*hp)]
    omega = 2*np.pi*setup.frequency_ghz*1e9
    source = -1j*omega*np.conj(s)/abs(s)
    raw = als.shape_derivative_mine(boundary, e, magnetic, source*e, source*magnetic, omega)
    np.testing.assert_allclose(raw, np.real(source*(als.EPS0*ep+als.MU0*hp)), atol=1e-7, rtol=1e-12)
    rows = []
    for i, delta in enumerate(offsets):
        qe, qh = [np.sum(k[i]*wz[:, None]*w[None, :]*speed[None, :])*1e-6 for k in (ke, kh)]
        ds = -1j*omega/(4*pin)*(qe+qh)*1e-3
        rows.append(dict(offset_mm=delta, predicted_complex_slope_per_mm=[ds.real, ds.imag],
            predicted_abs_slope_per_mm=float(np.real(np.conj(s)/abs(s)*ds)),
            electric_abs_slope_per_mm=float(np.real(source*qe)/(4*pin)*1e-3),
            magnetic_abs_slope_per_mm=float(np.real(source*qh)/(4*pin)*1e-3),
            raw_code_midplane_line_integral=float(np.sum(raw.reshape(len(offsets), 3, n)[i, 1]*w*speed))))
    np.savez_compressed(folder/'baseline_fields.npz', points=points, E=e, H=magnetic,
        speed=speed, line_weights_mm=w, z_weights_mm=wz, offsets_mm=offsets, S=s, containing_cells=found)
    return dict(predictions=rows, sampled_cell_quality_quantiles=np.quantile(quality[found], [0, .5, 1]).tolist(),
        normalization_note='Conditional -j*omega/(4*Pin) peak-field PEC formula; wave-port reaction normalization not independently certified')


def comparisons(rows):
    by_h = {r['h_mm']: r for r in rows if not r.get('repeat')}
    if 0 not in by_h or 'fields' not in by_h[0]:
        return []
    base = by_h[0]
    s0 = complex(*base['s11'])
    out = []
    for h in (.1, .05):
        if h not in by_h or -h not in by_h:
            continue
        sp, sm = [complex(*by_h[v]['s11']) for v in (h, -h)]
        ds, df = (sp-sm)/(2*h), (abs(sp)-abs(sm))/(2*h)
        for p in base['fields']['predictions']:
            pred = p['predicted_abs_slope_per_mm']
            ds_pred = complex(*p['predicted_complex_slope_per_mm'])
            out.append(dict(h_mm=h, offset_mm=p['offset_mm'], fd_abs_slope_per_mm=df,
                fd_complex_slope_per_mm=[ds.real, ds.imag], predicted_abs_slope_per_mm=pred,
                complex_relative_mismatch=abs(ds-ds_pred)/max(abs(ds), 1e-30),
                actual_delta_plus=abs(sp)-abs(s0), predicted_delta_plus=h*pred,
                actual_over_predicted_plus=(abs(sp)-abs(s0))/(h*pred) if pred else None,
                remainder_plus=abs(sp)-abs(s0)-h*pred,
                remainder_minus=abs(sm)-abs(s0)+h*pred,
                fd_chain_rule=float(np.real(np.conj(s0)/abs(s0)*ds))))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int)
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--case', choices=['divider', 'coupler'], nargs='+', default=['divider', 'coupler'])
    args = parser.parse_args()
    if args.self_check:
        for name in ('divider', 'coupler'):
            cfg = CaseConfig.from_yaml(ROOT/f'configs/{name}.yaml')
            for h in (0., .1, -.1, .05, -.05, 1e-6, -1e-6):
                geometry(cfg, h)
        print('14 exact CAD/area/quadrature checks passed')
        s0, ds = .3+.4j, .02-.01j
        expected = float(np.real(np.conj(s0)/abs(s0)*ds))
        toy = [dict(h_mm=0., s11=[s0.real, s0.imag], fields=dict(predictions=[dict(
            offset_mm=.05, predicted_abs_slope_per_mm=expected,
            predicted_complex_slope_per_mm=[ds.real, ds.imag])]))]
        for h in (.1, -.1, .05, -.05):
            s = s0+h*ds
            toy.append(dict(h_mm=h, s11=[s.real, s.imag]))
        checks = comparisons(toy)
        for row in checks:
            np.testing.assert_allclose(row['fd_complex_slope_per_mm'], [ds.real, ds.imag], atol=1e-15)
            np.testing.assert_allclose(row['fd_chain_rule'], expected, atol=1e-15)
            np.testing.assert_allclose(row['fd_abs_slope_per_mm'], expected, rtol=2e-5)
        np.testing.assert_allclose(checks[0]['remainder_plus']/checks[1]['remainder_plus'], 4., rtol=.002)
        print('2 analytic complex/abs directional differences and quadratic residual check passed')
        return
    assert args.pid, 'Supply an existing CST environment PID'
    import cst.interface
    from eaopt.cli import make_log, safe_console
    from eaopt.solver import cst_model, cst_results as R
    from eaopt.solver.cst import CstSolver, _run_solver, _save
    safe_console()
    OUT.mkdir(exist_ok=args.resume)
    tracked = ['adjoint_levelset.py', 'eaopt/runner.py', 'eaopt/geometry/design.py',
        'eaopt/solver/cst.py', 'eaopt/solver/cst_model.py', 'configs/divider.yaml', 'configs/coupler.yaml']
    hashes = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in tracked}
    de = cst.interface.DesignEnvironment.connect(args.pid)
    initial_projects = de.list_open_projects()
    assert not initial_projects, 'Keep unrelated CST projects untouched'
    quiet = de.in_quiet_mode()
    original = cst_model.frequency_domain_solver_setup
    cst_model.frequency_domain_solver_setup = lambda p, f: original(p, f).replace(
        '.MaxCPUs "2"', '.MaxCPUs "1"').replace('.AccuracyTet "1e-6"', '.AccuracyTet "1e-8"')
    solver = None
    de.set_quiet_mode(True)
    try:
        for name in args.case:
            folder = OUT/name
            folder.mkdir(exist_ok=args.resume)
            result_path = folder/'results.json'
            report = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() and args.resume else dict(
                case=name, objective='abs(S11)', mesh_step_mm=.5, adaptive=False,
                solver='FEM Second order, Direct 1e-8, single CPU', evaluations=[],
                fixed_mesh_controls_not_fixed_topology=True, production_hashes=hashes)
            report.pop('error', None)
            cfg = CaseConfig.from_yaml(ROOT/f'configs/{name}.yaml')
            cfg.objective.type, cfg.objective.to_port = 'reflection', 1
            setup, model = load_case(cfg)
            solver = CstSolver(cfg, setup=replace(setup, solver_type='fd'), model=model,
                auto_init=True, point_sampling=True, log=make_log(folder/'run.log'))
            solver.de = de
            try:
                for index, (label, h, repeat) in enumerate([
                    ('base', 0., False), ('plus0.1', .1, False), ('minus0.1', -.1, False),
                    ('plus0.05', .05, False), ('minus0.05', -.05, False), ('repeat', 0., True)]):
                    if any(r['label'] == label and (h != 0 or repeat or 'fields' in r) for r in report['evaluations']):
                        continue
                    case_dir = folder/label
                    case_dir.mkdir(exist_ok=args.resume)
                    cfg.output.dir = str(case_dir)
                    solver.begin_iteration(index)
                    points, metadata, *_ = geometry(cfg, h)
                    # Always start independent CAD projects; no HJ/projection or optimizer state.
                    solver.build_model([points], None)
                    project, path = solver.projects['fwd'], solver.project_path('fwd')
                    prepared = unary_union([Polygon(p) for p in solver.last_prepared_polys])
                    assert prepared.symmetric_difference(Polygon(points)).area < 1e-10
                    commands = ('With FDSolver\n .Type "Direct"\n .SetAllowFloatDirectSolver "False"\nEnd With\n'
                        'Solid.SetMeshStepwidthTet "design_region:design_0", "0.5"\n')
                    project.model3d.add_to_history('Fixed mesh S11 directional diagnostic', commands)
                    audit = native_audit(project, case_dir/'native.txt', metadata, setup.metal_thickness_mm)
                    write(case_dir/'geometry.json', dict(vertices=points.tolist(), **metadata, native=audit))
                    _save(project, path, solver.log)
                    seconds = _run_solver(project.model3d, solver.log)
                    _save(project, path, solver.log)
                    assert project.model3d.get_solver_run_info()['state'] == 'SUCCESS'
                    result_dir = path.with_suffix('')/'Result'
                    shutil.copy2(result_dir/'Model.log', case_dir/'Model.log')
                    s = R.read_s_params(path, 1, response_ports=(1,), freq_ghz=setup.frequency_ghz,
                        exact_frequency=True)[1, 1]
                    pin = R.read_incident_power(path, 1, setup.frequency_ghz)
                    mesh = read_slim(result_dir/'3d.slim')
                    row = dict(label=label, h_mm=h, repeat=repeat, s11=[s.real, s.imag], magnitude=abs(s),
                        incident_power_w=pin, mesh=mesh[2], native_geometry=audit, seconds=seconds,
                        project_path=str(path))
                    if h == 0 and not repeat:
                        row['fields'] = sample_predictions(project, path, case_dir, cfg, setup, s, pin, mesh)
                    report['evaluations'] = [r for r in report['evaluations'] if r['label'] != label]+[row]
                    report['comparisons'] = comparisons(report['evaluations'])
                    write(result_path, report)
                    print('CASE_DONE', name, label, abs(s), flush=True)
                    solver.close()
                report['completed'] = True
                base = next(r for r in report['evaluations'] if r['label'] == 'base')
                repeat = next(r for r in report['evaluations'] if r['label'] == 'repeat')
                report['repeat_abs_difference'] = abs(base['magnitude']-repeat['magnitude'])
                report['repeat_complex_difference'] = abs(complex(*base['s11'])-complex(*repeat['s11']))
                report['repeat_mesh_identical'] = base['mesh']['topology_sha256'] == repeat['mesh']['topology_sha256']
                report['all_meshes_identical'] = len({r['mesh']['topology_sha256'] for r in report['evaluations']}) == 1
            except Exception as error:
                report.update(completed=False, error=repr(error))
                raise
            finally:
                solver.close()
                write(result_path, report)
    finally:
        if solver is not None:
            solver.close()
        cst_model.frequency_domain_solver_setup = original
        de.set_quiet_mode(quiet)
        unchanged = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == value for p, value in hashes.items()}
        write(OUT/'final_state.json', dict(environment_pid=args.pid, projects=de.list_open_projects(),
            quiet_mode=de.in_quiet_mode(), production_hashes_unchanged=unchanged))
        assert all(unchanged.values())


if __name__ == '__main__':
    main()
