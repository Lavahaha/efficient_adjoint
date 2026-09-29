"""边界采样：轮廓 → 导数采样点；导数 → 网格速度栅格化。

- sample_boundary: 把轮廓按点距重采样，沿局部法向偏移得到场采样点
  （outside=金属外侧 PEC 用法；inside=金属内侧损耗金属用法），
  返回采样点 (N,2) 与边界法向 (N,2)；
- scatter_to_grid: 采样点上的标量（形状导数）赋给最近网格节点，
  供 LevelSet2D.extend_velocity 使用。
"""

from __future__ import annotations

import numpy as np

from eaopt.config import CaseConfig
from eaopt.geometry.contour import smooth_resample
from eaopt.geometry.levelset import LevelSet2D

__all__ = ["sample_boundary", "scatter_to_grid", "make_fixed_sdf", "is_fixed_contour"]


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
