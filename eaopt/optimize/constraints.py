"""几何约束：速度掩膜、最小间距投影、连通性。

- build_velocity_mask: fixed_region 内、allowed_region 外、
  设计区边缘 taper 带内 → 速度置零（保证馈线/直通线不变形、
  可动金属不越界、与固定几何平滑衔接）。taper 只作用在
  ``constraints.taper_edges`` 指定的边上——设计区本身就是可动金属生长
  边界的那条边不能 taper（见 ConstraintSpec）；
- apply_min_gap: 最小间距硬约束投影——构造固定金属的 SDF 加
  min_gap 偏移得到禁区，φ ← max(φ, φ_prohibit)，裁掉侵入禁区的
  可动金属（硬保证，优于速度抑制的渐近保证）；
- ConnectivityGuard: 连通性硬约束（``constraints.require_connected``）
  ——可动金属必须始终是**一块**、且贴着初始那几个"穿出设计区接固定馈线"
  的锚点。被掐断时按最短路径桥接，桥接节点冻结（速度置 0）。
"""

from __future__ import annotations

import numpy as np
from matplotlib.path import Path

from eaopt.config import CaseConfig
from eaopt.geometry.levelset import LevelSet2D

__all__ = ["build_velocity_mask", "apply_min_gap", "interp_mask_at",
           "anchor_runs", "ConnectivityGuard"]

#: 4-连通结构元。**不用 8-连通**：对角相接的两块金属在 CST 里挤出后只是
#: 点接触，桥接时容易塌成尖角；用 4-连通判据可以早一步补出实心的桥。
_STRUCT4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)


def interp_mask_at(ls: LevelSet2D, points: np.ndarray, mask_grid: np.ndarray) -> np.ndarray:
    """网格掩膜双线性插值到采样点（界外为 0）。"""
    from scipy.interpolate import RegularGridInterpolator

    interp = RegularGridInterpolator((ls.xs, ls.ys), mask_grid,
                                     bounds_error=False, fill_value=0.0)
    return interp(np.asarray(points, dtype=float))


def build_velocity_mask(
    ls: LevelSet2D, cfg: CaseConfig, taper_mm: float = 1.0,
    frozen: np.ndarray | None = None,
) -> np.ndarray:
    """速度掩膜（1 = 允许运动，0 = 禁止）。

    ``frozen``：``ConnectivityGuard`` 要求**永久冻结**的节点（桥接补丁），
    直接乘进来（float 数组不支持 &= 位运算）。

    注意用乘法组合。
    """
    mask = np.ones(ls.phi.shape)
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    pts = np.stack([X.ravel(), Y.ravel()], axis=1)

    # fixed_region 内及邻域（margin）禁止：只用 contains_points 会漏掉
    # 边界外侧节点，导致固定金属的 φ 边界被泄漏速度推动（实测显著改变
    # 求解结果）。margin 取 max(min_gap, 2dx)，与最小间距
    # 约束语义一致（可动金属本就不该进入该邻域）。
    for poly in cfg.fixed_region:
        p = np.asarray(poly.vertices, dtype=float)
        mask *= ~Path(p).contains_points(pts).reshape(ls.phi.shape)
    if cfg.fixed_region:
        tmp = LevelSet2D(ls.box, ls.dx)
        tmp.init_from_polygons(
            [np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region]
        )
        margin = max(cfg.constraints.min_gap_mm, 2.0 * ls.dx)
        mask *= tmp.phi > margin  # 固定金属外距离 > margin 才允许运动

    # allowed_region 外禁止
    if cfg.constraints.allowed_region is not None:
        b = cfg.constraints.allowed_region
        mask *= (X >= b.x[0]) & (X <= b.x[1]) & (Y >= b.y[0]) & (Y <= b.y[1])

    # 设计区边缘 taper（与固定馈线平滑衔接）。作用边由
    # ``cfg.constraints.taper_edges`` 选：只该 taper 可动金属**穿出去**的边
    # （那里连着区外的固定馈线）；可动金属自由生长的那条边 taper 不得开，
    # 否则等于沿边界削掉优化目标本身。
    edges = cfg.constraints.taper_edges
    if edges != "none":
        d = []
        if "x" in edges:
            d += [X - ls.box.x[0], ls.box.x[1] - X]
        if "y" in edges:
            d += [Y - ls.box.y[0], ls.box.y[1] - Y]
        mask *= np.clip(np.minimum.reduce(d) / taper_mm, 0.0, 1.0)
    if frozen is not None:
        mask *= ~np.asarray(frozen, dtype=bool)
    return mask


