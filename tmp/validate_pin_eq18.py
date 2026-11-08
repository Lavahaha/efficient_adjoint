"""Independently integrate native CST port fields and audit Eq. (18) scaling."""
from pathlib import Path
import hashlib
import json
import re
import sys
import traceback

import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eaopt.solver import cst_results as R
from eaopt.solver.cst import _connect, _open_or_reuse

OUT = ROOT / 'results/pin_eq18_validation_2026-11-03'
SOURCE = ROOT / 'results/fd_single_2026-11-02/coupler_probe/run_20261102T091050988480Z/cst/coupler_fwd.cst'
CONTROL = ROOT / 'results/fd_single_2026-11-02/coupler_port_reference_control/run_20261102T102544501976Z'
MESH_FILES = ('moegeom.tet', 'topot.tet', 'bc3d.tet')


def log(s):
    print(s, flush=True)
    with (OUT / 'execution.log').open('a', encoding='utf-8') as f:
        f.write(str(s) + '\n')


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def pair(z):
    return [float(np.real(z)), float(np.imag(z))]


def mesh_hashes(path):
    return {n: hashlib.sha256((path.with_suffix('') / 'Result' / n).read_bytes()).hexdigest()
            for n in MESH_FILES}


def quadrature_axis(edges, step):
    """Two-point Gauss rule on panels split at material/metal discontinuities."""
    points, weights = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        panel = np.linspace(lo, hi, int(np.ceil((hi-lo)/step)) + 1)
        half = np.diff(panel) / 2
        mid = (panel[:-1] + panel[1:]) / 2
        points.extend(np.column_stack((mid-half/np.sqrt(3), mid+half/np.sqrt(3))).ravel())
        weights.extend(np.repeat(half, 2))
    return np.array(points), np.array(weights)


def integrate(E, H, weights, normal):
    return np.sum((np.cross(E, H) @ normal) * weights)


def self_check():
    u, wu = quadrature_axis([0, 1, 3], .37)
    v, wv = quadrature_axis([-2, 0, 2], .41)
    a, b = np.meshgrid(u, v, indexing='ij')
    w = (wu[:, None] * wv).ravel() * 1e-6
    E = np.zeros((a.size, 3), complex)
    H = E.copy()
    E[:, 1] = (1+2j)*a.ravel()
    H[:, 2] = (3-1j)*a.ravel()
    # Integral u^2 du dv = 36 mm^4 for this analytic polynomial test.
    assert np.isclose(integrate(E, H, w, [1, 0, 0]), (1+2j)*(3-1j)*36e-6, rtol=1e-13)
    assert np.isclose(integrate(E, H.conj(), w, [1, 0, 0]), (1+2j)*(3+1j)*36e-6, rtol=1e-13)
    assert np.isclose(w.sum(), 12e-6, rtol=1e-13)
    log('PASS analytic area, complex cross product, conjugation and polynomial quadrature self-check')


def export_points(model, item, xyz, dest):
    if not dest.exists():
        pointfile = dest.with_suffix('.points.txt')
        np.savetxt(pointfile, xyz, fmt='%.12g')
        R._select_item(model, item)
        model._execute_vba_code(f'''Sub Main
With ASCIIExport
 .Reset
 .Mode "FixedWidth"
 .UseSubvolume False
 .FileName "{dest}"
 .SetPointFile "{pointfile}"
 .ExportCoordinatesInMeter False
 .Execute
End With
End Sub''')
    table = np.atleast_2d(np.loadtxt(dest, skiprows=2))
    assert table.shape == (len(xyz), 9) and np.isfinite(table).all(), (dest, table.shape)
    distance, ix = cKDTree(xyz).query(table[:, :3])
    assert distance.max() < 1e-5 and len(np.unique(ix)) == len(xyz), dest
    data = np.empty((len(xyz), 3), complex)
    data[ix] = table[:, 3::2] + 1j*table[:, 4::2]
    return data


