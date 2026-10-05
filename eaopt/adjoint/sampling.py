"""边界采样：轮廓 → 导数采样点；导数 → 网格速度。

两种方案（``sampling.scheme``）：

- ``nodes``（论文 Fig. 3b）：采样点 = **落在金属边界上的 φ 网格节点**，
  即边界外（或内）侧往外数第 ``offset_cells`` 排的一组节点；轮廓 = 这些
  节点按序连成的折线（采样点与轮廓顶点是同一份离散）。采样点集与网格
  同源 → 场值按索引直接取（零插值）、跨轮可比、不需要 scatter/延拓两步。
  要求 ``design_region.grid_step_mm`` = CST 场导出步长。
- ``contour``（旧）：轮廓按弧长重采样 + 沿法向偏移，再 scatter 到最近
  节点 + PDE 延拓。采样间距与网格无关、点集逐轮漂移，只留作 A/B 对照。

公共部分：make_fixed_sdf / is_fixed_contour 判定某条轮廓属于固定金属
（不参与采样——论文只优化可动边界，且固定金属表面场强会劫持归一化）。
"""

from __future__ import annotations

import numpy as np

from eaopt.config import CaseConfig
from eaopt.geometry.contour import extract_contours, smooth_resample
from eaopt.geometry.levelset import LevelSet2D

__all__ = [
    "sample_boundary", "scatter_to_grid", "make_fixed_sdf", "is_fixed_contour",
    "boundary_row", "boundary_nodes", "sample_nodes", "node_field_indices",
    "spread_to_grid",
]


