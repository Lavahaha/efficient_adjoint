"""边界采样：轮廓 → 导数采样点；导数 → 网格速度。

两种方案（``sampling.scheme``）：

- ``intersection``（当前）：边界点 = **marching 在网格线上的交点**（两端
  节点 φ 线性插值，亚格点）；场值用**加权最小二乘**按坐标从场导出网格取得
  （导出网格原点由 CST 包围盒定，与 φ 网格**不必对齐**）；速度回写 =
  最近节点 + 四邻居按反距离平方加权。交给 CST 的几何与做灵敏度分析的
  采样点是同一份交点折线。
- ``contour``（旧）：轮廓按弧长重采样 + 沿法向偏移，三线性取场，再
  scatter 到最近节点 + PDE 延拓。留作 A/B 对照。

公共部分：make_fixed_sdf / is_fixed_contour 判定某条轮廓属于固定金属
（不参与采样——论文只优化可动边界，且固定金属表面场强会劫持归一化）。
"""

from __future__ import annotations

import numpy as np

from eaopt.config import CaseConfig
from eaopt.geometry.contour import (dedupe_vertices, extract_contours,
                                    smooth_resample)
from eaopt.geometry.levelset import LevelSet2D

__all__ = [
    "boundary_points", "boundary_samples", "field_node_mask",
    "scatter_inverse_distance", "sample_boundary", "scatter_to_grid",
    "make_fixed_sdf", "is_fixed_contour",
]


# ---------------------------------------------------------------------- #
# 旧轮廓方案（contour）：按弧长重采样 + 偏移 + 最近节点栅格化
# ---------------------------------------------------------------------- #
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


# ---------------------------------------------------------------------- #
# 交点方案（intersection）：marching 交点 → WLS 取场 → 反距离平方回写
# ---------------------------------------------------------------------- #
def boundary_points(ls: LevelSet2D, cfg: CaseConfig) -> list[np.ndarray]:
    """可动金属的零等值线**交点折线**（世界坐标 mm，亚格点，按轮廓顺序）。

    交点位置由两端节点的 φ 线性插值给出（−0.3/0.7 → 距第一个节点 0.3 格），
    顶点**恒落在网格线上**——这是 extract_contours（matplotlib 的 marching）
    的既有性质，不另写情形表。去相邻重复点（零等值线穿过节点时提取器会
    连着发射同一交点），闭合者首尾精确相等。

    返回的折线同时是：① 交给 CST 重建的几何；② 灵敏度分析的采样点集。
    """
    fixed_sdf = make_fixed_sdf(ls, cfg)
    out = []
    for c in extract_contours(ls.xs, ls.ys, ls.phi):
        if fixed_sdf is not None and is_fixed_contour(c, fixed_sdf):
            continue
        p = dedupe_vertices(c)
        if len(p) < 2:
            continue
        out.append(p)
    return out


def boundary_samples(
    polys: list[np.ndarray], ls: LevelSet2D, cfg: CaseConfig
) -> tuple[np.ndarray, np.ndarray]:
    """边界折线 → (采样点 (N,2), 边界法向 (N,2))，逐点对应。

    ``intersection``：采样点就是交点本身（去掉闭合点重复，一个点只采样
    一次），法向 = 该点的 φ 梯度方向；``intersection_offset_mm`` > 0 时
    沿外法向再偏一段（把 WLS 从单侧外推变成内插）。
    ``contour``：走旧路径（弧长重采样 + ``sample_offset_mm`` 偏移）。
    """
    if cfg.sampling.scheme != "intersection":
        return sample_boundary(polys, ls, cfg)
    s = cfg.sampling
    side = 1.0 if s.sample_side == "outside" else -1.0
    pts_list, nrm_list = [], []
    for poly in polys:
        p = np.asarray(poly, dtype=float)[:, :2]
        if len(p) > 1 and np.allclose(p[0], p[-1]):
            p = p[:-1]  # 闭合点重复只留一个（否则该点被采样两次、权重翻倍）
        if len(p) == 0:
            continue
        n = ls.normals_at(p)
        pts_list.append(p + side * s.intersection_offset_mm * n)
        nrm_list.append(n)
    if not pts_list:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.vstack(pts_list), np.vstack(nrm_list)