def settings(model, port, folder, project_path):
    dest = folder / 'native_settings.txt'
    model._execute_vba_code(f'''Sub Main
Open "{dest}" For Output As #1
Print #1, GetSolverType()
Print #1, FDSolver.GetStimulationPort
Print #1, FDSolver.GetStimulationMode
Close #1
End Sub''')
    lines = [s.strip() for s in dest.read_text().splitlines() if s.strip()]
    assert lines[0] == 'HF Frequency Domain' and int(lines[1]) == port and int(lines[2]) == 1, lines
    # GetPortMeshCoordinates requires a hex mesh in CST 2024; read actual saved tetra model history.
    history_path = project_path.with_suffix('') / 'Model/3D/Model.mod'
    history = history_path.read_text(encoding='utf-8', errors='replace')
    blocks = [s for s in re.findall(r'With Port\s+(.*?)End With', history, re.S)
              if re.search(r'\.PortNumber\s+"'+str(port)+'"', s)]
    assert len(blocks) == 1, blocks
    block = blocks[0]
    (folder / 'port_definition.txt').write_text(block, encoding='utf-8')
    orientation = re.search(r'\.Orientation\s+"(\w+)"', block).group(1)
    ori = ['xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax'].index(orientation)
    bounds = np.array([[float(x) for x in re.search(r'\.'+a+r'range\s+"([^"]+)",\s*"([^"]+)"', block).groups()]
                       for a in ('X', 'Y', 'Z')])
    normal = np.zeros(3)
    normal[ori//2] = 1 if ori % 2 == 0 else -1
    return dict(solver=lines[0], port=port, mode=1, orientation=ori, bounds_mm=bounds.tolist(),
                inward_normal=normal.tolist(), port_definition_source=str(history_path))


def evaluate(de, tag, path, port):
    folder = OUT / tag
    folder.mkdir(exist_ok=True)
    project = _open_or_reuse(de, path, log)
    try:
        model = project.model3d
        meta = settings(model, port, folder, path)
        log(f'{tag} settings: {meta}')
        meta.update(source=str(path), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    mesh_sha256=mesh_hashes(path), pin_w=R.read_incident_power(path, port, 5., log=log))
        modal_file = path.with_suffix('') / f'Result/Port{port}_Info1(#0000).mmd'
        native_mode = json.loads(modal_file.read_text(encoding='utf-8'))
        assert native_mode['port mode meta data']['mode meta data'][0]['calculation frequency'] == 5e9
        save_json(folder / 'native_mode_metadata.json', native_mode)
        sp = R.read_s_params(path, port, freq_ghz=5., exact_frequency=True, log=log)
        meta['s_params'] = {f'S{i}{j}': pair(z) for (i, j), z in sp.items()}
        tree = R._tree_items(model)
        save_json(folder / 'field_tree.json', tree)
        items = []
        for kind in ('e1', 'h1'):
            candidates = [s for s in tree if s.endswith(f'\\Port{port}\\{kind}') and '\\Port Modes\\' in s]
            assert len(candidates) == 1, candidates
            items.append(candidates[0])
        normal = np.array(meta['inward_normal'])
        bounds = np.array(meta['bounds_mm'])
        axis = int(np.flatnonzero(normal)[0])
        tangent = [j for j in range(3) if j != axis]
        # These current coupler feeds have identical rectangular cross sections.
        strip = [1., 2.6] if port == 1 else [-1.6, 0.]
        uedges = sorted(set([*bounds[tangent[0]], *strip]))
        zedges = sorted(set([*bounds[2], -.762, 0., .035]))
        assert np.allclose(bounds[axis, 0], bounds[axis, 1])
        assert bounds[2, 0] < -.762 < 0 < .035 < bounds[2, 1]
        assert bounds[tangent[0], 0] < strip[0] < strip[1] < bounds[tangent[0], 1]
        rows = []
        for step in (.1, .05, .025, .0125):
            label = f'step_{step:g}'
            u, wu = quadrature_axis(uedges, step)
            v, wv = quadrature_axis(zedges, step)
            U, V = np.meshgrid(u, v, indexing='ij')
            weights = (wu[:, None]*wv).ravel()*1e-6
            xyz = np.zeros((U.size, 3))
            xyz[:, axis] = bounds[axis, 0]
            xyz[:, tangent[0]] = U.ravel()
            xyz[:, tangent[1]] = V.ravel()
            assert np.isclose(weights.sum(), np.prod(np.diff(bounds[tangent], axis=1))*1e-6)
            values = [export_points(model, item, xyz, folder / f'{label}_{kind}.txt')
                      for item, kind in zip(items, ('e1', 'h1'))]
            E, H = values
            I = integrate(E, H, weights, normal)
            J = integrate(E, H.conj(), weights, normal)
            row = dict(step_mm=step, points=len(xyz), area_m2=float(weights.sum()),
                       I18_raw=pair(I), conjugate_flux=pair(J), p_average_w=float(.5*J.real),
                       I18_to_pin=pair(I/meta['pin_w']),
                       error_raw_relative=float(abs(I-meta['pin_w'])/meta['pin_w']),
                       error_average_relative=float(abs(.5*J.real-meta['pin_w'])/meta['pin_w']))
            if rows:
                previous = complex(*rows[-1]['I18_raw'])
                row['successive_change_relative'] = float(abs(I-previous)/abs(I))
            np.savez_compressed(folder / f'{label}.npz', points_mm=xyz, weights_m2=weights, E_in=E, H_in=H,
                                h_cst=H, normal=normal)
            rows.append(row)
            meta['quadrature'] = rows
            save_json(folder / 'integrals.json', meta)
            log(f'{tag}: {json.dumps(row)}')
            if step == .05:
                # Independent consistency check: incident/reflected amplitudes from total fields.
                total = [export_points(model, R.resolve_field_item(model, kind, 5., stimulus_port=port), xyz,
                                       folder / f'{label}_total_{kind}.txt') for kind in ('Efield', 'Hfield')]
                Etotal, Htotal = total
                et, ht = E.copy(), H.copy()
                et[:, axis] = ht[:, axis] = 0
                ce = np.sum(np.sum(Etotal*et.conj(), axis=1)*weights)/np.sum(np.sum(abs(et)**2, axis=1)*weights)
                ch = np.sum(np.sum(Htotal*ht.conj(), axis=1)*weights)/np.sum(np.sum(abs(ht)**2, axis=1)*weights)
                a, b = (ce+ch)/2, (ce-ch)/2
                meta['incident_amplitude_check'] = dict(a_from_total_fields=pair(a), b_from_total_fields=pair(b),
                    E_coefficient=pair(ce), H_coefficient=pair(ch), S_reflection=pair(sp[(port, port)]),
                    incident_error_from_unit=float(abs(a-1)), reflection_error=float(abs(b-sp[(port, port)])),
                    note='Only tangential total fields are projected; no Power Stimulated used for scaling.')
                log(f'{tag} independent wave check: {meta["incident_amplitude_check"]}')
                np.savez_compressed(folder / 'total_fields_check.npz', points_mm=xyz, weights_m2=weights,
                                    Etotal=Etotal, Htotal=Htotal, e1=E, h1=H)
            if (step <= .025 and row.get('successive_change_relative', 1) < .002
                    and row['error_average_relative'] < .01):
                break
        meta['quadrature_converged'] = rows[-1].get('successive_change_relative', 1) < .002
        meta['average_power_pass_1pct'] = rows[-1]['error_average_relative'] < .01
        meta['raw_eq18_pass_1pct'] = rows[-1]['error_raw_relative'] < .01
        meta['total_field_amplitude_check_pass_1pct'] = meta['incident_amplitude_check']['incident_error_from_unit'] < .01
        save_json(folder / 'integrals.json', meta)
        return meta
    finally:
        project.close()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    self_check()
    if '--self-check' in sys.argv:
        return
    before = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    de = _connect(False, True, log, quiet=True)
    report = dict(source=str(SOURCE), native_solver_jobs_this_invocation=0,
                  prior_failed_solver_jobs=1,
                  failed_solver_reason='Backward clone failed: pre-conditioner not enough memory; 947.13 MB available. See bwd/model/Result/Model.log.',
                  source_amplitude=1., source_amplitude_basis='CST documented single-port/mode FD unit source. Independent total-field cross-check did NOT meet 1pct; recorded separately, not used to rescale modal fields.',
                  legacy_bwd_note='coupler_planar_smoke/cst/coupler_bwd.cst has no saved power or fields; use saved same-mesh reference-control pair instead')
    try:
        forward = CONTROL / 'forward_fwd/model.cst'
        bwd = CONTROL / 'forward_bwd/model.cst'
        report['same_control_fwd_bwd_mesh'] = mesh_hashes(forward) == mesh_hashes(bwd)
        assert report['same_control_fwd_bwd_mesh'], 'Saved control pair does not have identical electromagnetic mesh'
        report['fwd'] = evaluate(de, 'fwd', SOURCE, 1)
        save_json(OUT / 'validation.json', report)
        report['control_fwd'] = evaluate(de, 'control_fwd', forward, 1)
        save_json(OUT / 'validation.json', report)
        report['control_bwd'] = evaluate(de, 'control_bwd', bwd, 3)
        report['source_project_unchanged'] = hashlib.sha256(SOURCE.read_bytes()).hexdigest() == before
        assert report['source_project_unchanged']
        report['status'] = 'complete'
        report['status_meaning'] = 'Extraction/integration completed; individual accuracy checks can fail. This is not an all-checks-passed status.'
        save_json(OUT / 'validation.json', report)
        log('COMPLETE Eq. (18) integration: results/pin_eq18_validation_2026-11-03/validation.json')
    finally:
        de.close()


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / 'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
