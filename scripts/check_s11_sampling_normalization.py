"""Read-only boundary quadrature and wave-port reaction audit of S11 controls."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

import check_s11_cross_case as experiment
from check_antenna_field_sampling import read_slim, containing_cells
from eaopt.solver import cst_results as R
from eaopt.solver.cst import _open_or_reuse

ROOT, OUT = experiment.ROOT, experiment.OUT


def pair(value):
    return [float(np.real(value)), float(np.imag(value))]


def export_item(model, item, xyz, dest):
    """Use the same native point export as cst_results, for an explicit port item."""
    pointfile = dest.with_suffix('.points.txt')
    if not dest.exists():
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
    assert table.shape == (len(xyz), 9) and np.isfinite(table).all()
    distance, index = cKDTree(xyz).query(table[:, :3])
    assert distance.max() < 1e-5 and len(np.unique(index)) == len(xyz)
    result = np.empty((len(xyz), 3), complex)
    result[index] = table[:, 3::2]+1j*table[:, 4::2]
    return result


def axis_quadrature(edges, step):
    points, weights = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        panels = np.linspace(lo, hi, int(np.ceil((hi-lo)/step))+1)
        half, middle = np.diff(panels)/2, (panels[:-1]+panels[1:])/2
        points.extend(np.column_stack((middle-half/np.sqrt(3), middle+half/np.sqrt(3))).ravel())
        weights.extend(np.repeat(half, 2))
    return np.array(points), np.array(weights)


def port_audit(project, base, cfg, model, folder):
    m3d = project.model3d
    blocks = [code for title, code in model.template_blocks(cfg.name, 1, solver_type='fd',
        initial_metal=[p.vertices for p in cfg.initial_metal]) if title == 'Port p1']
    assert len(blocks) == 1 and '.Orientation "xmin"' in blocks[0]
    bounds = np.array([[float(v) for v in re.search(r'\.'+axis+r'range\s+"([^"]+)",\s*"([^"]+)"', blocks[0]).groups()]
        for axis in ('X', 'Y', 'Z')])
    tree = R._tree_items(m3d)
    experiment.write(folder/'tree.json', tree)
    items = []
    for kind in ('e1', 'h1'):
        matches = [p for p in tree if '\\Port Modes\\' in p and p.endswith('\\Port1\\'+kind)]
        assert len(matches) == 1, matches
        items.append(matches[0])
    strip = [-1., 1.] if cfg.name == 'divider' else [1., 2.6]
    yedges = sorted({*bounds[1], *strip})
    zedges = sorted({*bounds[2], -.762, 0., .035})
    result = dict(bounds_mm=bounds.tolist(), inward_normal=[1, 0, 0], rows=[])
    previous = None
    for step in (.1, .05, .025):
        y, wy = axis_quadrature(yedges, step)
        z, wz = axis_quadrature(zedges, step)
        yy, zz = np.meshgrid(y, z, indexing='ij')
        points = np.column_stack((np.full(yy.size, bounds[0, 0]), yy.ravel(), zz.ravel()))
        w = (wy[:, None]*wz).ravel()*1e-6
        np.testing.assert_allclose(w.sum(), np.prod(np.diff(bounds[1:], axis=1))*1e-6, atol=1e-15)
        e, h = [export_item(m3d, item, points, folder/f'port_{step:g}_{kind}.txt')
            for item, kind in zip(items, ('e1', 'h1'))]
        flux = np.sum(np.cross(e, h)[:, 0]*w)
        conjugate_flux = np.sum(np.cross(e, h.conj())[:, 0]*w)
        pin = base['incident_power_w']
        row = dict(step_mm=step, points=len(points), bilinear_flux_w=pair(flux),
            average_power_w=float(.5*conjugate_flux.real),
            peak_power_normalization_relative_error=float(abs(.5*conjugate_flux.real-pin)/pin),
            reaction_coefficient_relative_difference_from_4Pin=float(abs(2*flux-4*pin)/(4*pin)),
            successive_bilinear_change=float(abs(flux-previous)/abs(flux)) if previous is not None else None)
        if step == .05:
            ef, hf = [export_item(m3d, R.resolve_field_item(m3d, kind, 5., stimulus_port=1), points,
                folder/f'total_port_{kind}.txt') for kind in ('Efield', 'Hfield')]
            e[:, 0] = h[:, 0] = 0
            ce = np.sum(np.sum(ef*e.conj(), axis=1)*w)/np.sum(np.sum(abs(e)**2, axis=1)*w)
            ch = np.sum(np.sum(hf*h.conj(), axis=1)*w)/np.sum(np.sum(abs(h)**2, axis=1)*w)
            a, b = (ce+ch)/2, (ce-ch)/2
            s = complex(*base['s11'])
            result['total_field_projection'] = dict(incident=pair(a), reflected=pair(b),
                incident_error_from_unit=float(abs(a-1)), reconstructed_s11=pair(b/a),
                s11_absolute_error=float(abs(b/a-s)),
                note='Independent projection diagnostic, never used to fit or rescale the boundary prediction')
        result['rows'].append(row)
        experiment.write(folder/'port.json', result)
        if step <= .05 and row['successive_bilinear_change'] < .005 and row['peak_power_normalization_relative_error'] < .01:
            break
        previous = flux
    result['power_integral_pass_1pct'] = result['rows'][-1]['peak_power_normalization_relative_error'] < .01
    result['quadrature_pass_0_5pct'] = result['rows'][-1]['successive_bilinear_change'] < .005
    result['reaction_pass_1pct'] = result['rows'][-1]['reaction_coefficient_relative_difference_from_4Pin'] < .01
    result['incident_projection_pass_1pct'] = result['total_field_projection']['incident_error_from_unit'] < .01
    result['s11_projection_pass_1pct_relative'] = result['total_field_projection']['s11_absolute_error'] < .01*abs(complex(*base['s11']))
    experiment.write(folder/'port.json', result)
    return result


def boundary_audit(project, base, cfg, setup, folder):
    _, meta, *_ = experiment.geometry(cfg, 0.)
    a, b = meta['x_range_mm']
    xnodes = np.linspace(a, b, 25)
    unodes = np.sin(np.pi*(xnodes-a)/(b-a))**2
    unodes[[0, -1]] = 0
    offsets = [.05, .025, .0125, .00625, .003125, .00078125]
    rules = [(4, 3), (4, 7), (4, 15), (8, 15)]
    points, specifications = [], []
    for order_x, order_z in rules:
        nx, wx = np.polynomial.legendre.leggauss(order_x)
        nz, wz = np.polynomial.legendre.leggauss(order_z)
        t = (nx+1)/2
        x = (xnodes[:-1, None]+np.diff(xnodes)[:, None]*t).ravel()
        u = (unodes[:-1, None]*(1-t)+unodes[1:, None]*t).ravel()
        w = (np.diff(xnodes)[:, None]*wx/2).ravel()
        z, wz = (nz+1)*setup.metal_thickness_mm/2, wz*setup.metal_thickness_mm/2
        for delta in offsets:
            xx, zz = np.meshgrid(x, z, indexing='ij')
            part = np.column_stack((xx.ravel(), np.full(xx.size, meta['y_mm']+delta), zz.ravel()))
            points.append(part)
            weights = (w[:, None]*wz[None, :]*u[:, None]).ravel()*1e-6
            np.testing.assert_allclose(weights.sum(), meta['area_slope_mm']*setup.metal_thickness_mm*1e-6, atol=1e-15)
            specifications.append((order_x, order_z, delta, len(part), weights))
    points = np.concatenate(points)
    e, h = [R.export_field_points(project.model3d, kind, 5., points, folder/f'boundary_{kind}.txt', stimulus_port=1)
        for kind in ('Efield', 'Hfield')]
    path = Path(base['project_path'])
    xyz, tetra, identity = read_slim(path.with_suffix('')/'Result/3d.slim')
    found, _, quality, _ = containing_cells(xyz, tetra, points)
    assert np.all(found >= 0)
    electric = experiment.als.EPS0*e[:, 1]**2
    magnetic = experiment.als.MU0*(h[:, 0]**2+h[:, 2]**2)
    omega = 2*np.pi*5e9
    s = complex(*base['s11'])
    phase = np.conj(s)/abs(s)
    rows, start = [], 0
    for ox, oz, delta, count, w in specifications:
        qe, qh = [np.sum(k[start:start+count]*w) for k in (electric, magnetic)]
        ds = -1j*omega/(4*base['incident_power_w'])*(qe+qh)*1e-3
        rows.append(dict(order_x=ox, order_z=oz, offset_mm=delta, count=count,
            complex_slope_per_mm=pair(ds), abs_slope_per_mm=float(np.real(phase*ds)),
            electric_abs_slope_per_mm=float(np.real(phase*(-1j*omega/(4*base['incident_power_w']))*qe)*1e-3),
            magnetic_abs_slope_per_mm=float(np.real(phase*(-1j*omega/(4*base['incident_power_w']))*qh)*1e-3)))
        start += count
    assert start == len(points)
    np.savez_compressed(folder/'boundary_fields.npz', points=points, E=e, H=h, containing_cells=found)
    result = dict(rows=rows, points=len(points), mesh=identity,
        sampled_quality_quantiles=np.quantile(quality[found], [0, .5, 1]).tolist(),
        note='Finite-offset fields; quadrature convergence alone does not establish boundary-trace or mesh convergence')
    # Independently reproduce the first diagnostic quadrature, using another array layout.
    for old in base['fields']['predictions']:
        new = next(r for r in rows if r['order_x'] == 4 and r['order_z'] == 3 and r['offset_mm'] == old['offset_mm'])
        np.testing.assert_allclose(new['complex_slope_per_mm'], old['predicted_complex_slope_per_mm'], rtol=1e-10, atol=1e-12)
    result['previous_sampling_reproduced'] = True
    experiment.write(folder/'boundary.json', result)
    return result


def self_check():
    y, wy = axis_quadrature([0., 1., 3.], .37)
    z, wz = axis_quadrature([-2., 0., 2.], .41)
    yy, zz = np.meshgrid(y, z, indexing='ij')
    w = (wy[:, None]*wz).ravel()
    np.testing.assert_allclose(w.sum(), 12., atol=1e-12)
    np.testing.assert_allclose(np.sum(yy.ravel()**2*w), 36., atol=1e-12)
    # A phase-bearing vector field distinguishes bilinear and conjugate products.
    e = np.tile([0., 1+2j, 0.], (len(w), 1))
    h = np.tile([0., 0., 3-1j], (len(w), 1))
    np.testing.assert_allclose(np.sum(np.cross(e, h)[:, 0]*w), (1+2j)*(3-1j)*12.)
    np.testing.assert_allclose(np.sum(np.cross(e, h.conj())[:, 0]*w), (1+2j)*(3+1j)*12.)
    print('4 area/polynomial/bilinear/conjugate quadrature checks passed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int)
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    self_check()
    if args.self_check:
        return
    import cst.interface
    de = cst.interface.DesignEnvironment.connect(args.pid)
    assert not de.list_open_projects()
    quiet = de.in_quiet_mode()
    de.set_quiet_mode(True)
    summary = dict(new_solver_evaluations=0, cases={})
    try:
        for name in ('divider', 'coupler'):
            report = json.loads((OUT/name/'results.json').read_text(encoding='utf-8'))
            base = next(r for r in report['evaluations'] if r['label'] == 'base')
            cfg = experiment.CaseConfig.from_yaml(ROOT/f'configs/{name}.yaml')
            setup, model = experiment.load_case(cfg)
            folder = OUT/name/'sampling_normalization'
            folder.mkdir(exist_ok=True)
            path = Path(base['project_path'])
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            project = _open_or_reuse(de, path, print)
            try:
                summary['cases'][name] = dict(port=port_audit(project, base, cfg, model, folder),
                    boundary=boundary_audit(project, base, cfg, setup, folder))
            finally:
                project.close()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == before
            summary['cases'][name]['source_project_unchanged'] = True
            experiment.write(OUT/'sampling_normalization.json', summary)
    finally:
        de.set_quiet_mode(quiet)
        summary.update(projects=de.list_open_projects(), quiet_mode=de.in_quiet_mode())
        experiment.write(OUT/'sampling_normalization.json', summary)


if __name__ == '__main__':
    main()
