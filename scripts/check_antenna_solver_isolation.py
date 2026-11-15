"""Independent CST backend/refinement controls, with actual mesh fingerprints."""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from dataclasses import replace

import numpy as np

import check_antenna_geometry_consistency as d
from check_antenna_field_sampling import OUT, PREVIOUS, predictions, read_slim


def main():
    import cst.interface
    import cst.results
    from eaopt.cli import make_log, safe_console
    from eaopt.solver import cst_model, cst_results as R
    from eaopt.solver.cst import CstSolver, _run_solver, _save
    safe_console()
    mode = sys.argv[1]
    assert mode in ('backend', 'refinement')
    resume = '--resume' in sys.argv
    out = OUT/mode; out.mkdir(exist_ok=resume)
    log = make_log(out/'run.log')
    pid = json.loads((OUT/'connection.json').read_text())['environment_pid']
    de = cst.interface.DesignEnvironment.connect(pid)
    quiet = de.in_quiet_mode(); de.set_quiet_mode(True)
    original = cst_model.frequency_domain_solver_setup
    cst_model.frequency_domain_solver_setup = lambda p, f: original(p, f).replace(
        '.MaxCPUs "2"', '.MaxCPUs "1"')
    solver = CstSolver(d.CFG, setup=replace(d.SETUP, solver_type='fd'), model=d.MODEL,
        force_new=True, auto_init=True, point_sampling=True, log=log)
    solver.de = de
    with np.load(PREVIOUS/'regional_fields/densities.npz') as data:
        xy, normals, weights = [data[k].copy() for k in ('boundary', 'normals', 'weights')]
    points = np.column_stack((xy+.05*normals, np.full(len(xy), .009)))
    report = dict(mode=mode, evaluations=[], scope='diagnostic copies only; no production parameter changes',
        memory_before=d.memory(), fixed_accuracy=1e-8, frontend_pid=pid)
    if resume:
        shutil.copy2(out/'results.json', out/f'results_before_retry_{time.time_ns()}.json')
        report = json.loads((out/'results.json').read_text())
        report.pop('error', None)

    def checkpoint():
        (out/'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

    configurations = ([(backend.lower(), backend, None, None) for backend in ('Direct', 'Iterative')]
        if mode == 'backend' else [(method.lower()+'_'+representation, 'Direct', method, representation)
            for method in ('Automatic', 'Bisection') for representation in ('four', '85')])
    project = None
    checkpoint()
    try:
        for name, backend, refinement, representation in configurations:
            assert not de.list_open_projects(), 'Avoid interacting with unrelated open projects'
            existing = next((row for row in report['evaluations'] if row['name'] == name), None)
            if existing is not None:
                if 'predictions' in existing:
                    continue
                assert existing['completed'], 'Only completed electromagnetic solves may be reused'
                path = d.ROOT/existing['project_path']
                project = de.open_project(str(path))
                try:
                    text = (path.with_suffix('')/'Result/Model.log').read_text(errors='replace')
                    deltas = list(map(float, re.findall(r'All S-Parameters\s*:\s*([\d.eE+-]+)', text)))
                    existing.update(delta_s=deltas, adaptive_converged=('desired accuracy limit is reached' in text
                        and len(deltas) >= 2 and max(deltas[-2:]) <= 1e-4),
                        ignored_refinement_percent_matches=list(map(int, re.findall(r'\((\d+)%\) had to be ignored', text))))
                    s = complex(existing['s_real'], existing['s_imag'])
                    e, h = [R.export_field_points(project.model3d, kind, 7.5, points,
                        out/f'{name}_{kind}.txt', log=log, stimulus_port=1).T for kind in ('Efield', 'Hfield')]
                    existing['predictions'] = predictions(e, h, s, xy, normals, weights)
                    existing['incident_power_w'] = R.read_incident_power(path, 1, 7.5, log=log)
                    np.savez_compressed(out/f'{name}.npz', E=e, H=h, points=points, S=s,
                        boundary=xy, normals=normals, weights=weights)
                    checkpoint()
                finally:
                    project.close(); project = None
                continue
            if mode == 'backend':
                source = PREVIOUS/'controlled_refinement/local0.5_four/cst'
                folder = out/name/'cst'
                shutil.copytree(source, folder)
                path = folder/'antenna_fwd.cst'
                before = read_slim(path.with_suffix('')/'Result/3d.slim')[2]
                project = de.open_project(str(path))
            else:
                d.CFG.output.dir = str(out/name)
                solver.begin_iteration(len(report['evaluations']))
                solver.build_model(d.rectangle(subdivided=representation == '85'), None)
                project = solver.projects['fwd']; path = solver.project_path('fwd')
                before = None
            command = ('With FDSolver\n' + f' .Type "{backend}"\n .AccuracyTet "1e-8"\n'
                ' .SetAllowFloatDirectSolver "False"\n .MaxCPUs "1"\n'
                f' .MeshAdaptionTet "{"True" if refinement else "False"}"\n'
                ' .ResetSampleIntervals "all"\n'
                f' .AddSampleInterval "7.5", "7.5", "1", "Single", "{"True" if refinement else "False"}"\nEnd With\n')
            if refinement:
                command += ('With MeshAdaption3D\n .SetType "HighFrequencyTet"\n'
                    f' .RefinementType "{refinement}"\n'
                    ' .MinPasses "3"\n .MaxPasses "12"\n .MaxDeltaS "0.0001"\n'
                    ' .NumberOfDeltaSChecks "2"\n .EnableInnerSParameterAdaptation "True"\nEnd With\n')
            project.model3d.add_to_history('independent solver isolation', command)
            (out/f'{name}_settings.vba').write_text(command)
            report['pending_case'] = dict(name=name, backend=backend, refinement=refinement,
                representation=representation, project_path=str(path), memory=d.memory(), mesh_before=before)
            checkpoint(); _save(project, path, log)
            native_log = path.with_suffix('')/'Result/Model.log'
            try:
                seconds = _run_solver(project.model3d, log)
            finally:
                if native_log.exists():
                    shutil.copy2(native_log, out/f'{name}_Model.log')
            _save(project, path, log)
            after = read_slim(path.with_suffix('')/'Result/3d.slim')[2]
            if before:
                assert after['topology_sha256'] == before['topology_sha256'], 'Fixed-mesh isolation failed: mesh changed'
            s = R.read_s_params(path, 1, response_ports=(1,), freq_ghz=7.5,
                log=log, exact_frequency=True)[(1, 1)]
            text = native_log.read_text(errors='replace')
            counts = list(map(int, re.findall(r'Number of mesh cells\s*:\s*(\d+)', text)))
            assert counts and counts[-1] == after['tetrahedra']
            info = project.model3d.get_solver_run_info()
            assert info['state'] == 'SUCCESS', info
            row = dict(**report.pop('pending_case'), mesh_after=after, completed=True,
                solver_seconds=seconds, s_real=s.real, s_imag=s.imag, magnitude=abs(s), mesh_cells_history=counts)
            report['evaluations'].append(row); checkpoint()
            if refinement:
                deltas = list(map(float, re.findall(r'All S-Parameters\s*:\s*([\d.eE+-]+)', text)))
                passes = list(map(int, re.findall(r'Adaptive mesh refinement pass (\d+)', text)))
                assert passes == list(range(1, len(passes)+1)) and len(deltas) == len(passes)-1, 'Incomplete adaptive delta history'
                row.update(delta_s=deltas, adaptive_converged=('desired accuracy limit is reached' in text
                    and len(deltas) >= 2 and max(deltas[-2:]) <= 1e-4))
                skipped = list(map(int, re.findall(r'\((\d+)%\) had to be ignored', text)))
                row['ignored_refinement_percent_matches'] = skipped
            e, h = [R.export_field_points(project.model3d, kind, 7.5, points,
                out/f'{name}_{kind}.txt', log=log, stimulus_port=1).T for kind in ('Efield', 'Hfield')]
            row['predictions'] = predictions(e, h, s, xy, normals, weights)
            row['incident_power_w'] = R.read_incident_power(path, 1, 7.5, log=log)
            np.savez_compressed(out/f'{name}.npz', E=e, H=h, points=points, S=s,
                boundary=xy, normals=normals, weights=weights)
            checkpoint()
            if mode == 'backend':
                project.close()
            else:
                solver.close()
            project = None
        report['completed'] = True
    except Exception as error:
        report.update(completed=False, error=repr(error)); raise
    finally:
        if project is not None and mode == 'backend':
            project.close()
        solver.close(); de.set_quiet_mode(quiet)
        cst_model.frequency_domain_solver_setup = original
        report.update(memory_after=d.memory(), remaining_open_projects_count=len(de.list_open_projects()))
        checkpoint()


if __name__ == '__main__':
    main()
