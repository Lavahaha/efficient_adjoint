"""连通性硬约束（论文 III-B："the microstrip lines are always connected"）。

约束的语义：可动金属必须**始终是一块**，且贴着初始那几个"穿出设计区接
固定馈线"的锚点。破了就按最短路径桥接、冻结桥接节点（用户拍板的语义：
不整轮回滚，而是打补丁）。

这里用功分器的真实初始 φ 做实验：拿剪刀把 Y 形剪断（把一段 φ 置成空气），
断言守卫能补回来、补回来的是**细桥**（不是把缺口填成实心）、冻结掩膜非空、
且冻结节点确实被速度掩膜挡住了。
"""

import dataclasses
from pathlib import Path

import numpy as np
import pytest
from matplotlib.path import Path as MplPath
from scipy import ndimage

from eaopt.adjoint.fields import FieldGrid
from eaopt.config import CaseConfig
from eaopt.geometry.contour import close_open_contours
from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import (ConnectivityGuard, anchor_runs,
                                        build_velocity_mask)
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver import cst_model_divider as D
from eaopt.solver.base import Solution, SolverInterface

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "divider.yaml"


@pytest.fixture(scope="module")
def cfg() -> CaseConfig:
    return CaseConfig.from_yaml(CONFIG)


@pytest.fixture()
def ls(cfg) -> LevelSet2D:
    return make_level_set(cfg)


def _cut(ls: LevelSet2D, x0: float, x1: float, y0: float, y1: float) -> None:
    """把矩形 [x0,x1]×[y0,y1] 内的金属改成空气（= 剪刀）。"""
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    ls.phi[(X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)] = 1.0


def _n_components(ls: LevelSet2D) -> int:
    return int(ndimage.label(ls.phi < 0, structure=np.ones((3, 3)))[1])


# =========================================================================== #
# 锚点推导
# =========================================================================== #
def test_anchor_runs_finds_the_three_feed_crossings(ls):
    """锚点 = 金属在设计区边界上的连续段：左界输入段 + 右界两个臂端。"""
    runs = anchor_runs(ls)
    assert len(runs) == 3
    spans = sorted((round(float(a[:, 0].mean()), 3),
                    round(float(a[:, 1].min()), 3),
                    round(float(a[:, 1].max()), 3)) for a in runs)
    assert spans == [(6.0, -1.0, 1.0),          # 左界：输入段（宽 2）
                     (33.7, -4.5, -3.4),        # 右界：下臂端
                     (33.7, 3.4, 4.5)]          # 右界：上臂端


def test_guard_requires_the_flag_and_anchors(ls, cfg):
    off = dataclasses.replace(cfg, constraints=dataclasses.replace(
        cfg.constraints, require_connected=False))
    with pytest.raises(ValueError, match="require_connected"):
        ConnectivityGuard(ls, off)

    empty = LevelSet2D(ls.box, ls.dx)           # 全空气：没有任何锚点
    with pytest.raises(ValueError, match="没有任何锚点"):
        ConnectivityGuard(empty, cfg)


def test_config_requires_initial_metal_when_connected(cfg):
    """锚点从初始金属推导——两者必须一起给（配置写错要当场报错）。"""
    with pytest.raises(ValueError, match="没有 initial_metal"):
        dataclasses.replace(cfg, initial_metal=[]).validate()


# =========================================================================== #
# 修补：剪断 → 桥接
# =========================================================================== #
def test_bridge_reconnects_a_cut_y(ls, cfg):
    """把输入段在 x=12 处剪断：守卫应补出一根**细桥**并冻结它。

    断言的是补丁的四件事：① 又变成一块；② 锚点全在；③ 冻结掩膜非空；
    ④ 桥是细的（缺口里新增的金属远少于把整段填满）——填成实心等于偷偷
    改了几何，而 S 参数照样出数。
    """
    guard = ConnectivityGuard(ls, cfg, log=lambda *a: None)
    assert guard.apply(ls)                       # 初始状态本来就满足
    assert guard.bridges == 0 and not guard.frozen.any()

    _cut(ls, 12.0, 12.5, -1.5, 1.5)              # 剪断输入段
    assert _n_components(ls) == 2
    assert guard.violations(ls)                  # 记下症状（用于日志）
    assert guard.apply(ls)

    assert _n_components(ls) == 1
    assert guard.violations(ls) == ""
    assert guard.bridges == 1
    assert guard.frozen.sum() > 0

    # 桥是细的：缺口 0.5 mm 宽、2 mm 高（=420 个节点），补出来的该是几十个
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    in_cut = (X > 12.0) & (X < 12.5) & (np.abs(Y) < 1.0)
    assert 0 < int(((ls.phi < 0) & in_cut).sum()) < 100
    assert guard.frozen[in_cut].sum() > 0


