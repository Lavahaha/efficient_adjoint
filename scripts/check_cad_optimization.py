"""Exercise the actual optimizer on an exactly integrable shape objective.

This is a geometry/optimizer check, not an electromagnetic S-parameter case.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from eaopt.geometry.evolution import CadEvolution
from eaopt.geometry.state import CadGeometryState

OUT = ROOT/'results/cad_optimization_2026-11-13'


def integral(geometry, radius):
    """Exact polygon integral of radius**2 - x**2 - y**2."""
    if geometry.is_empty:
        return 0.
    parts = [geometry] if geometry.geom_type == 'Polygon' else geometry.geoms
    value = 0.
    for part in parts:
        part = orient(part, sign=1.)
        for ring in (part.exterior, *part.interiors):
            a = np.asarray(ring.coords)[:-1]
            b = np.roll(a, -1, axis=0)
            cross = a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]
            moment = np.sum(a*a+a*b+b*b, axis=1)/12
            value += np.sum(cross*(radius**2/2-moment))
    return float(value)


class GeometricProblem:
    fom_kind = 'real'

    def __init__(self, radius, sign):
        self.radius, self.sign, self.calls = radius, sign, 0

    def forward(self, ls):
        ls.geometry_state.validate_phi(ls.phi)
        value = self.sign*integral(ls.geometry_state.geometry, self.radius)
        result = als.ForwardResult(value, None, {'eval_id': self.calls})
        self.calls += 1
        return result

    def boundary_gradient(self, ls, fwd, boundary):
        return self.sign*(self.radius**2-np.sum(boundary.pts**2, axis=1))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    radius = 1.5
    theta = 2*np.pi*np.arange(64)/64
    initial = Polygon(np.column_stack((.8*np.cos(theta)+.12, 1.8*np.sin(theta)-.08)))
    grid = als.Grid2D(61, 61, .1, -3., -3.)
    maximum = np.pi*radius**4/2
    rows, finals = [], []
    for maximize, sign in ((True, 1.), (False, -1.)):
        state = CadGeometryState(initial, grid.x, grid.y)
        ls = als.LevelSet(grid, state.phi, geometry_state=state)
        problem = GeometricProblem(radius, sign)
        cfg = als.OptimizerConfig(maximize=maximize, pseudo_time_step=.04)
        optimizer = als.AdjointLevelSetOptimizer(problem, np.ones(ls.phi.shape), cfg,
                                                 geometry_evolution=CadEvolution())
        final = optimizer.run(ls, n_iter=120)
        geometry = final.geometry_state.geometry
        finals.append(geometry)
        value = integral(geometry, radius)
        relative_gap = (maximum-value)/maximum
        assert abs(relative_gap) < 2e-5, relative_gap
        updated = [r for r in optimizer.history if r['updated']]
        assert all(r['objective_gain'] > -1e-12 for r in updated)
        assert all(r['pseudo_time_step_used'] == .04 and r['backtracks'] == 0 for r in updated)
        assert all(r['linearization_matches_final_geometry'] for r in updated)
        name = 'maximize' if maximize else 'minimize_negative'
        (OUT/f'{name}_geometry.json').write_text(json.dumps(final.geometry_state.to_dict()), encoding='utf-8')
        rows.append(dict(mode=name, initial_value=integral(initial, radius), final_value=value,
            analytic_maximum=maximum, relative_optimum_gap=relative_gap,
            solves=problem.calls, stop_reason=optimizer.stop_reason, history=optimizer.history))
    assert finals[0].equals_exact(finals[1], 0.)
    sources = ('adjoint_levelset.py', 'eaopt/geometry/evolution.py', 'eaopt/geometry/state.py',
               'scripts/check_cad_optimization.py')
    report = dict(passed=True, scope='shared optimizer with an independent exact geometric integral',
        electromagnetic_optimization_verified=False, production_default_path_changed=False,
        topology_continuation_verified=False, full_goal_achieved=False, cases=rows,
        source_hashes={p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sources})
    (OUT/'results.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), constrained_layout=True)
    history = rows[0]['history']
    values = [rows[0]['initial_value']]+[r['f_after'] for r in history if r['updated']]
    axes[0].semilogy(np.arange(len(values)), (maximum-np.asarray(values))/maximum)
    axes[0].set(xlabel='Committed update', ylabel='Relative gap to analytic optimum',
                title='Exact geometric integral (not an EM FoM)')
    axes[0].grid(True, alpha=.3)
    for geometry, label in ((initial, 'Initial'), (finals[0], 'Optimized')):
        xy = np.asarray(geometry.exterior.coords)
        axes[1].plot(xy[:, 0], xy[:, 1], label=label)
    angle = np.linspace(0, 2*np.pi, 400)
    axes[1].plot(radius*np.cos(angle), radius*np.sin(angle), '--', label='Analytic circle')
    axes[1].set(xlabel='x (mm)', ylabel='y (mm)', aspect='equal', title='Shared optimizer, retained CAD')
    axes[1].legend()
    fig.savefig(OUT/'convergence.png', dpi=160)
    plt.close(fig)
    print(json.dumps([{k: v for k, v in row.items() if k != 'history'} for row in rows], indent=2))


if __name__ == '__main__':
    main()
