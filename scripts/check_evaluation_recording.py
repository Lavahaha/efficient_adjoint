"""Offline regression checks: python scripts/check_evaluation_recording.py.

Uses unittest and the normal numerical dependencies. No CST or pytest needed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.runner import _run_output_dir, _save_evaluation
from eaopt import plotting


class Problem:
    fom_kind = "abs"
    frequencies = ()

    def __init__(self, output_dir, values):
        self.output_dir = output_dir
        self.values = iter(values)
        self.calls = 0
        self.cfg = CaseConfig.from_yaml(ROOT / "configs/coupler.yaml")
        self.log = lambda _: None
        axis = np.arange(3, dtype=float)
        self.fields = als.FieldBundle(axis, axis, np.ones((3, 3, 3)),
                                      np.zeros((3, 3, 3)))

    def forward(self, ls):
        value = next(self.values)
        eval_id = self.calls
        self.calls += 1
        return als.ForwardResult(value, self.fields, {
            "eval_id": eval_id, "objective_value": abs(value),
            "s_params": {(3, 1): value}, "frequency_data": {},
        })

    def objective_value(self, fwd):
        return fwd.extra["objective_value"]

    def e_field_for_plot(self, eval_id, box):
        # Export must happen before another solve replaces the active fields.
        assert eval_id == self.calls - 1
        return self.fields.x, self.fields.y, np.full((3, 3), eval_id + 1.), 0.


class RecordingChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ls = als.LevelSet(als.Grid2D(3, 3, 1.), np.full((3, 3), -.5))

    def run_optimizer(self, values, iterations, maximize=True):
        problem = Problem(self.root, values)
        opt = als.AdjointLevelSetOptimizer(
            problem, np.ones((3, 3)),
            als.OptimizerConfig(pseudo_time_step=16., maximize=maximize))
        opt.velocity = lambda ls, fwd: (np.ones((3, 3)), abs(fwd.s), None, None)
        states = []

        def record(ls, fwd, details):
            _save_evaluation(problem, ls, fwd, details, problem.cfg.design_region.box)

        # Keep this test independent of the installed reinitialization backend.
        with patch.object(als, "advect_levelset", side_effect=lambda phi, *a, **kw: phi - .01) as advect, \
                patch.object(als, "apply_constraints"), \
                patch.object(plotting, "adjoint_levelset_state_figure"):
            opt.run(self.ls, iterations, evaluation_callback=record,
                    state_callback=lambda it, ls, fwd: states.append((it, fwd.extra["eval_id"])))
        self.assertEqual(advect.call_count, iterations)
        self.assertEqual([call.args[2] for call in advect.call_args_list], [16.] * iterations)
        for row in opt.history:
            self.assertEqual(row["pseudo_time_step_used"], 16.)
            self.assertEqual(row["pseudo_time_step_next"], 16.)
            self.assertEqual(row["backtracks"], 0)
        rows = [json.loads(line) for line in
                (self.root / "optimization_evaluations.jsonl").read_text().splitlines()]
        return opt, states, rows

    def test_worse_fom_is_committed_and_saved_before_next_solve(self):
        opt, states, rows = self.run_optimizer([1., .5, .4], 2)
        self.assertEqual([r["cst_eval_id"] for r in rows], [0, 1, 2])
        self.assertEqual([r["accepted"] for r in rows], [None, True, True])
        self.assertEqual(rows[1]["f_before"], 1.)
        self.assertEqual(rows[2]["f_before"], .5)
        self.assertEqual(rows[2]["backtracks"], 0)
        self.assertEqual(states, [(0, 0), (1, 1), (2, 2)])
        self.assertEqual(opt.history[-1]["f_after"], .4)
        self.assertEqual(opt.stop_reason, "max_iterations")
        self.assertTrue(opt.history[0]["updated"])
        for row in rows:
            with np.load(self.root / row["snapshot"]) as data:
                self.assertEqual(int(data["cst_eval_id"]), row["cst_eval_id"])
                self.assertTrue(np.all(data["e_mag"] == row["cst_eval_id"] + 1))
                np.testing.assert_array_equal(data["e_forward"], np.ones((3, 3, 3)))

    def test_equal_fom_is_committed_without_restore_solves(self):
        opt, states, rows = self.run_optimizer([1., 1., 1.], 2)
        self.assertTrue(rows[1]["accepted"])
        self.assertEqual(rows[1]["delta_f"], 0.)
        self.assertEqual(states, [(0, 0), (1, 1), (2, 2)])
        self.assertEqual([r["phase"] for r in rows], ["initial", "trial", "trial"])
        self.assertEqual(len(list(self.root.glob("iter_*/evaluation_state.npz"))), 3)
        self.assertTrue(all(r["updated"] for r in opt.history))

    def test_minimization_records_the_right_decision(self):
        _, _, rows = self.run_optimizer([1., 1.5, .5], 2, maximize=False)
        self.assertTrue(rows[1]["accepted"])
        self.assertTrue(rows[2]["accepted"])
        self.assertEqual(rows[1]["objective_gain"], -.5)
        self.assertEqual(rows[2]["objective_gain"], 1.)

    def test_non_finite_objective_still_aborts(self):
        problem = Problem(self.root, [1., float("nan")])
        opt = als.AdjointLevelSetOptimizer(problem, np.ones((3, 3)),
                                           als.OptimizerConfig(pseudo_time_step=16.))
        opt.velocity = lambda ls, fwd: (np.ones((3, 3)), abs(fwd.s), None, None)
        with patch.object(als, "advect_levelset", return_value=self.ls.phi - .01), \
                patch.object(als, "apply_constraints"):
            with self.assertRaisesRegex(RuntimeError, "non-finite objective"):
                opt.run(self.ls, 1)
        self.assertEqual(opt.history, [])

    def test_multifrequency_samples_survive_later_solves(self):
        problem = Problem(self.root, [1.])
        problem.cfg = CaseConfig.from_yaml(ROOT / "configs/divider_multifreq.yaml")
        problem.frequencies = (4., 6.)
        fwd = problem.forward(self.ls)
        fwd.extra["frequency_data"] = {
            f: ({(2, 1): f / 10.}, problem.fields, 0., als.EPS0) for f in problem.frequencies}
        with patch.object(plotting, "adjoint_levelset_state_figure"):
            row = _save_evaluation(problem, self.ls, fwd,
                                   dict(phase="initial", it=None, accepted=None),
                                   problem.cfg.design_region.box)
        self.assertEqual(set(row["s_params_db"]), {"S21@4GHz", "S21@6GHz"})
        with np.load(self.root / row["snapshot"]) as data:
            self.assertEqual(float(data["frequency_0_ghz"]), 4.)
            self.assertEqual(float(data["frequency_1_ghz"]), 6.)
            self.assertIn("frequency_1_H", data.files)

    def test_gradient_check_records_each_perturbation(self):
        problem = Problem(self.root, [1., 1.1, .9])
        phases = []
        with patch.object(als, "problem_gradient", return_value=np.zeros(0)):
            als.gradient_check(problem, self.ls, deltas=(.1,),
                               evaluation_callback=lambda ls, fwd, row: phases.append(row["phase"]))
        self.assertEqual(phases, ["gradient_base", "gradient_plus", "gradient_minus"])

    def test_repeated_runs_leave_existing_files_untouched(self):
        requested = self.root / "coupler"
        first = _run_output_dir(requested)
        sentinel = first / "adjoint_levelset_history.jsonl"
        sentinel.write_text("original run\n")
        second = _run_output_dir(requested)
        third = _run_output_dir(requested)
        self.assertEqual(sentinel.read_text(), "original run\n")
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(second.parent, first)

    def test_evaluation_plot_handles_multifrequency_records(self):
        cfg = CaseConfig.from_yaml(ROOT / "configs/divider_multifreq.yaml")
        target = self.root / "evaluations.png"
        plotting.adjoint_levelset_sparams_figure(cfg, [
            {"cst_eval_id": 0, "s_params_db": {"S21@4GHz": -4., "S21@6GHz": -5.}},
            {"cst_eval_id": 1, "s_params_db": {"S21@4GHz": -3., "S21@6GHz": -4.}},
        ], target, x_key="cst_eval_id")
        self.assertGreater(target.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
