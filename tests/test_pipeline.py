"""优化闭环测试：内联 stub 求解器（记录调用 + 回放给定 FoM/场）。

不依赖 CST。这里锁的是 pipeline 自己的行为——调用顺序、产物落盘
（history.jsonl / iter_NNN.png / ls_phi.npz）、收敛提前退出、异常时
close() 仍被调用（CST 会话必须收尾）。
"""

import json

import numpy as np
import pytest

from eaopt import artifacts
from eaopt.adjoint.fields import FieldGrid
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver.base import Solution, SolverInterface

from conftest import LINE_BOTTOM, make_compact_cfg


def _field(cfg, value: complex) -> FieldGrid:
    """覆盖设计区 ±1 mm 的常量场（stub 用，值本身不重要，够插值即可）。"""
    box = cfg.design_region.box
    spacing = (0.25, 0.25, 0.5)
    origin = (box.x[0] - 1.0, box.y[0] - 1.0, -0.5)
    nx = int(round((box.width + 2.0) / spacing[0])) + 1
    ny = int(round((box.height + 2.0) / spacing[1])) + 1
    data = np.full((nx, ny, 3, 3), value, dtype=complex)
    return FieldGrid(origin=origin, spacing=spacing, data=data)


def _field_on_lattice(cfg, value: complex, phase: float = 0.0, *,
                      z0: float = -0.5, dz: float = 0.5, nz: int = 3) -> FieldGrid:
    """格距 = 水准集网格、**原点错开**的场（nodes 方案的硬前提：零插值取场）。

    每个分量 = value·e^{i·phase·ix}。phase 不是装饰：δp = Re[−2jω·(…)]
    只取复场点积的虚部，全实的常量前向/后向场导数为零、边界根本不会动。

    x/y 原点偏移 (-1.0, -2.0) = 10/20 格：真实导出网格是整个包围盒（原点
    x −5.6 / y −6.9），与设计区原点差着几十个节点。**场下标 ≠ 网格下标**，
    把用错了的那一处照出来（否则掩膜/延拓会静默错位）。

    缺省 z 面 −0.5/0.0/0.5；要复现真实 z 网格用 z0=-0.6985, dz=0.1（此时
    z=0 附近的面是 +0.0015，落在 35 µm 金属体内）。
    """
    box = cfg.design_region.box
    dx = cfg.design_region.grid_step_mm
    off = (10 * dx, 20 * dx)
    nx = int(round((box.width + off[0]) / dx)) + 1
    ny = int(round((box.height + off[1]) / dx)) + 1
    data = np.empty((nx, ny, nz, 3), dtype=complex)
    data[:] = value * np.exp(1j * phase * np.arange(nx))[:, None, None, None]
    return FieldGrid(origin=(box.x[0] - off[0], box.y[0] - off[1], z0),
                     spacing=(dx, dx, dz), data=data)


class StubSolver(SolverInterface):
    """记录调用序列的求解器替身：第 it 轮正向 FoM = foms[it]。"""

    def __init__(self, cfg, *, foms=(0.2,), fail_at=None):
        self.cfg = cfg
        self.foms = list(foms)
        self.fail_at = fail_at
        self.calls: list = []
        self.shapes: list = []      # 每轮 build_model 收到的可动几何
        self.closed = False
        self._it = 0

    def begin_iteration(self, iteration):
        self._it = int(iteration)
        self.calls.append(("begin_iteration", self._it))

    def build_model(self, movable, fixed=None):
        self.calls.append(("build_model", self._it, len(movable)))
        self.shapes.append([np.asarray(m, dtype=float).copy() for m in movable])

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