def sample_boundary(
    contours: list[np.ndarray],
    ls: LevelSet2D,
    cfg: CaseConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """轮廓 → (采样点, 法向)。

    返回 points (N,2)（已按 sample_side 沿法向偏移 sample_offset）
    与 normals (N,2)（偏移前的边界法向，金属外法向）。

    固定金属（fixed_region）的轮廓不参与采样：论文只优化可动边界，
    且固定金属表面场强会劫持速度归一化（max|δp|），拖慢可动边界。
    """
    fixed_sdf = make_fixed_sdf(ls, cfg)
    s = cfg.sampling
    side = 1.0 if s.sample_side == "outside" else -1.0
    pts_list, nrm_list = [], []
    for c in contours:
        if fixed_sdf is not None and is_fixed_contour(c, fixed_sdf):
            continue
        closed = np.allclose(c[0], c[-1])
        r = smooth_resample(c, s.point_spacing_mm, smoothing=0.0, closed=closed)
        n = ls.normals_at(r[:, :2])
        pts_list.append(r[:, :2] + side * s.sample_offset_mm * n)
        nrm_list.append(n)
    if not pts_list:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.vstack(pts_list), np.vstack(nrm_list)


def make_fixed_sdf(ls: LevelSet2D, cfg: CaseConfig):
    """固定金属的带符号距离场（无固定区时返回 None）。"""
    if not cfg.fixed_region:
        return None
    tmp = LevelSet2D(ls.box, ls.dx)
    tmp.init_from_polygons(
        [np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region]
    )
    return tmp


def is_fixed_contour(c: np.ndarray, fixed_sdf: LevelSet2D) -> bool:
    """轮廓是否属于固定金属（其上点距固定区 < 0.1 mm）。"""
    from scipy.interpolate import RegularGridInterpolator

    interp = RegularGridInterpolator(
        (fixed_sdf.xs, fixed_sdf.ys), fixed_sdf.phi,
        bounds_error=False, fill_value=1.0,
    )
    dist = np.abs(interp(c[:, :2]))
    return bool(np.mean(dist) < 0.1)


def scatter_to_grid(
    ls: LevelSet2D, points: np.ndarray, values: np.ndarray
) -> np.ndarray:
    """采样点标量 → 最近网格节点（多值碰撞取平均）。"""
    out = np.zeros(ls.phi.shape)
    cnt = np.zeros(ls.phi.shape)
    if len(points) == 0:
        return out
    ix = np.round((points[:, 0] - ls.box.x[0]) / ls.dx).astype(int)
    iy = np.round((points[:, 1] - ls.box.y[0]) / ls.dx).astype(int)
    ok = (ix >= 0) & (ix < ls.nx) & (iy >= 0) & (iy < ls.ny)
    ix, iy = ix[ok], iy[ok]
    v = np.asarray(values, dtype=float)[ok]
    np.add.at(out, (ix, iy), v)
    np.add.at(cnt, (ix, iy), 1.0)
    return np.divide(out, cnt, out=np.zeros_like(out), where=cnt > 0)


# ---------------------------------------------------------------------- #
# 节点采样（论文 Fig. 3b：采样点 = 落在金属边界上的网格节点）
# ---------------------------------------------------------------------- #
_BAND_EPS_MM = 1e-9   # 带下界的容差位移（见 boundary_row）


def boundary_row(
    phi: np.ndarray, dx: float, offset_cells: int = 1, side: str = "outside"
) -> np.ndarray:
    """金属边界附近**一排**网格节点（bool 掩膜）。

    规则：``0 ≤ n·φ < dx`` 的带（``n = +1`` outside / ``−1`` inside），
    即沿外法向从边界往外数的第 ``offset_cells`` 排。带符号距离函数上，
    一条法线上间距 dx 的节点落进宽度 dx 的**半开**带里**恰好一个**——
    不会两排、也不会有空洞。所以照它采样时不必管 φ 是否正好落在格线上
    （真实算例的初始边就在格线上，此时这一排就是"落在边界上的网格节点"
    本身）。

    带整体下移 ``_BAND_EPS_MM``：边界压在格线上时那些节点的 φ 是
    ``−2e-16``（浮点舍入）而非 0，不带容差就会被判到带外，那一列留下
    空洞（折线于是跳过该列去够远处的节点）。
    """
    t = np.asarray(phi, dtype=float) * (1.0 if side == "outside" else -1.0)
    k = max(1, int(offset_cells))
    return (t >= (k - 1) * dx - _BAND_EPS_MM) & (t < k * dx - _BAND_EPS_MM)


def boundary_nodes(ls: LevelSet2D, cfg: CaseConfig) -> list[np.ndarray]:
    """可动金属的边界节点折线（世界坐标 mm，按轮廓顺序；闭合者首尾重复）。

    marching squares 的轮廓本来就按顺序给出边界走向，逐点吸附到最近的
    那一排网格节点（保持顺序、相邻去重）即可——不自写连通性追踪：8 连通/
    细颈的边界情形太多，而提取器的排序已经过测试。吸附到**单侧一排**
    很重要：否则折线会在边界两侧的节点间来回跳，采到的场一半来自金属
    覆盖区、一半来自间隙，δp 沿边界交替跳变。
    """
    contours = extract_contours(ls.xs, ls.ys, ls.phi)
    fixed_sdf = make_fixed_sdf(ls, cfg)
    s = cfg.sampling
    row = boundary_row(ls.phi, ls.dx, s.offset_cells, s.sample_side)
    out = []
    for c in contours:
        if fixed_sdf is not None and is_fixed_contour(c, fixed_sdf):
            continue
        closed = bool(np.allclose(c[0], c[-1]))
        nodes = _snap_to_row(c[:, :2], ls, row)
        if len(nodes) < 2:
            continue
        if closed and not np.allclose(nodes[0], nodes[-1]):
            nodes = np.vstack([nodes, nodes[0]])
        out.append(nodes)
    return out


def _snap_to_row(points: np.ndarray, ls: LevelSet2D, row: np.ndarray) -> np.ndarray:
    """折线 → 该排节点序列（保持折线顺序，相邻重复只留一个）。"""
    ix = np.clip(np.floor((points[:, 0] - ls.xs[0]) / ls.dx).astype(int), 1, ls.nx - 3)
    iy = np.clip(np.floor((points[:, 1] - ls.ys[0]) / ls.dx).astype(int), 1, ls.ny - 3)
    out: list[tuple[int, int]] = []
    for k in range(len(points)):
        bx = by = -1
        bd = np.inf
        for cx in range(ix[k] - 1, ix[k] + 3):
            for cy in range(iy[k] - 1, iy[k] + 3):
                if not row[cx, cy]:
                    continue
                d = (ls.xs[cx] - points[k, 0]) ** 2 + (ls.ys[cy] - points[k, 1]) ** 2
                if d < bd:
                    bx, by, bd = cx, cy, d
        if bx < 0:
            continue  # 轮廓周围两格内必有该排节点，理论到不了这里
        if out and (bx, by) == out[-1]:
            continue
        out.append((bx, by))
    if not out:
        return np.zeros((0, 2))
    return np.array([(ls.xs[i], ls.ys[j]) for i, j in out], dtype=float)


def sample_nodes(
    ls: LevelSet2D, polys: list[np.ndarray]
) -> tuple[np.ndarray, np.ndarray]:
    """边界节点折线 → (采样点 (N,2) mm, 网格下标 (N,2) int)，一一对应。

    采样点**就是**轮廓顶点（论文 Fig.3b：采样点连点成轮廓，两者是同一
    份离散），因此场值可按下标直接取。重复节点（多条轮廓相接处）只留一个。
    """
    xy_list, ij_list = [], []
    for poly in polys:
        p = np.asarray(poly, dtype=float)
        if len(p) > 1 and np.allclose(p[0], p[-1]):
            p = p[:-1]  # 闭合点重复只留一个
        ix = np.rint((p[:, 0] - ls.xs[0]) / ls.dx).astype(int)
        iy = np.rint((p[:, 1] - ls.ys[0]) / ls.dx).astype(int)
        if np.any(np.abs(ls.xs[ix] - p[:, 0]) > 1e-9) or \
           np.any(np.abs(ls.ys[iy] - p[:, 1]) > 1e-9):
            raise ValueError("边界节点不在网格节点上——boundary_nodes 的吸附出错了")
        xy_list.append(p)
        ij_list.append(np.column_stack([ix, iy]))
    if not xy_list:
        return np.zeros((0, 2)), np.zeros((0, 2), dtype=int)
    xy, ij = np.vstack(xy_list), np.vstack(ij_list)
    seen: set[tuple[int, int]] = set()
    keep = []
    for k, (i, j) in enumerate(ij):
        key = (int(i), int(j))
        if key in seen:
            continue
        seen.add(key)
        keep.append(k)
    return xy[keep], ij[keep]


def node_field_indices(
    field, xy: np.ndarray, z_mm: float
) -> tuple[np.ndarray, np.ndarray, int, float]:
    """采样点 → 场数组下标 ``(ix, iy, iz, z_used)``（**直接索引，零插值**）。

    点必须正好落在导出网格节点上；差一点就说明"采样网格 = 导出网格"这条
    不变量破了（导出步长或设计区对齐改错）。**此处必须报错而不是退回插值**：
    静默插值会把节点采样方案的全部收益（零插值误差、跨轮可比）悄悄还回去。
    z 例外：吸附到最近的网格面，实际平面随 ``z_used`` 返回——导出网格的 z
    面由 CST 包围盒原点决定，z=0 一般不在上面。
    """
    ax = field.axes()
    xy = np.asarray(xy, dtype=float)
    out = []
    for k, name in ((0, "x"), (1, "y")):
        i = np.rint((xy[:, k] - ax[k][0]) / field.spacing[k]).astype(int)
        resid = np.abs(ax[k][0] + i * field.spacing[k] - xy[:, k])
        bad = (i < 0) | (i >= ax[k].size)
        if bad.any() or float(resid.max() if resid.size else 0.0) > 1e-6:
            j = int(np.argmax(resid)) if not bad.any() else int(np.argmax(bad))
            raise ValueError(
                f"采样点 ({xy[j, 0]:.6g}, {xy[j, 1]:.6g}) mm 不在场导出网格节点上："
                f"{name} 轴原点 {ax[k][0]:g}、步长 {field.spacing[k]:g}、"
                f"范围 [{ax[k][0]:g}, {ax[k][-1]:g}]（该点偏差 {resid[j]:.3g} mm）。"
                "nodes 采样要求 design_region.grid_step_mm 与 CST 场导出步长相等，"
                "且设计区对齐到导出网格。"
            )
        out.append(i)
    iz = int(np.argmin(np.abs(ax[2] - float(z_mm))))
    return out[0], out[1], iz, float(ax[2][iz])


def spread_to_grid(
    ls: LevelSet2D, ixy: np.ndarray, values: np.ndarray
) -> np.ndarray:
    """采样节点上的标量 → 全网格（取最近采样点的值）。

    这是"速度沿法向常值延拓"的离散等价，也是节点方案唯一需要的传播。
    它替掉两步：scatter_to_grid 撒点会留下采样间距尺度的空洞，再经
    extend_velocity 的"未播种节点钉 0"外推成梳状速度尖峰。实测（真实
    iter=1 场数据）现行装配只兑现一阶预测位移的 0.30、边界粗糙度
    2.4e-2 mm；最近点延拓为 0.98 / 8.0e-3 mm。
    """
    from scipy.spatial import cKDTree

    v = np.asarray(values, dtype=float)
    out = np.zeros(ls.phi.shape)
    if len(v) == 0:
        return out
    ij = np.asarray(ixy)
    xy = np.column_stack([ls.xs[ij[:, 0]], ls.ys[ij[:, 1]]])
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    _, j = cKDTree(xy).query(np.column_stack([X.ravel(), Y.ravel()]))
    return v[j].reshape(ls.phi.shape)