def test_bridge_restores_a_lost_anchor(ls, cfg):
    """锚点失守（金属从框左界缩回去了）：守卫把锚点节点接回主域。"""
    guard = ConnectivityGuard(ls, cfg, log=lambda *a: None)
    _cut(ls, 6.0, 7.0, -2.0, 2.0)                # 削掉贴着左界的一整段
    assert _n_components(ls) == 1                # 还是一块（只是缩回去了）
    assert "锚点" in guard.violations(ls)
    assert guard.apply(ls)
    assert guard.violations(ls) == ""
    left = ls.phi[0, np.abs(ls.ys) <= D.W / 2 + 1e-9] <= 0.0
    assert left.any()                            # 左界又有了金属


def test_repair_is_idempotent_and_keeps_the_initial_state_untouched(ls, cfg):
    """守卫只在被剪断时才动手：初始状态下 apply 不该改 φ、也不该冻结。"""
    guard = ConnectivityGuard(ls, cfg, log=lambda *a: None)
    before = ls.phi.copy()
    for _ in range(3):
        assert guard.apply(ls, log=lambda *a: None)
    assert np.array_equal(ls.phi, before)
    assert not guard.frozen.any()


# =========================================================================== #
# 冻结掩膜
# =========================================================================== #
def test_frozen_nodes_are_blocked_in_the_velocity_mask(ls, cfg):
    """冻结的意义就是"速度置 0"：掩膜必须在这些节点上是 0。

    否则下一子步的形状导数会把刚补的桥再推开，来回拉锯（补丁白打）。
    """
    guard = ConnectivityGuard(ls, cfg, log=lambda *a: None)
    _cut(ls, 20.0, 20.5, -1.5, 1.5)
    assert guard.apply(ls)
    mask = build_velocity_mask(ls, cfg, frozen=guard.frozen)
    assert np.all(mask[guard.frozen] == 0.0)
    # 冻结之外仍有可动节点（别把整个设计区冻住）
    assert (mask > 0.5).sum() > 0.5 * ls.phi.size


def test_guard_survives_hj_steps_and_reinitialization(ls, cfg):
    """补丁要能扛住后续演化：给一个**整体收缩**的速度场再走几步 + 重初始化，
    桥不该散架（收缩速度对细桥最不友好）。"""
    guard = ConnectivityGuard(ls, cfg, log=lambda *a: None)
    _cut(ls, 12.0, 12.5, -1.5, 1.5)
    assert guard.apply(ls)

    V = np.full(ls.phi.shape, -0.05)             # 处处收缩（法向速度 < 0）
    for _ in range(2):
        mask = build_velocity_mask(ls, cfg, frozen=guard.frozen)
        ls.update(V * mask, steps=1, cfl=0.5)
        assert guard.apply(ls, log=lambda *a: None)
    ls.reinitialize(band=0.5, iters=20)
    assert guard.apply(ls, log=lambda *a: None)
    assert _n_components(ls) == 1
    assert guard.violations(ls) == ""


def test_metal_annihilated_cannot_be_repaired(ls, cfg):
    """金属被整块抹掉 → 修补不了（apply 返回 False，调用方回滚该子步）。

    这是 ``apply`` 唯一会返回 False 的情形（其余情形最短路径桥接必成），
    也是 pipeline 连续回滚 3 次就停的触发条件。
    """
    guard = ConnectivityGuard(ls, cfg, log=lambda *a: None)
    ls.phi[:] = 1.0                              # 全空气
    assert not guard.apply(ls, log=lambda *a: None)
    assert guard.bridges == 0 and not guard.frozen.any()   # 没得补，也没留痕