def apply_min_gap(ls: LevelSet2D, cfg: CaseConfig) -> None:
    """最小间距投影：可动金属不得侵入固定金属的 min_gap 邻域。

    实现：固定金属 SDF 给出禁区外壳（固定金属外的 dist < gap 区域），
    把禁区内的 φ 翻到正侧（|φ|），即裁掉侵入的可动金属。
    注意不能用 max(φ, ·)：禁区外的负底板会把整块金属"压薄"。
    """
    gap = cfg.constraints.min_gap_mm
    if gap <= 0.0 or not cfg.fixed_region:
        return
    tmp = LevelSet2D(ls.box, ls.dx)
    tmp.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region])
    dist = np.abs(tmp.phi)
    prohibited = (tmp.phi > 0) & (dist < gap)  # 固定金属外的禁区外壳
    ls.phi[prohibited] = np.abs(ls.phi[prohibited])


# ---------------------------------------------------------------------- #
# 连通性（论文 III-B 的几何约束："microstrip lines are always connected"）
# ---------------------------------------------------------------------- #
def _boundary_lines(ls: LevelSet2D) -> list[list[tuple[int, int]]]:
    """设计区四条边上的节点序号（(i,j)，按沿边顺序）。"""
    nx, ny = ls.nx, ls.ny
    return [
        [(0, j) for j in range(ny)],                  # 左界 x=x0
        [(nx - 1, j) for j in range(ny)],              # 右界 x=x1
        [(i, 0) for i in range(nx)],                   # 下界 y=y0
        [(i, ny - 1) for i in range(nx)],              # 上界 y=y1
    ]


def anchor_runs(ls: LevelSet2D) -> list[np.ndarray]:
    """设计区**边界上**的金属段（每段一组 (N,2) 节点坐标）。

    金属在边界上（φ ≤ 0）就意味着它穿出去与区外的固定馈线相接——这正是
    必须始终保住的连接点。每个连续段（沿边 4-连通）算一个锚点。用 φ ≤ 0 而
    非 < 0：正压在多边形边上的节点 φ = −0.0，严格小于会漏判（实测本算例
    三个锚点会全部漏掉）。
    """
    out: list[np.ndarray] = []
    for line in _boundary_lines(ls):
        run: list[tuple[int, int]] = []
        for k, (i, j) in enumerate(line):
            if ls.phi[i, j] <= 0.0:
                run.append((i, j))
            elif run:
                out.append(run)
                run = []
        if run:
            out.append(run)
    return [np.array([[ls.xs[i], ls.ys[j]] for i, j in run]) for run in out]


