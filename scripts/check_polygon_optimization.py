"""Independent Maxwell objective through the shared CAD optimizer.

This short integration check does not certify final convergence or CST cases.
"""
from pathlib import Path
import json
import hashlib

import numpy as np
from shapely.geometry import Polygon

from pec_polygon_reference import SourceReference, boundary_quadrature, project_edge_density, als
from eaopt.geometry.evolution import CadBoundaryMesh, CadEvolution
from eaopt.geometry.state import CadGeometryState

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'results/polygon_optimization_2026-11-13'


class MaxwellProblem:
    fom_kind = 'abs'

    def __init__(self, output, incident, cluster_count=7, interior_fraction=.6, cluster_ratio=.4, taper_sigma=None):
        self.output, self.incident = output, incident
        self.cluster_count = cluster_count
        self.interior_fraction, self.cluster_ratio = interior_fraction, cluster_ratio
        self.taper_sigma = taper_sigma
        self.solves = []

    def forward(self, ls):
        ls.geometry_state.validate_phi(ls.phi)
        sol = SourceReference(ls.geometry_state.geometry, cluster_count=self.cluster_count,
                              interior_fraction=self.interior_fraction, cluster_ratio=self.cluster_ratio,
                              taper_sigma=self.taper_sigma).solve(
                                  fit_order=12, check_order=15)
        self.solves.append(dict(residual=sol.residual, power_error=sol.power_error,
                               reciprocity_error=sol.reciprocity_error))
        # Reject a degraded reference rather than treating its objective as truth.
        if sol.residual > .002 or sol.power_error > 2e-5 or sol.reciprocity_error > 2e-6:
            self.solves[-1]['failed_geometry'] = ls.geometry_state.to_dict()
            raise ValueError('Independent Maxwell reference failed its accuracy envelope')
        return als.ForwardResult(sol.scattering[self.output, self.incident], None,
                                 {'solution': sol, 'eval_id': len(self.solves)-1})

    def boundary_gradient(self, ls, fwd, boundary):
        mesh = CadBoundaryMesh.from_geometry(ls.geometry_state.geometry, 2*ls.grid.dx)
        points, normals, weights, _ = mesh.quadrature()
        if not (np.array_equal(points, boundary.pts) and np.array_equal(normals, boundary.normals)
                and np.array_equal(weights, boundary.weights)):
            raise ValueError('Equivalent density requires the same CAD edge basis')
        geometry = mesh.moved(np.zeros_like(mesh.vertices), 0.)
        p, n, w, t = boundary_quadrature(geometry, 16, graded=True, with_parameter=True)
        sol = fwd.extra['solution']
        density = sol.density(p, n, self.output, self.incident)
        _, weight = als.fom_value_and_weight(fwd.s, 'abs')
        edges = len(mesh.edges)
        projected = project_edge_density(density.reshape(edges, -1), w.reshape(edges, -1), t.reshape(edges, -1))
        fwd.extra['complex_boundary_density'] = projected
        return np.real(np.conj(weight)*projected)