# =========================================================================== #
# 闭环集成：交给 CST 的几何必须始终是"一块、且压着三个锚点"
#
# 这是论文那条约束真正的兑现处——φ 里连着不算数，得看落到 CST 手里的
# 多边形（φ → 零等值线 → close_open_contours → 多边形）是不是还连着。
# =========================================================================== #
class _StubSolver(SolverInterface):
    """只记账的求解器替身：存下每轮 build_model 收到的可动几何。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.shapes: list[list[np.ndarray]] = []
        self.closed = False

    def begin_iteration(self, iteration):
        pass

    def build_model(self, movable, fixed=None):
        self.shapes.append([np.asarray(m, dtype=float) for m in movable])

    def solve_forward(self):
        return self._solution()

    def solve_backward(self):
        return self._solution()

    def close(self):
        self.closed = True

    def _solution(self) -> Solution:
        """常量场：够 WLS 插值就行（值本身不参与本文件的断言）。"""
        box = self.cfg.design_region.box
        h = 0.25
        origin = (box.x[0] - 1.5, box.y[0] - 1.5, -0.5)   # z 面 −0.5/−0.4
        nx = int(round((box.width + 3.0) / h)) + 1
        ny = int(round((box.height + 3.0) / h)) + 1
        e = np.full((nx, ny, 2, 3), 1.0 + 0.0j, dtype=complex)
        hf = np.full((nx, ny, 2, 3), 0.5j, dtype=complex)
        return Solution(
            s_params={(2, 1): 0.5 + 0j, (3, 1): 0.5 + 0j},
            e_field=FieldGrid(origin=origin, spacing=(h, h, 0.1), data=e),
            h_field=FieldGrid(origin=origin, spacing=(h, h, 0.1), data=hf),
            pin=0.5)


def _tmp_cfg(cfg, tmp_path, **optimizer_kw) -> CaseConfig:
    """功分器配置副本：输出改到 tmp、迭代数按测试需要覆盖。"""
    opt = dataclasses.replace(cfg.optimizer, convergence_window=99, **optimizer_kw)
    return dataclasses.replace(
        cfg, optimizer=opt,
        output=dataclasses.replace(cfg.output, dir=str(Path(tmp_path) / "results")))


def _cst_polygons(polys, cfg) -> list[np.ndarray]:
    """照 cst.py:156 的口径闭合轮廓（pad 外扩 → 与区外固定馈线重叠）。"""
    return close_open_contours([np.asarray(p, dtype=float) for p in polys],
                               cfg.design_region.box)


def _assert_one_body_with_anchors(polys, cfg, tag: str) -> None:
    """多边形并集：4-连通必须是一块，且三个锚点处必须有金属。"""
    box = cfg.design_region.box
    dx = 0.05
    xs = np.arange(box.x[0], box.x[1] + 0.5 * dx, dx)
    ys = np.arange(box.y[0], box.y[1] + 0.5 * dx, dx)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    pts = np.stack([X.ravel(), Y.ravel()], axis=1)
    inside = np.zeros(X.shape, dtype=bool)
    for p in polys:
        inside |= MplPath(p).contains_points(pts).reshape(X.shape)

    n = ndimage.label(inside, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]],
                                                 dtype=bool))[1]
    assert n == 1, f"{tag}: 交给 CST 的金属被切成 {n} 块"
    near_left = inside & (X < box.x[0] + 0.2)
    assert np.any(near_left & (np.abs(Y) <= D.W / 2)), f"{tag}: 输入锚点没接上"
    near_right = inside & (X > box.x[1] - 0.2)
    for sgn in (1, -1):
        win = (sgn * Y >= D.ARM_DY - D.W / 2 - 0.2) & (sgn * Y <= D.ARM_DY + D.W / 2 + 0.2)
        assert np.any(near_right & win), f"{tag}: {'上' if sgn > 0 else '下'}臂锚点没接上"


def test_divider_pipeline_runs_clean_offline(tmp_path, cfg):
    """整条闭环在**初始 Y** 上跑两轮：一次修补都不许有。

    回归"锚点判定用 φ<0"那个 bug：初始状态锚点节点恰好是 φ=−0.0，会被判成
    三个锚点全失守——第 0 轮就桥接并冻结边界节点，实跑时静默改掉初始几何。
    """
    c = _tmp_cfg(cfg, tmp_path, max_iterations=2)
    solver = _StubSolver(c)
    pipe = OptimizerPipeline(c, solver, make_level_set(c))
    assert pipe.guard is not None                    # require_connected: true
    hist = pipe.run()

    assert hist.stop_reason == "max_iterations"
    assert pipe.guard.bridges == 0 and not pipe.guard.frozen.any()
    assert len(solver.shapes) == 2
    for it, polys in enumerate(solver.shapes):
        closed = _cst_polygons(polys, c)
        assert len(closed) == 1                      # Y 形 3 个开口 → 1 个多边形
        _assert_one_body_with_anchors(closed, c, f"iter {it}")


def test_guard_keeps_every_cst_shape_connected_under_a_tearing_velocity(
        tmp_path, cfg, monkeypatch):
    """速度场反复在 x≈20 处咬断输入段：每轮交给 CST 的几何仍必须是一块。

    这是"打补丁 + 冻结"的兑现处：咬断 → 最短路径桥接 → 桥接节点冻结 →
    下一轮的速度场咬不动它。只断言 φ 里连着是不够的——φ 里的桥要是细到
    零等值线提取不出来（或自交），CST 拿到的仍是断的。
    """
    c = _tmp_cfg(cfg, tmp_path, max_iterations=3, step_cells=10.0)
    solver = _StubSolver(c)

    def tear(self, sol_f, sol_b, movable):
        V = np.zeros(self.ls.phi.shape)
        V[(self.ls.xs > 20.0) & (self.ls.xs < 21.0), :] = -1.0    # 收缩 → 咬断
        return V * build_velocity_mask(self.ls, self.cfg,
                                       frozen=self.guard.frozen)

    monkeypatch.setattr(OptimizerPipeline, "_assemble_velocity", tear)
    pipe = OptimizerPipeline(c, solver, make_level_set(c))
    pipe.run()

    assert pipe.guard.bridges >= 1 and pipe.guard.frozen.any()
    assert solver.shapes[-1]                          # 每轮都重建了几何
    for it, polys in enumerate(solver.shapes):
        _assert_one_body_with_anchors(_cst_polygons(polys, c), c, f"iter {it}")


def test_pipeline_rolls_back_and_stops_when_repair_keeps_failing(
        tmp_path, cfg, monkeypatch):
    """修补永远失败 → 每个子步回滚（φ 纹丝不动），连续 3 次后以 connectivity 停。

    模拟"速度场在把结构整块抹掉"的灾难路径：不能带着断掉的中间态继续演化，
    也不该无限重试。计数按**子步**累计（成功一次即清零），step_cells=1/cfl=0.5
    每轮 2 个子步 → 第 0 轮 2 次、第 1 轮第 1 个子步到 3 就停，所以落盘 2 轮。
    """
    c = _tmp_cfg(cfg, tmp_path, max_iterations=5)
    solver = _StubSolver(c)
    monkeypatch.setattr(
        OptimizerPipeline, "_assemble_velocity",
        lambda self, sol_f, sol_b, movable: np.full(self.ls.phi.shape, -0.5))
    monkeypatch.setattr(ConnectivityGuard, "apply",
                        lambda self, ls, log=print: False)

    ls = make_level_set(c)
    phi0 = ls.phi.copy()
    hist = OptimizerPipeline(c, solver, ls).run()

    assert hist.stop_reason == "connectivity"
    assert len(hist.records) == 2                     # 第 1 轮第 1 个子步到阈值
    # 金属区域一点没动（每个子步都回滚了）。比的是**几何**不是 φ 的数值：
    # 轮末的周期性重初始化会合法地重标定 φ（零等值线不动）。
    assert np.array_equal(ls.phi < 0, phi0 < 0)
    assert solver.closed