class ConnectivityGuard:
    """连通性硬约束：**一块金属 + 锚点不失守**，破了就按最短路径桥接。

    为什么需要它：论文 III-B 的初始结构是 Y 形（可动金属穿出设计区接三条
    固定馈线），优化过程中速度场完全可能把某条臂掐断——断了以后两输出不再
    同相、Wilkinson 功分器的前提没了，而 CST 照样出 S 参数，只是结果是
    另一个器件的（静默失效，最贵的一类）。

    做法（用户拍板）**不是**整轮回滚，而是打补丁：

    1. 每个 HJ 子步后（``apply_min_gap`` 之后再检查）标记金属连通域；
    2. 主域 = 含锚点最多的那一块，其余各域按"到主域最近"逐个用
       **最短路径走廊**（KD-tree 找最近点对）接回主域；
    3. 锚点脱落（边界那段没有金属了）同理：从锚点节点桥接到最近的主域；
    4. 走廊节点 φ ← −0.5·dx（贴着金属内侧，零等值线把它包成 ~0.4 mm 宽的
       金属桥），并写进**持久冻结掩膜**（``frozen``，速度置 0）——
       否则下一子步速度场会把刚补的桥再掐掉，来回拉锯；
    5. 补完仍不满足（找不到可接的域、或金属被彻底清空）→ 调用方回滚该子步；
       连续多次失败就该停（速度场在试图撕裂结构，继续跑没有意义）。

    冻结节点是"补丁"，会随迭代累积：超过设计区节点的 5% 时打印告警
    （雪崩的征兆，那时该换"最小颈宽投影"而不是继续打补丁）。
    """

    #: 桥接走廊的半径（格）：2 格 = 0.2 mm → 零等值线包出 ~0.4 mm 宽的桥，
    #: 与论文照片里的细颈同量级，也够 CST 六面体网格认出来。
    RADIUS_CELLS = 2.0
    #: 冻结节点占比告警阈值
    FROZEN_WARN_FRACTION = 0.05

    def __init__(self, ls: LevelSet2D, cfg: CaseConfig, *, log=print):
        if not cfg.constraints.require_connected:
            raise ValueError("ConnectivityGuard 需要 constraints.require_connected=true")
        self.cfg = cfg
        self.anchors = anchor_runs(ls)          # 锚点从**初始** φ 推导，此后不变
        if not self.anchors:
            raise ValueError(
                "require_connected：初始金属没有一段贴在设计区边界上——"
                "没有任何锚点可守（可动金属压根没接上区外馈线？）")
        self.frozen = np.zeros(ls.phi.shape, dtype=bool)
        self.bridges = 0                        # 累计桥接次数
        self.frozen_warned = False
        log("[约束] 连通性已开：锚点 "
            + "、".join(f"{len(a)} 点 (x∈[{a[:, 0].min():g},{a[:, 0].max():g}], "
                        f"y∈[{a[:, 1].min():g},{a[:, 1].max():g}])"
                        for a in self.anchors))

    # ------------------------------------------------------------------ #
    def apply(self, ls: LevelSet2D, *, log=print) -> bool:
        """检查 + 修补。返回约束是否（修补后）成立。"""
        self._repair(ls, log=log)
        bad = self.violations(ls)
        if bad:
            log(f"[约束] 连通性修补后仍不满足：{bad}")
        return not bad

    def _pts(self, ls: LevelSet2D, a: np.ndarray) -> np.ndarray:
        """锚点坐标 → 网格节点序号 (N,2)。"""
        return np.round((a - [ls.xs[0], ls.ys[0]]) / ls.dx).astype(int)

    def _anchor_has_metal(self, ls: LevelSet2D, a: np.ndarray) -> bool:
        """锚点处还有金属吗（φ ≤ 0）。

        **必须是 ≤ 0 而不是 < 0**：锚点节点正压在设计区边界（多边形边上），
        φ 是 −0.0 这种"贴着界面"的值，严格小于会判成空气——初始状态就报
        "三个锚点全失守"并真的动手桥接、冻结边界节点（实测）。
        """
        pts = self._pts(ls, a)
        return bool(np.any(ls.phi[pts[:, 0], pts[:, 1]] <= 0.0))

    def _anchor_labels(self, ls: LevelSet2D, lab: np.ndarray,
                       a: np.ndarray) -> list[int]:
        """锚点贴着的连通域编号（0 = 无）。

        锚点节点本身多半不是金属（φ = −0.0，见 ``_anchor_has_metal``），
        所以取节点 3×3 邻域内的金属标签——朝内那一侧必有金属，否则
        ``_anchor_has_metal`` 早就判它失守了。只用节点自身的标签会全部得到
        0，"主域 = 锚点覆盖最多"就退化成"主域 = 最大块"，锚点白记。
        """
        out: list[int] = []
        for i, j in self._pts(ls, a):
            block = lab[max(i - 1, 0):i + 2, max(j - 1, 0):j + 2]
            out += [int(v) for v in block.ravel() if v > 0]
        return out

    def violations(self, ls: LevelSet2D) -> str:
        """"哪里不满足"的可读描述（空串 = 满足）。"""
        from scipy import ndimage

        _, n = ndimage.label(ls.phi < 0, structure=_STRUCT4)
        if n != 1:
            return f"金属连通域 {n} 个（应为 1）"
        for k, a in enumerate(self.anchors):
            if not self._anchor_has_metal(ls, a):
                c = a.mean(axis=0)
                return (f"第 {k + 1} 个锚点（x≈{c[0]:g}, y≈{c[1]:g}）"
                        "在设计区边界上没有金属了")
        return ""

    # ------------------------------------------------------------------ #
    def _repair(self, ls: LevelSet2D, *, log=print) -> None:
        from scipy import ndimage
        from scipy.spatial import cKDTree

        metal = ls.phi < 0
        lab, n = ndimage.label(metal, structure=_STRUCT4)
        if n == 0:                              # 整块金属都没了：补不回来
            return
        coords = np.stack(np.meshgrid(ls.xs, ls.ys, indexing="ij"), axis=-1)

        # 锚点节点所属的连通域（用锚点覆盖数选主域）
        anchor_ids = []
        for a in self.anchors:
            anchor_ids += self._anchor_labels(ls, lab, a)
        sizes = ndimage.sum_labels(np.ones_like(lab), lab, index=range(1, n + 1))
        cover = {i: anchor_ids.count(i) for i in range(1, n + 1)}
        main = max(range(1, n + 1), key=lambda i: (cover[i], sizes[i - 1]))
        if cover[main] == 0:                    # 所有锚点都失守：先随便挑一块
            main = int(np.argmax(sizes)) + 1
        main_pts = coords[lab == main]

        # 其余各域：从大到小逐个接回主域
        for comp in sorted((i for i in range(1, n + 1) if i != main),
                           key=lambda i: -sizes[i - 1]):
            comp_pts = coords[lab == comp]
            d, k = cKDTree(main_pts).query(comp_pts)
            j = int(np.argmin(d))
            p0, p1 = main_pts[k[j]], comp_pts[j]
            added = self._bridge(ls, p0, p1)
            self.bridges += 1
            log(f"[约束] 连通性破坏 → 桥接（最短路径 {d[j]:.2f} mm），"
                f"冻结 {added} 节点")
            main_pts = np.vstack([main_pts, comp_pts])
            lab[lab == comp] = main              # 视作已并入主域

        # 锚点失守：从锚点节点桥接到最近的主域节点
        for k, a in enumerate(self.anchors):
            if self._anchor_has_metal(ls, a):
                continue
            d, idx = cKDTree(main_pts).query(a)
            j = int(np.argmin(d))
            added = self._bridge(ls, main_pts[idx[j]], a[j])
            self.bridges += 1
            log(f"[约束] 锚点 {k + 1} 失守 → 桥接（{d[j]:.2f} mm），"
                f"冻结 {added} 节点")

        frac = float(self.frozen.mean())
        if frac > self.FROZEN_WARN_FRACTION and not self.frozen_warned:
            self.frozen_warned = True
            log(f"[约束] 警告：冻结节点已达设计区的 {frac:.1%}"
                f"（累计桥接 {self.bridges} 次）——连通性靠打补丁维持，"
                "说明速度场在持续撕裂结构，考虑最小颈宽投影或缩小步长")

    def _bridge(self, ls: LevelSet2D, p0: np.ndarray, p1: np.ndarray) -> int:
        """沿 p0→p1 铺一条走廊：节点 φ ← −0.5·dx 并冻结，返回新增节点数。"""
        length = float(np.linalg.norm(np.asarray(p1) - np.asarray(p0)))
        n = max(int(np.ceil(length / (0.5 * ls.dx))), 2)
        r = int(np.ceil(self.RADIUS_CELLS))
        val = -0.5 * ls.dx
        rad = self.RADIUS_CELLS * ls.dx + 1e-12
        added = 0
        for x, y in np.linspace(p0, p1, n):
            i0 = int(round((x - ls.xs[0]) / ls.dx))
            j0 = int(round((y - ls.ys[0]) / ls.dx))
            for i in range(max(i0 - r, 0), min(i0 + r + 1, ls.nx)):
                for j in range(max(j0 - r, 0), min(j0 + r + 1, ls.ny)):
                    if np.hypot(ls.xs[i] - x, ls.ys[j] - y) > rad:
                        continue
                    if ls.phi[i, j] > val:      # 已是金属的节点不动（保持原形）
                        ls.phi[i, j] = val
                        self.frozen[i, j] = True
                        added += 1
        return added