def check_deformed_reference():
    rows = []
    for output in (0, 1):
        state = CadGeometryState.from_dict(json.loads((OUT/f'S{output+1}1_geometry.json').read_text()))
        grid = als.Grid2D(len(state.x), len(state.y), state.x[1]-state.x[0], state.x[0], state.y[0])
        ls = als.LevelSet(grid, state.phi, geometry_state=state)
        mesh = CadBoundaryMesh.from_geometry(state.geometry, 2*grid.dx)
        p, n, w, _ = mesh.quadrature()
        boundary = als.Boundary(p, n, w, np.ones(len(p), bool))
        previous = None
        for count in (7, 9):
            problem = MaxwellProblem(output, 0, count)
            fwd = problem.forward(ls)
            g = problem.boundary_gradient(ls, fwd, boundary)
            row = dict(target=f'S{output+1}1', clusters=count, s=[fwd.s.real, fwd.s.imag],
                       checks=problem.solves[-1], gradient=g.tolist())
            if previous is not None:
                old_s, old_g = previous
                row['s_absolute_change'] = float(abs(fwd.s-old_s))
                row['gradient_relative_change'] = float(np.sqrt(np.sum(w*(g-old_g)**2)/np.sum(w*g*g)))
            previous = fwd.s, g
            rows.append(row)
            (OUT/'deformed_reference.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
            print(json.dumps({k: v for k, v in row.items() if k != 'gradient'}), flush=True)


def refine_failed_reference(counts=(9, 11), fit_order=12, interior_fraction=.6, output_name=None, cluster_ratio=.4,
                            run_folder='long_S11', taper_sigma=None):
    """Re-solve the rejected trial, without changing or resuming its trajectory."""
    folder = OUT/run_folder
    run = json.loads((folder/'results.json').read_text())
    if run['status'] != 'failed_check' or 'failed_geometry' not in run['solve_checks'][-1]:
        raise ValueError('A terminal forward-accuracy failure with saved geometry is required')
    state = CadGeometryState.from_dict(run['solve_checks'][-1]['failed_geometry'])
    grid = als.Grid2D(len(state.x), len(state.y), state.x[1]-state.x[0], state.x[0], state.y[0])
    ls = als.LevelSet(grid, state.phi, geometry_state=state)
    mesh = CadBoundaryMesh.from_geometry(state.geometry, 2*grid.dx)
    p, n, w, _ = mesh.quadrature()
    boundary = als.Boundary(p, n, w, np.ones(len(p), bool))
    rows, previous = [], None
    for count in counts:
        sol = SourceReference(state.geometry, cluster_count=count, interior_fraction=interior_fraction,
                              cluster_ratio=cluster_ratio, taper_sigma=taper_sigma).solve(
            fit_order=fit_order, check_order=fit_order+3)
        s = sol.scattering[0, 0]
        fwd = als.ForwardResult(s, None, {'solution': sol})
        g = MaxwellProblem(0, 0, count).boundary_gradient(ls, fwd, boundary)
        row = dict(clusters=count, fit_order=fit_order, interior_fraction=interior_fraction, cluster_ratio=cluster_ratio,
                   taper_sigma=taper_sigma,
                   s=[s.real, s.imag], value=float(abs(s)),
                   residual=sol.residual, power_error=sol.power_error, reciprocity_error=sol.reciprocity_error,
                   rank=sol.rank, unknowns=sol.unknowns, condition=sol.condition_number,
                   gradient=g.tolist(),
                   residual_profile=sol.residual_profile,
                   accuracy_envelope_passed=bool(sol.residual <= .002 and sol.power_error <= 2e-5 and sol.reciprocity_error <= 2e-6))
        if previous is not None:
            old_s, old_g = previous
            row['s_absolute_change'] = float(abs(s-old_s))
            row['gradient_relative_change'] = float(np.sqrt(np.sum(w*(g-old_g)**2)/np.sum(w*g*g)))
        previous = s, g
        rows.append(row)
        name = 'failed_trial_refinement.json' if fit_order == 12 else f'failed_trial_fit{fit_order}.json'
        if interior_fraction != .6:
            name = f'failed_trial_sources{interior_fraction:g}_fit{fit_order}.json'
        if output_name is not None:
            name = output_name
        (folder/name).write_text(json.dumps(rows, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in row.items() if k not in ('gradient', 'residual_profile')}), flush=True)


