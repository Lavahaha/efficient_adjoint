"""Independent actual-antenna amplitude scan along an archived CAD step."""
import json
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eaopt.config import CaseConfig
from eaopt.geometry.evolution import CadBoundaryMesh
from eaopt.geometry.state import CadGeometryState
from eaopt.geometry.design import compose_metal_regions, region_rings
from eaopt.solver.case import load_case
from eaopt.solver.cst import CstSolver, _run_solver, _save
from eaopt.solver import cst_results as results
from eaopt.cli import make_log, safe_console


def main(source, factors, affine_length=False):
    safe_console()
    source = Path(source).resolve()
    if not factors or not np.isfinite(factors).all() or any(f < 0 for f in factors):
        raise ValueError('Finite nonnegative displacement factors required')
    state = CadGeometryState.from_dict(json.loads((source/'geometry_state.json').read_text()))
    step = np.load(source/'replayed_step.npz')
    mesh = CadBoundaryMesh.from_geometry(state.geometry, .4)
    np.testing.assert_allclose(mesh.vertices, step['vertices'], rtol=0, atol=1e-12)
    np.testing.assert_array_equal(mesh.edges, step['edges'])
    displacement = step['displacement'].copy()
    if affine_length:
        # Same antenna and ports, but preserve a rectangular eight-vertex solid
        # to isolate geometry-complexity changes from a small physical motion.
        amplitude = float(np.linalg.norm(displacement, axis=1).max())
        fraction = (mesh.vertices[:, 0]-mesh.vertices[:, 0].min())/np.ptp(mesh.vertices[:, 0])
        displacement[:] = 0.
        displacement[:, 0] = amplitude*fraction
    out = ROOT/'results'/('antenna_direction_scan_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
    out.mkdir()
    cfg = CaseConfig.from_yaml(ROOT/'configs/antenna.yaml')
    cfg.output.dir = str(out)
    setup, model = load_case(cfg)
    setup = replace(setup, solver_type='fd')
    log = make_log(out/'run.log')
    solver = CstSolver(cfg, setup=setup, model=model, auto_init=True,
                       force_new=True, point_sampling=True, log=log)
    report = dict(source=str(source), scope='fixed archived CAD displacement; no manufacturing projection; no optimizer change',
                  direction='affine length control' if affine_length else 'archived optimizer direction',
                  evaluations=[], status='running')
    def checkpoint():
        (out/'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    checkpoint()
    try:
        for index, factor in enumerate((0., *factors, 0.)):
            trial = state.with_geometry(mesh.moved(displacement, factor))
            regions = compose_metal_regions(cfg, trial.phi, geometry_state=trial)
            rings = region_rings(regions)
            solver.begin_iteration(index)
            solver.build_model(rings)
            folder = out/f'iter_{index:03d}'
            folder.mkdir(exist_ok=True)
            (folder/'geometry_state.json').write_text(json.dumps(trial.to_dict()), encoding='utf-8')
            project = solver.projects['fwd']
            path = solver.project_path('fwd')
            native_path = folder/'native_volume.txt'
            native_solids = model.merged_design_solids(regions)
            if len(native_solids) != 1:
                raise ValueError('Direction scan requires one connected native signal solid')
            native_name, native_polygon = native_solids[0]
            code = ('Sub Main\nOpen "'+str(native_path)+'" For Output As #1\n'
                    f'Print #1, Solid.GetVolume("{native_name}")\n'
                    f'Print #1, Solid.GetNumberOfPoints("{native_name}")\n'
                    'Close #1\nEnd Sub')
            project.model3d._execute_vba_code(code)
            native_volume, native_vertices = map(float, native_path.read_text().split())
            expected_volume = native_polygon.area*setup.metal_thickness_mm
            np.testing.assert_allclose(native_volume, expected_volume, rtol=1e-8, atol=1e-9)
            elapsed = _run_solver(project.model3d, log)
            _save(project, path, log)
            if project.model3d.get_solver_run_info()['state'] != 'SUCCESS':
                raise RuntimeError('Direction-scan forward solve did not succeed')
            s = results.read_s_params(path, 1, response_ports=(1,),
                freq_ghz=setup.frequency_ghz, log=log, exact_frequency=True)[(1,1)]
            row = dict(index=index, factor=factor, real=s.real, imag=s.imag,
                       magnitude=abs(s), solver_seconds=elapsed,
                       native_volume_mm3=native_volume, expected_volume_mm3=expected_volume,
                       native_vertices=int(native_vertices))
            if report['evaluations']:
                base = report['evaluations'][0]
                row['delta_magnitude'] = abs(s)-base['magnitude']
                if factor:
                    row['one_sided_slope'] = row['delta_magnitude']/factor
            report['evaluations'].append(row)
            checkpoint()
            log(f'[direction scan] factor={factor:g}, |S11|={abs(s):.10g}')
        report['status'] = 'complete'
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        checkpoint()
        solver.close()
    print(out)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('--factors', nargs='+', type=float, default=[.0625,.125,.25,.5,1.])
    parser.add_argument('--affine-length', action='store_true')
    args = parser.parse_args()
    main(args.source, args.factors, args.affine_length)
