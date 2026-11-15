"""Replay one actual CAD step from archived fields, without starting CST."""
import json
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.geometry.state import CadGeometryState
from eaopt.geometry.evolution import CadEvolution
from eaopt.geometry.levelset import make_level_set, LevelSet2D
from eaopt.geometry.design import connection_boundary_mask, compose_metal_regions
from eaopt.runner import _movement_constraints, _project_case_geometry
from eaopt.solver.case import load_case


def replay(folder):
    folder = Path(folder)
    cfg = CaseConfig.from_yaml(ROOT/'configs/antenna.yaml')
    setup, model = load_case(cfg)
    original = make_level_set(cfg)
    mask, force_metal, force_air, _ = _movement_constraints(original, cfg)
    pinned = connection_boundary_mask(original, cfg, model.fixed_metal_polygons())
    mask[pinned] = 0.
    state = CadGeometryState.from_dict(json.loads((folder/'geometry_state.json').read_text()))
    grid = als.Grid2D(len(state.x), len(state.y), cfg.design_region.grid_step_mm,
                      float(state.x[0]), float(state.y[0]))
    ls = als.LevelSet(grid, state.phi, geometry_state=state)
    saved = np.load(folder/'boundary_fields.npz')
    meta = json.loads((folder/'meta.json').read_text())['tags']['fwd']
    s = complex(*meta['s_params']['S1,1'])
    omega = 2*np.pi*setup.frequency_ghz*1e9
    amplitude = -1j*omega*np.conj(s)/abs(s)

    class CachedProblem:
        def boundary_gradient(self, ls, fwd, boundary):
            np.testing.assert_allclose(boundary.pts, saved['boundary'], rtol=0, atol=1e-12)
            np.testing.assert_allclose(boundary.normals, saved['normals'], rtol=0, atol=1e-12)
            boundary.active[:] = saved['active']
            indices = saved['boundary_indices']
            selected = als.Boundary(boundary.pts[indices], boundary.normals[indices],
                                    boundary.weights[indices], np.ones(len(indices), bool))
            gradient = np.zeros(len(boundary.pts))
            gradient[indices] = als.shape_derivative_mine(selected,
                saved['E_fwd'], saved['H_fwd'], amplitude*saved['E_bwd'],
                amplitude*saved['H_bwd'], omega, float(saved['eps_f_per_m']), als.MU0)
            self.gradient = gradient.copy()
            return gradient

    opt = als.OptimizerConfig(maximize=False, pseudo_time_step=cfg.optimizer.pseudo_time_step,
                              smooth_sigma_cells=1.5)
    problem = CachedProblem()
    step = CadEvolution().prepare(ls, problem, None, mask, opt)
    xy, edges = step.mesh.vertices, step.mesh.edges
    delta = opt.pseudo_time_step*step.velocity
    a, b = edges.T
    old, change = xy[b]-xy[a], delta[b]-delta[a]
    denominator = np.sum(change*change, axis=1)
    time = np.clip(-np.sum(old*change, axis=1)/np.where(denominator > 0, denominator, 1.), 0., 1.)
    minimum = np.linalg.norm(old+time[:, None]*change, axis=1)
    rows = []
    for i in np.argsort(minimum)[:10]:
        rows.append(dict(edge=int(i), vertices_mm=xy[edges[i]].tolist(),
                         displacement_mm=delta[edges[i]].tolist(),
                         initial_length_mm=float(np.linalg.norm(old[i])),
                         closest_time_fraction=float(time[i]), minimum_length_mm=float(minimum[i])))
    report = dict(signed_work=step.signed_work, maximum_displacement_mm=float(np.linalg.norm(delta,axis=1).max()),
                  shortest_swept_edges=rows)
    weights = saved['weights']
    ids = saved['boundary_indices']
    limited = saved['actual_offsets_mm'] < .05-1e-10
    contribution = weights*abs(problem.gradient)
    report['density_diagnostics'] = dict(
        limited_point_count=int(limited.sum()),
        limited_absolute_integral_fraction=float(contribution[ids[limited]].sum()/contribution.sum()),
        largest_points=[dict(index=int(i), point_mm=saved['boundary'][i].tolist(),
                            density=float(problem.gradient[i]), weighted_magnitude=float(contribution[i]))
                        for i in np.argsort(contribution)[-8:][::-1]])
    try:
        trial = step.advance(opt.pseudo_time_step)
        report['transport'] = 'passed'
        unprojected = trial.geometry_state.geometry
        _project_case_geometry(trial, LevelSet2D(original.box, original.dx), cfg,
            original.phi, pinned, force_air, force_metal, None, mask, lambda _: None)
        exported = compose_metal_regions(cfg, trial.phi, geometry_state=trial.geometry_state)
        report['projection'] = dict(regions=len(exported),
            vertices=sum(len(r.outer)+sum(map(len,r.holes)) for r in exported),
            changed_area_mm2=unprojected.symmetric_difference(trial.geometry_state.geometry).area)
        (folder/'replayed_projected_geometry.json').write_text(json.dumps(trial.geometry_state.to_dict()), encoding='utf-8')
    except ValueError as error:
        report['transport'] = str(error)
    (folder/'replayed_step.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    np.savez_compressed(folder/'replayed_step.npz', vertices=xy, edges=edges, displacement=delta)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    replay(sys.argv[1])
