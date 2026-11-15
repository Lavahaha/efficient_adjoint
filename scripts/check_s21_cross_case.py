"""S21 controls on exactly the saved S11 geometries; two baseline adjoint solves."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np

import check_s11_cross_case as previous
from check_antenna_field_sampling import read_slim, containing_cells

ROOT, SOURCE = previous.ROOT, previous.OUT
OUT = ROOT/'results/s21_cross_case_2026-11-12'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def pair(value):
    return [float(np.real(value)), float(np.imag(value))]


def fingerprint(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def summarize(evaluations, predictions):
    by_h = {r['h_mm']: r for r in evaluations if not r['repeat']}
    s0 = complex(*by_h[0.]['s21'])
    rows = []
    for h in (.1, .05):
        sp, sm = [complex(*by_h[v]['s21']) for v in (h, -h)]
        ds = (sp-sm)/(2*h)
        df = (abs(sp)-abs(sm))/(2*h)
        for p in predictions:
            gs, gf = complex(*p['complex_slope_per_mm']), p['abs_slope_per_mm']
            rows.append(dict(h_mm=h, offset_mm=p['offset_mm'], order_x=p['order_x'], order_z=p['order_z'],
                fd_complex_per_mm=pair(ds), fd_abs_per_mm=df,
                chain_rule_from_complex_fd=float(np.real(np.conj(s0)/abs(s0)*ds)),
                predicted_complex_per_mm=pair(gs), predicted_abs_per_mm=gf,
                complex_relative_mismatch=abs(ds-gs)/max(abs(ds), 1e-30),
                actual_delta_plus=abs(sp)-abs(s0), actual_delta_minus=abs(sm)-abs(s0),
                predicted_delta_plus=h*gf, predicted_delta_minus=-h*gf,
                actual_over_predicted_plus=(abs(sp)-abs(s0))/(h*gf) if gf else None,
                central_fd_over_prediction=df/gf if gf else None,
                abs_remainder_plus=abs(sp)-abs(s0)-h*gf,
                abs_remainder_minus=abs(sm)-abs(s0)+h*gf,
                complex_remainder_plus=pair(sp-s0-h*gs), complex_remainder_minus=pair(sm-s0+h*gs)))
    return rows


def self_check():
    s0, gs = .3+.4j, .02-.01j
    gf = float(np.real(np.conj(s0)/abs(s0)*gs))
    rows = [dict(h_mm=h, s21=pair(s0+h*gs), repeat=False) for h in (0., .1, -.1, .05, -.05)]
    predictions = [dict(offset_mm=.05, order_x=4, order_z=3, complex_slope_per_mm=pair(gs), abs_slope_per_mm=gf)]
    result = summarize(rows, predictions)
    for row in result:
        np.testing.assert_allclose(row['fd_complex_per_mm'], pair(gs), atol=1e-15)
        np.testing.assert_allclose(row['chain_rule_from_complex_fd'], gf, atol=1e-15)
        np.testing.assert_allclose(row['fd_abs_per_mm'], gf, rtol=2e-5)
        assert row['complex_relative_mismatch'] < 1e-12
    np.testing.assert_allclose(result[0]['abs_remainder_plus']/result[1]['abs_remainder_plus'], 4., rtol=.002)
    # Different forward/adjoint fields detect accidental reuse of S11 self products.
    b = previous.als.Boundary(np.zeros((2, 2)), np.tile([0., 1.], (2, 1)), np.ones(2), np.ones(2, bool))
    ef = np.array([[1+1j, 2], [3, 4-1j], [5, 6]], complex)
    hf = 2*ef
    eb, hb = (1-2j)*ef, (3+1j)*hf
    ep, hp = previous.als._boundary_field_products(b, ef, hf, eb, hb)
    np.testing.assert_allclose(ep, ef[1]*eb[1])
    np.testing.assert_allclose(hp, hf[0]*hb[0]+hf[2]*hb[2])
    assert not np.allclose(ep, ef[1]**2)
    print('PASS analytic complex/abs FD, quadratic residual and independent forward/adjoint bilinear fields')


def refined_predictions(project, source_base, cfg, setup, s, pin_f, pin_b, folder):
    from eaopt.solver import cst_results as R
    cache_folder = SOURCE/cfg.name/'sampling_normalization'
    specs = read(cache_folder/'boundary.json')['rows']
    with np.load(cache_folder/'boundary_fields.npz') as cache:
        points, ef, hf = [cache[k].copy() for k in ('points', 'E', 'H')]
    eb, hb = [R.export_field_points(project.model3d, kind, 5., points, folder/f'adjoint_{kind}.txt',
        stimulus_port=2).T for kind in ('Efield', 'Hfield')]
    ef, hf = ef.T, hf.T
    mesh = read_slim(Path(project.filename()).with_suffix('')/'Result/3d.slim')
    found, _, quality, _ = containing_cells(mesh[0], mesh[1], points)
    assert np.all(found >= 0)
    b = previous.als.Boundary(points[:, :2], np.tile([0., 1.], (len(points), 1)),
        np.ones(len(points)), np.ones(len(points), bool))
    ep, hp = previous.als._boundary_field_products(b, ef, hf, eb, hb)
    source = -1j*2*np.pi*5e9*np.conj(s)/abs(s)
    raw = previous.als.shape_derivative_mine(b, ef, hf, source*eb, source*hb, 2*np.pi*5e9)
    np.testing.assert_allclose(raw, np.real(source*(previous.als.EPS0*ep+previous.als.MU0*hp)), rtol=1e-12, atol=1e-7)
    _, metadata, *_ = previous.geometry(cfg, 0.)
    a, end = metadata['x_range_mm']
    xnodes = np.linspace(a, end, 25)
    unodes = np.sin(np.pi*(xnodes-a)/(end-a))**2
    unodes[[0, -1]] = 0
    rows, start = [], 0
    normalization = 4*np.sqrt(pin_f*pin_b)
    for spec in specs:
        ox, oz, delta = spec['order_x'], spec['order_z'], spec['offset_mm']
        nx, wx = np.polynomial.legendre.leggauss(ox)
        nz, wz = np.polynomial.legendre.leggauss(oz)
        t = (nx+1)/2
        x = (xnodes[:-1, None]+np.diff(xnodes)[:, None]*t).ravel()
        u = (unodes[:-1, None]*(1-t)+unodes[1:, None]*t).ravel()
        w = (np.diff(xnodes)[:, None]*wx/2).ravel()
        z, wz = (nz+1)*setup.metal_thickness_mm/2, wz*setup.metal_thickness_mm/2
        xx, zz = np.meshgrid(x, z, indexing='ij')
        expected = np.column_stack((xx.ravel(), np.full(xx.size, metadata['y_mm']+delta), zz.ravel()))
        count = len(expected)
        assert count == spec['count']
        np.testing.assert_allclose(points[start:start+count], expected, atol=1e-12)
        weights = (w[:, None]*wz[None, :]*u[:, None]).ravel()*1e-6
        np.testing.assert_allclose(weights.sum(), metadata['area_slope_mm']*setup.metal_thickness_mm*1e-6, atol=1e-15)
        qe, qh = [np.sum(k[start:start+count]*weights) for k in (previous.als.EPS0*ep, previous.als.MU0*hp)]
        gs = -1j*2*np.pi*5e9/normalization*(qe+qh)*1e-3
        middle = oz//2
        raw_mid = np.sum(raw[start:start+count].reshape(len(x), oz)[:, middle]*u*w)
        rows.append(dict(offset_mm=delta, order_x=ox, order_z=oz,
            complex_slope_per_mm=pair(gs), abs_slope_per_mm=float(np.real(np.conj(s)/abs(s)*gs)),
            electric_abs_per_mm=float(np.real(source*qe)/normalization*1e-3),
            magnetic_abs_per_mm=float(np.real(source*qh)/normalization*1e-3),
            raw_code_midplane_line_integral=float(raw_mid)))
        start += count
    assert start == len(points)
    np.savez_compressed(folder/'adjoint_boundary_fields.npz', points=points, E_bwd=eb, H_bwd=hb,
        E_fwd=ef, H_fwd=hf, containing_cells=found)
    return dict(rows=rows, forward_cache=str(cache_folder/'boundary_fields.npz'),
        quality_quantiles=np.quantile(quality[found], [0, .5, 1]).tolist(), normalization=normalization,
        normalization_assumption='Conditional peak-field -j*omega/[4*sqrt(Pin1*Pin2)], port modal reaction normalization remains unverified')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int)
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    self_check()
    if args.self_check:
        return
    assert args.pid
    import cst.interface
    from eaopt.solver import cst_results as R
    from eaopt.solver.cst import _open_or_reuse, _save, _clear_results, _run_solver
    from eaopt.cli import safe_console
    safe_console()
    OUT.mkdir(exist_ok=args.resume)
    de = cst.interface.DesignEnvironment.connect(args.pid)
    assert not de.list_open_projects()
    quiet = de.in_quiet_mode()
    de.set_quiet_mode(True)
    production_paths = [ROOT/p for p in ('adjoint_levelset.py', 'eaopt/runner.py', 'eaopt/geometry/design.py',
        'eaopt/solver/cst.py', 'eaopt/solver/cst_model.py', 'configs/divider.yaml', 'configs/coupler.yaml')]
    production_before = fingerprint(production_paths)
    summary = dict(cases={}, reused_forward_evaluations=12, new_adjoint_evaluations=0,
        objective='abs(S21)', geometry_and_mesh_controls='identical to saved S11 controls', completed=False)
    try:
        for name in ('divider', 'coupler'):
            folder = OUT/name
            folder.mkdir(exist_ok=args.resume)
            result_path = folder/'results.json'
            if args.resume and result_path.exists() and read(result_path).get('completed'):
                summary['cases'][name] = read(result_path)
                summary['new_adjoint_evaluations'] += 1
                continue
            original = read(SOURCE/name/'results.json')
            cfg = previous.CaseConfig.from_yaml(ROOT/f'configs/{name}.yaml')
            setup, model = previous.load_case(cfg)
            source_files = []
            for r in original['evaluations']:
                path = Path(r['project_path'])
                source_files.extend([path, path.with_suffix('')/'Result/3d.slim', path.with_suffix('')/'Model/3D/Model.mod'])
            source_before = fingerprint(source_files)
            data = dict(case=name, objective='abs(S21)', evaluations=[], completed=False)
            for r in original['evaluations']:
                s = R.read_s_params(r['project_path'], 1, response_ports=(2,), freq_ghz=5., exact_frequency=True)[2, 1]
                data['evaluations'].append(dict(label=r['label'], h_mm=r['h_mm'], repeat=r['repeat'],
                    s21=pair(s), magnitude=abs(s), s11=r['s11'], mesh=r['mesh'], project_path=r['project_path']))
            previous.write(result_path, data)
            base = next(r for r in original['evaluations'] if r['label'] == 'base')
            s21 = complex(*next(r for r in data['evaluations'] if r['label'] == 'base')['s21'])
            source_path = Path(base['project_path'])
            path = folder/'adjoint_port2.cst'
            project = None
            try:
                if path.exists():
                    assert args.resume, 'Never overwrite an existing diagnostic project'
                    project = _open_or_reuse(de, path, print)
                else:
                    project = _open_or_reuse(de, source_path, print)
                    _save(project, path, print, what='Save independent baseline clone for port 2')
                if not (folder/'adjoint_solved.json').exists():
                    assert _clear_results(project.model3d, what='Only independent adjoint clone', log=print)
                    project.model3d.add_to_history('S21 adjoint: excite port 2 only',
                        'With FDSolver\n .Stimulation "2", "1"\nEnd With\n')
                    _, meta, *_ = previous.geometry(cfg, 0.)
                    native = previous.native_audit(project, folder/'adjoint_native.txt', meta, setup.metal_thickness_mm)
                    _save(project, path, print)
                    seconds = _run_solver(project.model3d, print)
                    _save(project, path, print)
                    assert project.model3d.get_solver_run_info()['state'] == 'SUCCESS'
                    shutil.copy2(path.with_suffix('')/'Result/Model.log', folder/'adjoint_Model.log')
                    previous.write(folder/'adjoint_solved.json', dict(seconds=seconds, native_geometry=native))
                pin_b = R.read_incident_power(path, 2, 5.)
                s12 = R.read_s_params(path, 2, response_ports=(1,), freq_ghz=5., exact_frequency=True)[1, 2]
                mesh_b = read_slim(path.with_suffix('')/'Result/3d.slim')
                mesh_f = read_slim(source_path.with_suffix('')/'Result/3d.slim')
                data['adjoint'] = dict(project_path=str(path), incident_power_w=pin_b, stimulus_port=2,
                    s12=pair(s12), reciprocity_absolute_difference=abs(s12-s21),
                    reciprocity_relative_difference=abs(s12-s21)/abs(s21), mesh=mesh_b[2],
                    same_spatial_mesh_as_forward=mesh_b[2]['topology_sha256'] == mesh_f[2]['topology_sha256'])
                data['fields'] = refined_predictions(project, base, cfg, setup, s21, base['incident_power_w'], pin_b, folder)
                data['comparisons'] = summarize(data['evaluations'], data['fields']['rows'])
                chosen = [r for r in data['comparisons'] if r['offset_mm'] == .05 and r['order_x'] == 4 and r['order_z'] == 3]
                d1, d2 = [r['fd_abs_per_mm'] for r in chosen]
                data['stability'] = dict(abs_fd_h_0_1=d1, abs_fd_h_0_05=d2,
                    signs_agree=d1*d2>0, relative_change_to_max=abs(d2-d1)/max(abs(d1), abs(d2)),
                    within_10pct=abs(d2-d1) <= .1*max(abs(d1), abs(d2)))
                repeated = next(r for r in data['evaluations'] if r['repeat'])
                data['repeat_complex_difference'] = abs(complex(*repeated['s21'])-s21)
                data['completed'] = True
                summary['new_adjoint_evaluations'] += 1
            except Exception as error:
                data['error'] = repr(error)
                raise
            finally:
                if project is not None:
                    project.close()
                data['source_files_unchanged'] = fingerprint(source_files) == source_before
                assert data['source_files_unchanged']
                previous.write(result_path, data)
                summary['cases'][name] = data
                previous.write(OUT/'summary.json', summary)
            print('CASE_COMPLETE', name, 'S21', abs(s21), 'stability', data['stability'], flush=True)
        summary['completed'] = True
    finally:
        de.set_quiet_mode(quiet)
        summary.update(projects=de.list_open_projects(), quiet_mode=de.in_quiet_mode(),
            production_files_unchanged=fingerprint(production_paths) == production_before,
            physical_gradient_certified=False)
        assert summary['production_files_unchanged']
        previous.write(OUT/'summary.json', summary)


if __name__ == '__main__':
    main()