def check_failed_trial_direction():
    folder = OUT/'long_S11'
    run = json.loads((folder/'results.json').read_text())
    state = CadGeometryState.from_dict(run['solve_checks'][-1]['failed_geometry'])
    grid = als.Grid2D(len(state.x), len(state.y), state.x[1]-state.x[0], state.x[0], state.y[0])
    ls = als.LevelSet(grid, state.phi, geometry_state=state)
    problem = MaxwellProblem(0, 0, 13, .8, .5)
    fwd = problem.forward(ls)
    step = CadEvolution().prepare(ls, problem, fwd, np.ones(ls.phi.shape),
                                  als.OptimizerConfig(maximize=False, pseudo_time_step=100.))
    _, _, w, _ = step.mesh.quadrature()
    pred = np.sum(w*fwd.extra['complex_boundary_density']*step.mesh.normal_velocity(step.velocity))
    abs_pred = np.real(np.conj(fwd.s)/abs(fwd.s)*pred)
    np.testing.assert_allclose(abs_pred, -step.signed_work, rtol=1e-12)
    report = dict(clusters=13, interior_fraction=.8, cluster_ratio=.5,
                  prediction=[pred.real, pred.imag], abs_prediction=float(abs_pred),
                  vmax=step.vmax, trials=[], solves=problem.solves, full_goal_achieved=False)
    for time_step in (10., 5.):
        plus = problem.forward(step.advance(time_step))
        minus = problem.forward(step.advance(-time_step))
        fd = (plus.s-minus.s)/(2*time_step)
        abs_fd = (abs(plus.s)-abs(minus.s))/(2*time_step)
        report['trials'].append(dict(time_step=time_step, max_displacement_mm=time_step*step.vmax,
            complex_relative_error=float(abs(pred-fd)/abs(fd)),
            abs_relative_error=float(abs(abs_pred-abs_fd)/abs(abs_fd)),
            complex_remainder=float(abs(plus.s-fwd.s-time_step*pred)),
            abs_remainder=float(abs(abs(plus.s)-abs(fwd.s)-time_step*abs_pred))))
        (folder/'refined_direction_2026-11-14.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report['trials'][-1]), flush=True)


def continue_optimization(output=0, n_iter=60, *, cluster_count=9, source_path=None,
                          interior_fraction=.6, cluster_ratio=.4, taper_sigma=None):
    """Continue the saved three-step geometry; persist all committed iterates."""
    suffix = '' if cluster_count == 9 else f'_c{cluster_count}'
    if interior_fraction != .6 or cluster_ratio != .4:
        suffix += f'_i{interior_fraction:g}_r{cluster_ratio:g}'
    if taper_sigma is not None:
        suffix += f'_t{taper_sigma:g}'
    folder = OUT/f'long_S{output+1}1{suffix}'
    folder.mkdir(parents=True, exist_ok=True)
    if (folder/'results.json').exists():
        raise ValueError('Long-run evidence already exists; inspect it before starting another run')
    source_path = OUT/f'S{output+1}1_geometry.json' if source_path is None else Path(source_path)
    state = CadGeometryState.from_dict(json.loads(source_path.read_text()))
    grid = als.Grid2D(len(state.x), len(state.y), state.x[1]-state.x[0], state.x[0], state.y[0])
    ls = als.LevelSet(grid, state.phi, geometry_state=state)
    problem = MaxwellProblem(output, 0, cluster_count=cluster_count,
                             interior_fraction=interior_fraction, cluster_ratio=cluster_ratio, taper_sigma=taper_sigma)
    cfg = als.OptimizerConfig(maximize=output != 0, pseudo_time_step=100.)
    opt = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape), cfg, geometry_evolution=CadEvolution())
    report = dict(target=f'S{output+1}1', cluster_count=cluster_count, pseudo_time_step=100.,
                  interior_fraction=interior_fraction, cluster_ratio=cluster_ratio,
                  taper_sigma=taper_sigma,
                  initial_from=str(source_path), status='running',
                  history=opt.history, solve_checks=problem.solves, states=[],
                  convergence_verified=False, full_goal_achieved=False, production_changed=False,
                  source_hashes={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                                 ('scripts/check_polygon_optimization.py', 'scripts/pec_polygon_reference.py',
                                  'adjoint_levelset.py', 'eaopt/geometry/evolution.py')})
    def write():
        temporary = folder/'results.tmp'
        temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
        temporary.replace(folder/'results.json')
    def save(index, current, fwd):
        name = f'geometry_{index:03d}.json'
        (folder/name).write_text(json.dumps(current.geometry_state.to_dict()), encoding='utf-8')
        report['states'].append(dict(index=index, geometry=name, s=[fwd.s.real, fwd.s.imag],
                                     value=float(abs(fwd.s))))
        write()
        print(json.dumps(dict(index=index, value=float(abs(fwd.s)),
                              latest=opt.history[-1] if opt.history else None)), flush=True)
    try:
        opt.run(ls, n_iter=n_iter, state_callback=save)
        report['status'], report['stop_reason'] = 'finished', opt.stop_reason
    except ValueError as error:
        report['status'], report['error'] = 'failed_check', str(error)
    finally:
        write()
    print(json.dumps({k: v for k, v in report.items() if k not in ('history', 'states', 'solve_checks')}), flush=True)
    return report


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    theta = 2*np.pi*np.arange(64)/64
    initial = Polygon(10*np.column_stack((np.cos(theta), np.sin(theta))))
    grid = als.Grid2D(41, 41, 1., -20., -20.)
    results = []
    for output, maximize in ((0, False), (1, True)):
        state = CadGeometryState(initial, grid.x, grid.y)
        ls = als.LevelSet(grid, state.phi, geometry_state=state)
        problem = MaxwellProblem(output, 0)
        # Fixed diagnostic time, in physical-density units; production unchanged.
        cfg = als.OptimizerConfig(maximize=maximize, pseudo_time_step=100.)
        opt = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape), cfg,
                                           geometry_evolution=CadEvolution())
        final = opt.run(ls, n_iter=3)
        updated = [r for r in opt.history if r['updated']]
        assert len(updated) == 3, opt.stop_reason
        assert all(r['objective_gain'] > 0 and r['linearization_matches_final_geometry'] for r in updated)
        assert all(r['backtracks'] == 0 and r['pseudo_time_step_used'] == 100. for r in updated)
        results.append(dict(target=f'S{output+1}1', maximize=maximize, history=opt.history,
                            solve_checks=problem.solves, stop_reason=opt.stop_reason))
        (OUT/f'S{output+1}1_geometry.json').write_text(json.dumps(final.geometry_state.to_dict()), encoding='utf-8')
        (OUT/'results.json').write_text(json.dumps(dict(cases=results, convergence_verified=False,
            full_goal_achieved=False, production_changed=False), indent=2), encoding='utf-8')
        print(json.dumps(results[-1]), flush=True)


if __name__ == '__main__':
    main()