def field_node_mask(
    ls: LevelSet2D, field, cfg: CaseConfig, side: str | None = None
) -> np.ndarray:
    """场网格节点上"可用"的掩膜 (nx_f, ny_f) bool。

    WLS 只用**与采样点同侧**、且**不在固定金属里**的节点：

    - 同侧：``sample_side=outside`` → φ_可动 > 0（介质侧）。PEC 界面两侧场
      不连续（金属体内 E≈0），两侧混着拟合会把 δp 系统性压低；
    - 固定金属（直通线）：φ_可动 在那里是正值，光看它会把直通线里 E≈0 的
      节点当成空气节点混进来——必须用固定区的 SDF 再排一次。

    场网格比设计区大 ``field_margin_mm``：区外的 φ 用线性外推（只做符号
    分类，不参与数值）。
    """
    from scipy.interpolate import RegularGridInterpolator

    s = cfg.sampling
    side = s.sample_side if side is None else side
    xs, ys = field.axes()[:2]
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    flat = np.column_stack([X.ravel(), Y.ravel()])

    def phi_at(ls_like: LevelSet2D) -> np.ndarray:
        interp = RegularGridInterpolator(
            (ls_like.xs, ls_like.ys), ls_like.phi,
            bounds_error=False, fill_value=None,  # None = 线性外推
        )
        return interp(flat).reshape(X.shape)

    phi = phi_at(ls)
    mask = phi > 0.0 if side == "outside" else phi < 0.0
    if side == "outside":
        fixed = make_fixed_sdf(ls, cfg)
        if fixed is not None:
            mask &= phi_at(fixed) > 0.0
    return mask


def scatter_inverse_distance(
    ls: LevelSet2D,
    points: np.ndarray,
    values: np.ndarray,
    *,
    max_dist_cells: float = 1.0,
    hit_tol_mm: float = 1e-9,
    neighbors: str = "cross",
) -> tuple[np.ndarray, np.ndarray]:
    """边界点上的标量 → (节点值 (nx,ny), counts (nx,ny))。反距离平方回写。

    - **命中节点**（``|x_s − x_p| ≤ hit_tol_mm``）：该节点独得全部权重
      （"边界点正好落在节点上 → 直接赋值"）；
    - 否则取 {最近节点} ∪ {四个轴邻居}（``neighbors="full"`` 时另有四个对角），
      保留距离 ≤ ``max_dist_cells``·dx 的，权重 ∝ 1/d²、归一化后累加；
    - 同一节点被多个边界点覆盖时按权重累积平均。

    交点恒落在网格线上 → "≤1 格"这条距离规则已把对角节点排除（对角距
    √(1+t²)·dx > dx），"最近节点+四邻居"于是**恰好退化为该边的两个端点**，
    权重 = (1−t)²/(t²+(1−t)²)（t=0.25 → 0.90；比线性插值的 0.75 略锐，
    是这类粒子-网格回写的有界偏置）。

    返回的 counts > 0 即"边界已知值节点"，PDE 回退延拓用它当种子。
    """
    pts = np.asarray(points, dtype=float)[:, :2]
    v = np.asarray(values, dtype=float).ravel()
    if len(pts) != len(v):
        raise ValueError(f"points {len(pts)} 个与 values {len(v)} 个不匹配")
    out = np.zeros(ls.phi.shape)
    cnt = np.zeros(ls.phi.shape)
    if len(pts) == 0:
        return out, cnt
    if neighbors not in ("cross", "full"):
        raise ValueError(f'neighbors 只认 "cross" / "full"，收到 {neighbors!r}')
    offs = [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]
    if neighbors == "full":
        offs += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
    offs = np.asarray(offs, dtype=int)
    ix0 = np.rint((pts[:, 0] - ls.xs[0]) / ls.dx).astype(int)
    iy0 = np.rint((pts[:, 1] - ls.ys[0]) / ls.dx).astype(int)
    ii = ix0[:, None] + offs[None, :, 0]          # (N, C)
    jj = iy0[:, None] + offs[None, :, 1]
    ok = (ii >= 0) & (ii < ls.nx) & (jj >= 0) & (jj < ls.ny)
    ii_c = np.clip(ii, 0, ls.nx - 1)
    jj_c = np.clip(jj, 0, ls.ny - 1)
    d = np.hypot(pts[:, 0:1] - ls.xs[ii_c], pts[:, 1:2] - ls.ys[jj_c])
    hit = d <= hit_tol_mm
    # 命中优先：命中点上只保留命中的那个节点（其余清零）
    w = np.where(hit, 1.0, 1.0 / np.maximum(d, 1e-12) ** 2)
    w = np.where(d <= max_dist_cells * ls.dx, w, 0.0)
    w = np.where(ok, w, 0.0)
    any_hit = hit.any(axis=1)
    if any_hit.any():
        w[any_hit] = np.where(hit[any_hit], 1.0, 0.0)
    total = w.sum(axis=1, keepdims=True)
    w = np.divide(w, total, out=np.zeros_like(w), where=total > 0)
    np.add.at(out, (ii_c.ravel(), jj_c.ravel()), (w * v[:, None]).ravel())
    np.add.at(cnt, (ii_c.ravel(), jj_c.ravel()), w.ravel())
    return np.divide(out, cnt, out=np.zeros_like(out), where=cnt > 0), cnt
