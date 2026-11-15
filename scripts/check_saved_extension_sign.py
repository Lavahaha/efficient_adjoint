"""Replay saved field gradients to measure actual extension fallback activation."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.interpolate import RegularGridInterpolator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import make_level_set, LevelSet2D
from eaopt.geometry.design import connection_boundary_mask
from eaopt.optimize.constraints import ConnectivityGuard
from eaopt.runner import _movement_constraints, _project_case_geometry
from eaopt.solver.case import load_case


def replay(root):
    frozen = json.loads((root/'run_config.json').read_text(encoding='utf-8'))
    cfg = CaseConfig.from_dict(frozen['config'])
    assert frozen['arguments']['geometry_update'] == 'levelset'
    assert frozen['arguments']['adjoint_source'] == 'objective'
    initial = make_level_set(cfg)
    _, model = load_case(cfg)
    grid = als.Grid2D(initial.nx, initial.ny, initial.dx, initial.xs[0], initial.ys[0])
    mask, fm, fa, anchor = _movement_constraints(initial, cfg)
    pinned = connection_boundary_mask(initial, cfg, model.fixed_metal_polygons())
    mask[pinned] = 0
    guard = ConnectivityGuard(initial, cfg, log=lambda _: None) if cfg.constraints.require_connected else None
    candidate = LevelSet2D(initial.box, initial.dx)
    history = [json.loads(s) for s in (root/'adjoint_levelset_history.jsonl').read_text().splitlines()]
    rows = []
    for row in history:
        it = row['it']
        folder = root/f"iter_{row['eval_id_before']:03d}"
        with np.load(root/f'phi_before_iter_{it:03d}.npz') as snapshot:
            ls = als.LevelSet(grid, snapshot['phi'])
        with np.load(folder/'boundary_fields.npz') as d:
            b = als.Boundary(d['boundary'], d['normals'], d['weights'], d['active'])
            ids = d['boundary_indices']
            selected = als.Boundary(b.pts[ids], b.normals[ids], b.weights[ids], np.ones(len(ids), bool))
            meta = json.loads((folder/'meta.json').read_text())['tags']['fwd']
            key = f'S{cfg.objective.to_port},{cfg.objective.from_port}'
            s = complex(*meta['s_params'][key])
            omega = 2*np.pi*frozen['setup']['frequency_ghz']*1e9
            amplitude = -1j*omega*s.conjugate()/abs(s)
            q = np.zeros(len(b.pts))
            q[ids] = als.shape_derivative_mine(selected, d['E_fwd'], d['H_fwd'],
                amplitude*d['E_bwd'], amplitude*d['H_bwd'], omega, float(d['eps_f_per_m']), als.MU0)
        q *= frozen['arguments']['prefactor_scale']*cfg.optimizer.velocity_sign
        if cfg.name == 'antenna':
            q *= -1
        q[~b.active] = 0
        # Returning zero here only observes whether the original extension fails.
        with patch.object(als, '_positive_boundary_velocity', return_value=np.zeros_like(ls.phi)) as probe:
            original = als.boundary_to_grid_velocity(ls, b, q, mask,
                cfg.level_set.extension_band_mm/grid.dx, smooth_sigma_cells=1.5)
            fallback = probe.called
        velocity = als.boundary_to_grid_velocity(ls, b, q, mask,
            cfg.level_set.extension_band_mm/grid.dx, smooth_sigma_cells=1.5)
        np.testing.assert_allclose(abs(velocity).max()*row['pseudo_time_step_used'],
                                   row['predicted_displacement'], rtol=1e-10, atol=1e-12)
        sampled = RegularGridInterpolator((grid.x, grid.y), velocity)(b.pts)
        work = float(np.sum(b.weights*q*sampled))
        assert work >= -1e-12
        if not fallback:
            np.testing.assert_array_equal(original, velocity)
        rows.append(dict(iteration=it, fallback_triggered=fallback, signed_work=work,
            actual_objective_gain=row['objective_gain'],
            normalized_alignment=work/np.sqrt(np.sum(b.weights*q*q)*np.sum(b.weights*sampled*sampled)),
            predicted_move_mm=row['predicted_displacement']))
        # Reproduce constraints to carry the connectivity guard and mask forward.
        trial = ls.copy()
        trial.phi = als.advect_levelset(ls.phi, velocity, row['pseudo_time_step_used'], grid.dx, cfl=cfg.optimizer.cfl)
        als.apply_constraints(trial, fm, fa, anchor)
        _project_case_geometry(trial, candidate, cfg, initial.phi, pinned,
                               fa, fm, guard, mask, lambda _: None)
        with np.load(root/f'phi_after_iter_{it:03d}.npz') as after:
            np.testing.assert_allclose(trial.phi, after['phi'], rtol=1e-10, atol=1e-11)
    return dict(run=str(root), case=cfg.name, steps=len(rows),
        fallback_count=sum(r['fallback_triggered'] for r in rows),
        worsening_count=sum(r['actual_objective_gain'] < 0 for r in rows), rows=rows)


if __name__ == '__main__':
    runs = [ROOT/'results'/name for name in (
        'antenna_hj_audited_2026-11-15', 'antenna_hj_capped_2026-11-15',
        'antenna_hj_capped_repeat_2026-11-15')]
    for case in ('coupler', 'divider'):
        matches = list((ROOT/'results'/f'{case}_hj_after_cap_change_2026-11-15').glob('run_*/run_config.json'))
        assert len(matches) == 1
        runs.append(matches[0].parent)
    report = [replay(root) for root in runs]
    out = ROOT/'results'/'extension_sign_replay_2026-11-15.json'
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    for run in report:
        print(json.dumps({k:v for k,v in run.items() if k != 'rows'}))
