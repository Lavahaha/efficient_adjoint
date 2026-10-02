"""优化闭环测试：内联 stub 求解器（记录调用 + 回放给定 FoM/场）。

不依赖 CST。这里锁的是 pipeline 自己的行为——调用顺序、产物落盘
（history.jsonl / iter_NNN.png / ls_phi.npz）、收敛提前退出、异常时
close() 仍被调用（CST 会话必须收尾）。
"""

import json

import numpy as np
import pytest

from eaopt.adjoint.fields import FieldGrid
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver.base import Solution, SolverInterface

from conftest import make_compact_cfg


def _field(cfg, value: complex) -> FieldGrid:
    """覆盖设计区 ±1 mm 的常量场（stub 用，值本身不重要，够插值即可）。"""
    box = cfg.design_region.box
    spacing = (0.25, 0.25, 0.5)
    origin = (box.x[0] - 1.0, box.y[0] - 1.0, -0.5)
    nx = int(round((box.width + 2.0) / spacing[0])) + 1
    ny = int(round((box.height + 2.0) / spacing[1])) + 1
    data = np.full((nx, ny, 3, 3), value, dtype=complex)
    return FieldGrid(origin=origin, spacing=spacing, data=data)


class StubSolver(SolverInterface):
    """记录调用序列的求解器替身：第 it 轮正向 FoM = foms[it]。"""

    def __init__(self, cfg, *, foms=(0.2,), fail_at=None):
        self.cfg = cfg
        self.foms = list(foms)
        self.fail_at = fail_at
        self.calls: list = []
        self.closed = False
        self._it = 0

    def begin_iteration(self, iteration):
        self._it = int(iteration)
        self.calls.append(("begin_iteration", self._it))

    def build_model(self, movable, fixed=None):
        self.calls.append(("build_model", self._it, len(movable)))

    def solve_forward(self):
        self.calls.append(("solve_forward", self._it))
        if self.fail_at == self._it:
            raise RuntimeError("stub 正向仿真失败")
        return self._solution(self.foms[min(self._it, len(self.foms) - 1)])

    def solve_backward(self):
        self.calls.append(("solve_backward", self._it))
        return self._solution(0.1 * self.foms[0])

    def close(self):
        self.closed = True
        self.calls.append(("close",))

    def _solution(self, fom: float) -> Solution:
        # FoM = |S_{to,from}|，直接按目标值给 S 参数
        return Solution(s_params={(3, 1): fom + 0j, (2, 1): 0.01 * fom},
                        e_field=_field(self.cfg, 1.0 + 0.0j),
                        h_field=_field(self.cfg, 0.5j), pin=0.5)


def test_call_sequence_per_iteration(tmp_path):
    """每轮：告知轮次 → 重建几何 → 正向 → 后向；结束后 close 一次。"""
    cfg = make_compact_cfg(tmp_path, max_iterations=3)
    solver = StubSolver(cfg, foms=(0.2, 0.3, 0.4))
    OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()

    per_iter = [c for c in solver.calls if c[0] != "close"]
    assert [c[0] for c in per_iter] == ["begin_iteration", "build_model",
                                        "solve_forward", "solve_backward"] * 3
    assert [c[1] for c in per_iter if c[0] == "begin_iteration"] == [0, 1, 2]
    assert solver.calls[-1] == ("close",) and solver.closed


def test_artifacts_are_written(tmp_path):
    cfg = make_compact_cfg(tmp_path, max_iterations=2)
    solver = StubSolver(cfg, foms=(0.2, 0.3))
    history = OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()

    outdir = tmp_path / "results"
    lines = (outdir / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(history.records) == 2
    rec = json.loads(lines[0])
    assert set(rec) >= {"iteration", "fom", "s31_db", "s21_db", "time_s"}
    assert [r["fom"] for r in history.records] == [0.2, 0.3]
    for it in (0, 1):
        assert (outdir / f"iter_{it:03d}.png").exists()
    assert (outdir / "fom.png").exists()


def test_ls_phi_snapshot_is_the_phi_that_made_the_shape(tmp_path):
    """φ 快照必须是**本轮**形状的来源（写早不写晚：更新后写就对不上了）。"""
    from eaopt import artifacts

    cfg = make_compact_cfg(tmp_path, max_iterations=2)
    solver = StubSolver(cfg, foms=(0.2, 0.3))
    ls = make_level_set(cfg)
    phi0 = ls.phi.copy()
    OptimizerPipeline(cfg, solver, ls).run()

    snap = artifacts.load_ls_phi(tmp_path / "results" / "iter_000" / "ls_phi.npz")
    assert np.array_equal(snap["phi"], phi0)           # 第 0 轮 = 初始 φ
    assert snap["phi"].shape == (ls.nx, ls.ny)
    last = artifacts.load_ls_phi(tmp_path / "results" / "iter_001" / "ls_phi.npz")
    assert last["dx"] == ls.dx


def test_convergence_breaks_before_backward_sim(tmp_path):
    """FoM 在窗口内不再变化 → 提前结束，且不再跑后向仿真。"""
    cfg = make_compact_cfg(tmp_path, max_iterations=10)
    cfg.optimizer.convergence_window = 2
    cfg.optimizer.fom_tolerance = 1e-3
    solver = StubSolver(cfg, foms=(0.25,))             # 恒定 FoM
    history = OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()

    assert history.stop_reason == "converged" and history.converged
    assert len(history.records) == 3                   # window+1
    assert ("solve_backward", 2) not in solver.calls   # 最后一轮没跑后向
    assert ("solve_backward", 1) in solver.calls


def test_close_is_called_even_when_a_sim_fails(tmp_path):
    """异常退出也必须收尾（否则下一次会附接到状态不明的 CST 实例）。"""
    cfg = make_compact_cfg(tmp_path, max_iterations=5)
    solver = StubSolver(cfg, fail_at=1)
    with pytest.raises(RuntimeError, match="stub"):
        OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()
    assert solver.closed