class LatticeStubSolver(StubSolver):
    """场导出网格与水准集网格同源的 stub（nodes 方案的硬前提）。

    z 网格按真实导出复现（原点 −0.6985、步长 0.1）：z=0 附近的面是
    +0.0015 mm，落在 35 µm 金属体内。
    """

    def __init__(self, cfg, *, z0=-0.6985, dz=0.1, nz=8, **kw):
        super().__init__(cfg, **kw)
        self._zkw = dict(z0=z0, dz=dz, nz=nz)

    def _solution(self, fom: float) -> Solution:
        return Solution(s_params={(3, 1): fom + 0j, (2, 1): 0.01 * fom},
                        e_field=_field_on_lattice(self.cfg, 1.0, phase=0.9, **self._zkw),
                        h_field=_field_on_lattice(self.cfg, 0.5j, phase=0.4, **self._zkw),
                        pin=0.5)


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


# ---------------------------------------------------------------------- #
# nodes 方案（采样点 = 边界网格节点，零插值取场）
# ---------------------------------------------------------------------- #
def test_nodes_scheme_runs_on_the_unified_grid(tmp_path):
    """nodes 全链路：建出来的是节点折线、按索引取场、一轮位移 ≤ 1 格。

    场数据与 φ 网格同源（LatticeStubSolver）——不同源时 node_field_indices
    会直接报错，那正是"导出步长 = 网格步长"这条不变量的守卫。
    """
    cfg = make_compact_cfg(tmp_path, max_iterations=2)
    cfg.sampling.scheme = "nodes"
    cfg.sampling.field_z_mm = -0.1          # 金属下方（会吸附到 z=-0.0985）
    cfg.level_set.reinit_every = 100        # 隔离"位移 ≤ 1 格"这条不变量
    solver = LatticeStubSolver(cfg, foms=(0.2, 0.3))
    OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()

    # 1) 交给 CST 的几何是**网格节点折线**（采样点连点成轮廓）
    box, dx = cfg.design_region.box, cfg.design_region.grid_step_mm
    assert len(solver.shapes) == 2
    for polys in solver.shapes:
        assert polys
        p = np.vstack(polys)
        r = (p - np.array([box.x[0], box.y[0]])) / dx
        assert np.abs(r - np.rint(r)).max() < 1e-9
        assert p[:, 1].max() < LINE_BOTTOM - 1e-9   # 固定直通线不参与

    # 2) φ 是距离函数 → |Δφ| ≈ 边界位移；一轮最多 step_cells 格
    d = tmp_path / "results"
    phi = [artifacts.load_ls_phi(d / f"iter_{it:03d}" / "ls_phi.npz")["phi"]
           for it in (0, 1)]
    dphi = np.abs(phi[1] - phi[0])
    step = cfg.optimizer.step_cells * dx
    assert dphi.max() <= step + 1e-6
    assert dphi.max() > 0.2 * step          # 真的在动，不是空转


def test_nodes_scheme_rejects_a_foreign_field_grid(tmp_path):
    """导出网格 ≠ φ 网格时必须报错，不能静默插值（否则收益悄悄还回去）。"""
    cfg = make_compact_cfg(tmp_path, max_iterations=2)
    cfg.sampling.scheme = "nodes"
    solver = StubSolver(cfg, foms=(0.2, 0.3))          # 步长 0.25 的场
    with pytest.raises(ValueError, match="不在场导出网格节点上"):
        OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()
    assert solver.closed


def test_nodes_scheme_rejects_a_sample_plane_inside_the_metal(tmp_path):
    """采样面吸附进金属体内 → 报错。PEC 里 E/H≈0，δp 会恒为 0、优化空转。

    field_z_mm=0.0（旧配置的写法）在 0.1 mm 导出网格上正好吸到 z=+0.0015：
    这个面落在 35 µm 金属内部，而 0.2 mm 网格时它跨过金属、反而没问题。
    """
    cfg = make_compact_cfg(tmp_path, max_iterations=1)
    cfg.sampling.scheme = "nodes"
    cfg.sampling.field_z_mm = 0.0
    solver = LatticeStubSolver(cfg, foms=(0.2,))
    with pytest.raises(ValueError, match="落在金属体内"):
        OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()
    assert solver.closed
