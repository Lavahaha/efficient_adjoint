"""CST scheduling regressions with project stubs; never starts CST."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from eaopt.config import CaseConfig

try:
    from eaopt.solver import cst
except SystemExit:
    cst = None


@unittest.skipIf(cst is None, "CST Python link is not installed")
class ParallelSolverTests(unittest.TestCase):
    def projects(self, events, states=None):
        projects = {}
        started = set()
        for tag in ("fwd", "bwd"):
            def start(tag=tag):
                events.append(("start", tag))
                started.add(tag)

            def running(tag=tag):
                if tag not in started:
                    return False
                events.append(("poll", tag))
                return False

            projects[tag] = Mock()
            model = projects[tag].model3d
            model.start_solver.side_effect = start
            model.is_solver_running.side_effect = running
            model.get_solver_run_info.return_value = {
                "state": (states or {}).get(tag, "SUCCESS"), "message": "test"}
        return projects

    def solver(self, name="coupler", **kwargs):
        cfg = CaseConfig.from_yaml(Path(__file__).resolve().parents[1] / f"configs/{name}.yaml")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        cfg.output.dir = temp.name
        return cst.CstSolver(cfg, log=lambda _: None, **kwargs)

    def test_both_start_before_waiting_or_reading_results(self):
        events = []
        projects = self.projects(events)
        projects["fwd"].model3d.is_solver_running.side_effect = [False, True, False]
        with patch.object(cst.time, "sleep") as sleep:
            seconds = cst._run_solvers(projects, lambda _: None)
        self.assertEqual(set(seconds), {"fwd", "bwd"})
        self.assertEqual(events[:2], [("start", "fwd"), ("start", "bwd")])
        sleep.assert_called_once_with(.5)
        for project in projects.values():
            project.model3d.run_solver.assert_not_called()
            project.model3d.start_solver.assert_called_once_with()

    def test_failure_stops_other_running_solver(self):
        projects = self.projects([], {"fwd": "ERROR"})
        projects["bwd"].model3d.is_solver_running.side_effect = [False, True, True]
        with self.assertRaisesRegex(RuntimeError, "fwd.*ERROR"):
            cst._run_solvers(projects, lambda _: None)
        projects["bwd"].model3d.abort_solver.assert_called_once_with()

    def test_second_start_failure_stops_first_solver(self):
        projects = self.projects([])
        projects["fwd"].model3d.is_solver_running.side_effect = [False, True]
        projects["bwd"].model3d.start_solver.side_effect = RuntimeError("no license")
        with self.assertRaisesRegex(RuntimeError, "no license"):
            cst._run_solvers(projects, lambda _: None)
        projects["fwd"].model3d.abort_solver.assert_called_once_with()

    def test_existing_run_is_not_restarted_or_aborted(self):
        projects = self.projects([])
        projects["bwd"].model3d.is_solver_running.side_effect = None
        projects["bwd"].model3d.is_solver_running.return_value = True
        with self.assertRaisesRegex(RuntimeError, "bwd"):
            cst._run_solvers(projects, lambda _: None)
        for project in projects.values():
            project.model3d.start_solver.assert_not_called()
            project.model3d.abort_solver.assert_not_called()

    def test_opened_projects_stay_open_until_close(self):
        solver = self.solver()
        solver.de = Mock()
        projects = [Mock(), Mock()]
        for tag in solver.project_tags:
            path = solver.project_path(tag)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        with patch.object(cst, "_open_or_reuse", side_effect=projects) as opened, \
                patch.object(solver, "_check_project"), patch.object(cst, "_save") as save:
            self.assertIs(solver._open("fwd"), projects[0])
            self.assertIs(solver._open("bwd"), projects[1])
            self.assertIs(solver._open("fwd"), projects[0])
            self.assertEqual(opened.call_count, 2)
            for project in projects:
                project.close.assert_not_called()
            solver.close()
            self.assertEqual(save.call_count, 2)
        self.assertFalse(solver.projects)
        for project in projects:
            project.close.assert_called_once_with()

    def test_initializing_both_projects_keeps_first_open(self):
        solver = self.solver(auto_init=True)
        solver.de = Mock()
        projects = [Mock(), Mock()]
        solver.de.new_mws.side_effect = projects
        with patch.object(cst, "_apply_blocks"), patch.object(cst, "_save"):
            solver._ensure_projects()
        self.assertEqual(solver.projects, dict(zip(("fwd", "bwd"), projects)))
        for project in projects:
            project.close.assert_not_called()

    def test_backward_result_reused_and_cleared_when_geometry_changes(self):
        solver = self.solver()
        solver.projects = self.projects([])
        solver.begin_iteration(0)
        forward, backward = object(), object()

        def collect(tag, seconds):
            solver._solved_at[tag] = solver._it
            return forward if tag == "fwd" else backward

        with patch.object(solver, "_check_project"), \
                patch.object(cst, "_run_solvers", return_value={"fwd": 1., "bwd": 2.}) as run, \
                patch.object(solver, "_collect_solution", side_effect=collect) as read:
            self.assertIs(solver.solve_forward(), forward)
            self.assertIs(solver.solve_backward(), backward)
            self.assertIs(solver.solve_backward(), backward)
            run.assert_called_once()
            self.assertEqual([call.args[0] for call in read.call_args_list], ["fwd", "bwd"])
        polygon = np.asarray([*solver.cfg.initial_metal[0].vertices,
                              solver.cfg.initial_metal[0].vertices[0]])
        solver.begin_iteration(1)
        with patch.object(cst, "_clear_results"), patch.object(cst, "_update_design_region"):
            solver.build_model([polygon])
        self.assertIsNone(solver._backward_solution)
        self.assertFalse(solver._solved_at)
        with patch.object(solver, "_solve", return_value="new result") as run:
            self.assertEqual(solver.solve_backward(), "new result")
            run.assert_called_once_with("bwd")

    def test_reflection_runs_only_forward(self):
        solver = self.solver("antenna")
        with patch.object(solver, "_solve", return_value="forward") as run, \
                patch.object(cst, "_run_solvers") as parallel:
            self.assertEqual(solver.solve_forward(), "forward")
            run.assert_called_once_with("fwd")
            parallel.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "reflection"):
                solver.solve_backward()

    def test_fd_project_check_keeps_excitation_validation(self):
        solver = self.solver()
        solver.setup = replace(solver.setup, solver_type="fd")
        for tag in solver.project_tags:
            port = solver.setup.stimulus(tag, solver.cfg.objective)
            for actual_port, mode in ((port, "1"), (99, "1"), (port, "All")):
                with self.subTest(tag=tag, port=actual_port, mode=mode):
                    project = Mock()
                    path = Path(solver.cfg.output.dir) / f"cst_work/solver_{tag}.txt"

                    def execute(code):
                        result = "HF Frequency Domain\n"
                        if "GetStimulationPort" in code:
                            self.assertLess(code.index("GetStimulationMode"), code.index("Open "))
                            result += f"{actual_port}\n{mode}\n"
                        path.write_text(result, encoding="utf-8")

                    project.model3d._execute_vba_code.side_effect = execute
                    if actual_port == port and mode == "1":
                        solver._check_project(tag, project)
                    else:
                        with self.assertRaisesRegex(ValueError, "FD excitation mismatch"):
                            solver._check_project(tag, project)

    def test_fd_project_check_compatibility_and_error_propagation(self):
        solver = self.solver()
        solver.setup = replace(solver.setup, solver_type="fd")
        for tag in solver.project_tags:
            for error in ("Method or property not found.", "CST connection lost"):
                for setter_fails in (False, True):
                    with self.subTest(tag=tag, error=error, setter_fails=setter_fails):
                        project = Mock()
                        path = Path(solver.cfg.output.dir) / f"cst_work/solver_{tag}.txt"

                        def execute(code):
                            if "GetStimulationPort" in code:
                                raise RuntimeError(error)
                            if "FDSolver.Stimulation" in code:
                                if setter_fails:
                                    raise RuntimeError("Cannot set excitation")
                            else:
                                path.write_text("HF Frequency Domain\n", encoding="utf-8")

                        project.model3d._execute_vba_code.side_effect = execute
                        if error == "CST connection lost" or setter_fails:
                            message = error if error == "CST connection lost" else "Cannot set excitation"
                            with self.assertRaisesRegex(RuntimeError, message):
                                solver._check_project(tag, project)
                        else:
                            solver._check_project(tag, project)
                        calls = project.model3d._execute_vba_code.call_args_list
                        if error == "CST connection lost":
                            self.assertEqual(len(calls), 2)
                        else:
                            port = solver.setup.stimulus(tag, solver.cfg.objective)
                            self.assertIn(f'FDSolver.Stimulation "{port}", "1"', calls[-1].args[0])

    def test_parallel_collection_checks_meshes_and_preserves_both_metadata_tags(self):
        for matching in (True, False):
            with self.subTest(matching_meshes=matching):
                solver = self.solver()
                solver.setup = replace(solver.setup, solver_type="fd")
                solver.projects = self.projects([])
                solver.begin_iteration(0)
                for tag in solver.project_tags:
                    folder = solver.project_path(tag).with_suffix("") / "Result"
                    folder.mkdir(parents=True)
                    for name in ("moegeom.tet", "topot.tet", "bc3d.tet"):
                        (folder / name).write_bytes(b"same" if matching else tag.encode())
                with patch.object(solver, "_check_project"), patch.object(cst, "_save"), \
                        patch.object(cst, "_export", return_value=None), \
                        patch.object(cst.R, "read_s_params", return_value={(3, 1): .1j}), \
                        patch.object(cst.R, "read_incident_power", return_value=.5):
                    if matching:
                        self.assertEqual(solver.solve_forward().extra["tag"], "fwd")
                        self.assertEqual(solver.solve_backward().extra["tag"], "bwd")
                        meta = json.loads((Path(solver.cfg.output.dir) / "iter_000/meta.json")
                                          .read_text(encoding="utf-8"))
                        self.assertEqual(set(meta["tags"]), {"fwd", "bwd"})
                    else:
                        with self.assertRaisesRegex(RuntimeError, "meshes differ"):
                            solver.solve_forward()
                        self.assertIsNone(solver._backward_solution)


if __name__ == "__main__":
    unittest.main()
