"""Direct-CAD length perturbations, native geometry audit and noise-aware FD."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

import check_antenna_geometry_consistency as d
from check_antenna_field_sampling import containing_cells, read_slim

OUT = d.ROOT/'results/antenna_directional_validation_2026-11-11'


def native_geometry(project, destination, h):
    code = '''Sub Main
Dim name As String, ok As Boolean
Dim xmin As Double, xmax As Double, ymin As Double, ymax As Double, zmin As Double, zmax As Double
Dim x0 As Double, y0 As Double, z0 As Double, x1 As Double, y1 As Double, z1 As Double
name = "design_region:antenna_metal_0"
Open "OUTPUT" For Output As #1
Print #1, "volume|" & Solid.GetVolume(name)
Print #1, "area|" & Solid.GetArea(name)
Print #1, "vertices|" & Solid.GetNumberOfPoints(name)
ok = Solid.GetLooseBoundingBoxOfShape(name, xmin, xmax, ymin, ymax, zmin, zmax)
If Not ok Then Err.Raise 1, , "Patch bounding box unavailable"
Print #1, "bbox|" & xmin & "|" & xmax & "|" & ymin & "|" & ymax & "|" & zmin & "|" & zmax
ok = DiscretePort.GetCoordinates(1, x0, y0, z0, x1, y1, z1)
If Not ok Then Err.Raise 1, , "Port coordinates unavailable"
Print #1, "port|" & x0 & "|" & y0 & "|" & z0 & "|" & x1 & "|" & y1 & "|" & z1
Print #1, "port_length|" & DiscretePort.GetLength(1)
Print #1, "port_grid_length|" & DiscretePort.GetGridLength(1)
Print #1, "contact_patch|" & Solid.IsPointInsideShape(-19.999999, 0, 0.009, name)
Print #1, "contact_feed|" & Solid.IsPointInsideShape(-20.000001, 0, 0.009, "component1:feed_pad_fixed")
Close #1
End Sub'''.replace('OUTPUT', str(destination))
    project.model3d._execute_vba_code(code)
    record = {}
    for line in destination.read_text().splitlines():
        key, *values = [v.strip() for v in line.split('|')]
        record[key] = [float(v) for v in values] if not key.startswith('contact_') else values[0].lower() == 'true'
    np.testing.assert_allclose(record['bbox'], [-20, h, -10, 10, 0, .018], rtol=0, atol=1e-10)
    np.testing.assert_allclose(record['volume'], [7.2+.36*h], rtol=0, atol=1e-8)
    np.testing.assert_allclose(record['area'], [801.44+40.036*h], rtol=0, atol=1e-8)
    np.testing.assert_allclose(record['port'], [-21.865, 0, -1.524, -20.45, 0, -1.524], rtol=0, atol=1e-10)
    assert record['vertices'] == [8] and record['contact_patch'] and record['contact_feed']
    np.testing.assert_allclose(record['port_length'], [1.415], rtol=0, atol=1e-10)
    return record


def summarize(report):
    rows = {(r['mesh_step_mm'], r['h_mm']): r for r in report['evaluations'] if r.get('completed')}
    out = []
    for h in (.08, .04, .02):
        if not all((step, sign*h) in rows for step in (.5, .35) for sign in (-1, 1)):
            continue
        error = {sign: abs(rows[.5, sign*h]['magnitude']-rows[.35, sign*h]['magnitude']) for sign in (-1, 1)}
        record = dict(h_mm=h, mesh_uncertainty_at_plus=error[1], mesh_uncertainty_at_minus=error[-1],
            derivative_uncertainty_estimate=(error[1]+error[-1])/(2*h), estimates_are_not_rigorous_bounds=True, meshes=[])
        for step in (.5, .35):
            base, plus, minus = [rows[step, v] for v in (0., h, -h)]
            s0, sp, sm = [complex(r['s_real'], r['s_imag']) for r in (base, plus, minus)]
            ds = (sp-sm)/(2*h)
            df = (abs(sp)-abs(sm))/(2*h)
            record['meshes'].append(dict(step_mm=step, central_objective_slope_per_mm=df,
                central_complex_slope_per_mm=[ds.real, ds.imag],
                chain_rule_from_complex_slope=float(np.real(np.conj(s0)/abs(s0)*ds)),
                forward_slope=(abs(sp)-abs(s0))/h, backward_slope=(abs(s0)-abs(sm))/h,
                delta_f_plus=abs(sp)-abs(s0), delta_f_minus=abs(sm)-abs(s0)))
        slope = record['meshes'][-1]['central_objective_slope_per_mm']
        uncertainty = record['derivative_uncertainty_estimate']
        record.update(signal_to_mesh_uncertainty=abs(slope)/max(uncertainty, 1e-30),
            signal_resolved=abs(slope)>5*uncertainty)
        out.append(record)
    report['finite_difference'] = out
    return out


def main():
    import cst.interface
    from eaopt.cli import make_log, safe_console
    from eaopt.solver import cst_model, cst_results as R
    from eaopt.solver.cst import CstSolver, _run_solver, _save
    safe_console()
    resume = '--resume' in sys.argv
    out = OUT/'direct_cad'; out.mkdir(exist_ok=resume)
    report_path = out/'results.json'
    report = dict(evaluations=[], scope='independent direct-CAD diagnostic, fixed Direct 1e-8, no optimizer changes',
        initial_h_mm=.08, smaller_steps_conditional=True)
    if resume:
        shutil.copy2(report_path, out/f'results_before_resume_{time.time_ns()}.json')
        report = json.loads(report_path.read_text()); report.pop('error', None)
    manifest = json.loads((d.ROOT/'results/antenna_corner_analysis_2026-11-10/summary.json').read_text())['production_hashes']
    def checkpoint():
        summarize(report)
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    pid = json.loads((OUT/'connection.json').read_text())['environment_pid']
    de = cst.interface.DesignEnvironment.connect(pid)
    assert not de.list_open_projects(), 'Avoid unrelated open projects'
    quiet = de.in_quiet_mode(); de.set_quiet_mode(True)
    original = cst_model.frequency_domain_solver_setup
    cst_model.frequency_domain_solver_setup = lambda p, f: original(p, f).replace(
        '.MaxCPUs "2"', '.MaxCPUs "1"').replace('.AccuracyTet "1e-6"', '.AccuracyTet "1e-8"')
    solver = CstSolver(d.CFG, setup=replace(d.SETUP, solver_type='fd'), model=d.MODEL,
        force_new=True, auto_init=True, point_sampling=True, log=make_log(out/'run.log'))
    solver.de = de
    log = solver.log
    try:
        for h in (0., .08, -.08, .04, -.04, .02, -.02):
            if abs(h) in (.04, .02):
                fd = summarize(report)
                previous = next(r for r in fd if r['h_mm'] == (.08 if abs(h) == .04 else .04))
                if not previous['signal_resolved']:
                    report['stopped_before_smaller_steps'] = dict(next_h_mm=abs(h),
                        reason='central difference does not exceed five times cross-mesh uncertainty estimate')
                    break
            for step in (.5, .35):
                if any(r['mesh_step_mm'] == step and r['h_mm'] == h and r.get('completed') for r in report['evaluations']):
                    continue
                name = f'mesh{step:g}_h{h:+g}'.replace('+', 'plus').replace('-', 'minus')
                solver.close(); d.CFG.output.dir = str(out/name); solver.begin_iteration(len(report['evaluations']))
                polygon = Polygon(d.rectangle(h, 'length', False)[0])
                assert polygon.is_valid and abs(polygon.area-(400+20*h)) < 1e-10
                solver.build_model(d.rectangle(h, 'length', False), None)
                project = solver.projects['fwd']; path = solver.project_path('fwd')
                command = ('With FDSolver\n .Type "Direct"\n .SetAllowFloatDirectSolver "False"\nEnd With\n'
                    f'Solid.SetMeshStepwidthTet "design_region:antenna_metal_0", "{step:g}"\n')
                project.model3d.add_to_history('direct CAD controlled length derivative', command)
                native = native_geometry(project, out/f'{name}_native.txt', h)
                report['pending_case'] = dict(name=name, h_mm=h, mesh_step_mm=step,
                    native_geometry=native, project_path=str(path), memory=d.memory())
                checkpoint(); _save(project, path, log)
                native_log = path.with_suffix('')/'Result/Model.log'
                try:
                    seconds = _run_solver(project.model3d, log)
                finally:
                    if native_log.exists(): shutil.copy2(native_log, out/f'{name}_Model.log')
                _save(project, path, log)
                assert project.model3d.get_solver_run_info()['state'] == 'SUCCESS'
                s = R.read_s_params(path, 1, response_ports=(1,), freq_ghz=7.5, log=log, exact_frequency=True)[(1, 1)]
                xyz, tetrahedra, identity = read_slim(path.with_suffix('')/'Result/3d.slim')
                row = dict(**report.pop('pending_case'), completed=True, s_real=s.real, s_imag=s.imag,
                    magnitude=abs(s), solver_seconds=seconds, mesh=identity,
                    incident_power_w=R.read_incident_power(path, 1, 7.5, log=log))
                assert abs(row['incident_power_w']-.5) < 1e-10
                report['evaluations'].append(row); checkpoint()
                if h == 0:
                    offsets = [.003125, .00625, .0125, .025, .05]
                    nodes, gauss_weights = np.polynomial.legendre.leggauss(3)
                    heights, z_weights = .009*(1+nodes), .009*gauss_weights
                    ys = -10+(np.arange(200)+.5)*.1
                    specifications = [(offset, z) for offset in offsets for z in heights]
                    points = np.concatenate([np.column_stack((np.full(200, offset), ys, np.full(200, z)))
                        for offset, z in specifications])
                    e, magnetic = [R.export_field_points(project.model3d, kind, 7.5, points,
                        out/f'{name}_{kind}.txt', log=log, stimulus_port=1).T for kind in ('Efield', 'Hfield')]
                    k = d.als.EPS0*e[0]**2+d.als.MU0*(magnetic[1]**2+magnetic[2]**2)
                    k = k.reshape(len(offsets), 3, 200)
                    factor = -1j*2*np.pi*7.5e9
                    row['baseline_integrals'] = []
                    for i, offset in enumerate(offsets):
                        line_mid = .1*np.sum(k[i, 1])
                        surface_si = np.sum(k[i]*z_weights[:, None])*.1*1e-6
                        row['baseline_integrals'].append(dict(offset_mm=offset,
                            raw_midplane_objective_line=float(np.real(np.conj(s)/abs(s)*factor*line_mid)),
                            surface_kernel_si=[surface_si.real, surface_si.imag],
                            omega_surface_chain_rule=float(np.real(np.conj(s)/abs(s)*factor*surface_si))))
                    found, edge_lengths, quality, volume = containing_cells(xyz, tetrahedra, points)
                    assert np.all(found >= 0), 'A requested field point is outside linear tetrahedron hulls'
                    row['sampled_cell_quality'] = np.quantile(quality[found], [0, .1, .5, 1]).tolist()
                    np.savez_compressed(out/f'{name}_baseline_fields.npz', points=points, E=e, H=magnetic,
                        S=s, heights_mm=heights, z_weights_mm=z_weights, offsets_mm=offsets,
                        containing_cells=found)
                    checkpoint()
                solver.close()
        report.update(completed=True, physical_gradient_certified=False)
    except Exception as error:
        report.update(completed=False, error=repr(error)); raise
    finally:
        solver.close(); de.set_quiet_mode(quiet); cst_model.frequency_domain_solver_setup = original
        report.update(memory_after=d.memory(), remaining_open_projects_count=len(de.list_open_projects()),
            production_hashes_unchanged={p:hashlib.sha256((d.ROOT/p).read_bytes()).hexdigest()==value for p,value in manifest.items()})
        assert all(report['production_hashes_unchanged'].values())
        checkpoint()


if __name__ == '__main__':
    main()
