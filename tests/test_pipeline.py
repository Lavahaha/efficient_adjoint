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


def _export_field(cfg, value: complex, phase: float = 0.0, *,
                  x0: float = -5.6, y0: float = -3.55, h: float = 0.1,
                  z0: float = -0.6985, dz: float = 0.1, nz: int = 8,
                  margin: float = 1.0) -> FieldGrid:
    """按**给定原点/步长**造一张覆盖设计区的解析场（默认 = 服务器实测的导出网格）。

    默认原点 (−5.6, −3.55)：CST 的导出网格原点由**包围盒**定，设计区
    y=−2.6 相对它差**半格**（y 轴 −3.55、步长 0.1）——旧 nodes 方案在这里
    必抛"采样点不在场导出网格节点上"，本方案按坐标用 WLS 取场，照样能跑。

    每个分量 = value·e^{i·phase·ix}。phase 不是装饰：δp 只取复场点积的
    虚部，全实的常量前向/后向场导数为零、边界根本不会动。

    z 面 −0.6985/步长 0.1 也是实测值：z=0 附近的面是 +0.0015，落在
    35 µm 金属体内（采样面吸附到那里必须报错）。
    """
    box = cfg.design_region.box
    nx = int(np.ceil((box.x[1] + margin - x0) / h)) + 1
    ny = int(np.ceil((box.y[1] + margin - y0) / h)) + 1
    data = np.empty((nx, ny, nz, 3), dtype=complex)
    data[:] = value * np.exp(1j * phase * np.arange(nx))[:, None, None, None]
    return FieldGrid(origin=(x0, y0, z0), spacing=(h, h, dz), data=data)


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


class OffsetGridStubSolver(StubSolver):
    """场导出网格 = 服务器实测那张（原点 −5.6/−3.55、步长 0.1），与设计区错半格。"""

    def _solution(self, fom: float) -> Solution:
        return Solution(s_params={(3, 1): fom + 0j, (2, 1): 0.01 * fom},
                        e_field=_export_field(self.cfg, 1.0, phase=0.9),
                        h_field=_export_field(self.cfg, 0.5j, phase=0.4),
                        pin=0.5)


class MismatchedGridStubSolver(OffsetGridStubSolver):
    """fwd H 导出在**另一张**网格上（x 原点差半格）——必须报错。"""

    def _solution(self, fom: float) -> Solution:
        sol = super()._solution(fom)
        sol.h_field = _export_field(self.cfg, 0.5j, phase=0.4, x0=-5.65)
        return sol


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
# intersection 方案（marching 交点 + WLS 取场 + 反距离平方回写）
# ---------------------------------------------------------------------- #
def test_intersection_scheme_accepts_a_half_cell_offset_export_grid(tmp_path):
    """**服务器上那次崩溃的回归**：导出网格与设计区差半格也要能跑。

    CST 的导出网格原点由包围盒定（实测 x −5.6 / y −3.55），设计区 y=−2.6
    相对它差半格——旧 nodes 方案（要求采样点与导出节点逐点重合）在这里
    必抛 ValueError。交点方案按坐标用 WLS 取场，原点错位多少都不影响。

    臂顶边取在 y=0.05（半格）上：交点是**亚格点**的，旧方案做不到。
    """
    cfg = make_compact_cfg(tmp_path, arm_top=0.05, max_iterations=2)
    cfg.sampling.field_z_mm = -0.1          # 金属下方（吸附到 z=-0.0985）
    cfg.level_set.reinit_every = 100        # 隔离"位移 ≤ 1 格"这条不变量
    solver = OffsetGridStubSolver(cfg, foms=(0.2, 0.3))
    OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()

    # 1) 交给 CST 的几何 = 交点折线：落在网格线上、但**不落在节点上**
    box, dx = cfg.design_region.box, cfg.design_region.grid_step_mm
    assert len(solver.shapes) == 2
    for polys in solver.shapes:
        assert polys
        assert np.vstack(polys)[:, 1].max() < LINE_BOTTOM - 1e-9  # 固定直通线不参与
    p = np.vstack(solver.shapes[0])                 # 第 0 轮 = 初始几何
    r = (p - np.array([box.x[0], box.y[0]])) / dx
    fx = np.abs(r[:, 0] - np.rint(r[:, 0]))
    fy = np.abs(r[:, 1] - np.rint(r[:, 1]))
    assert np.minimum(fx, fy).max() < 1e-9          # 每个顶点压在网格线上
    # 臂顶边（y=0.05，故意取半格）：顶点是亚格点的（旧 nodes 只能给格点）
    on_top = np.abs(p[:, 1] - 0.05) < 1e-9
    assert on_top.sum() > 5
    assert fy[on_top].min() > 0.1

    # 2) φ 是距离函数 → |Δφ| ≈ 边界位移；一轮最多 step_cells 格
    d = tmp_path / "results"
    phi = [artifacts.load_ls_phi(d / f"iter_{it:03d}" / "ls_phi.npz")["phi"]
           for it in (0, 1)]
    dphi = np.abs(phi[1] - phi[0])
    step = cfg.optimizer.step_cells * dx
    assert dphi.max() <= step + 1e-6
    assert dphi.max() > 0.2 * step          # 真的在动，不是空转


def test_contour_scheme_still_runs_for_the_server_ab(tmp_path):
    """旧 contour 路径（弧长重采样 + 三线性取场 + 最近节点回写）留作 A/B。

    服务器上要用同一份场数据比两种方案的 δp，这条路必须一直能跑。
    """
    cfg = make_compact_cfg(tmp_path, max_iterations=2)
    cfg.sampling.scheme = "contour"
    cfg.sampling.field_z_mm = -0.1
    cfg.level_set.reinit_every = 100
    solver = OffsetGridStubSolver(cfg, foms=(0.2, 0.3))
    OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()

    d = tmp_path / "results"
    phi = [artifacts.load_ls_phi(d / f"iter_{it:03d}" / "ls_phi.npz")["phi"]
           for it in (0, 1)]
    assert np.abs(phi[1] - phi[0]).max() > 0        # 真的在动


def test_intersection_scheme_rejects_mismatched_field_grids(tmp_path):
    """四个场必须导出在同一张网格上（伴随法要求 fwd/bwd 同一离散）。

    按坐标取场时网格错位既不报错、结果又是错的——必须显式查。
    """
    cfg = make_compact_cfg(tmp_path, max_iterations=1)
    cfg.sampling.field_z_mm = -0.1
    solver = MismatchedGridStubSolver(cfg, foms=(0.2,))
    with pytest.raises(ValueError, match="必须导出在同一张网格上"):
        OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()
    assert solver.closed


def test_intersection_scheme_rejects_a_sample_plane_inside_the_metal(tmp_path):
    """采样面吸附进金属体内 → 报错。PEC 里 E/H≈0，δp 会恒为 0、优化空转。

    field_z_mm=0.0（旧配置的写法）在 0.1 mm 导出网格上正好吸到 z=+0.0015：
    这个面落在 35 µm 金属内部，而 0.2 mm 网格时它跨过金属、反而没问题。
    """
    cfg = make_compact_cfg(tmp_path, max_iterations=1)
    cfg.sampling.field_z_mm = 0.0
    solver = OffsetGridStubSolver(cfg, foms=(0.2,))
    with pytest.raises(ValueError, match="落在金属体内"):
        OptimizerPipeline(cfg, solver, make_level_set(cfg)).run()
    assert solver.closed
