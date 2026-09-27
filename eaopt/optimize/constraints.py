"""几何约束：速度掩膜与最小间距投影。

- build_velocity_mask: fixed_region 内、allowed_region 外、
  设计区边缘 taper 带内 → 速度置零（保证馈线/直通线不变形、
  可动金属不越界、与固定几何平滑衔接）；
- apply_min_gap: 最小间距硬约束投影——构造固定金属的 SDF 加
  min_gap 偏移得到禁区，φ ← max(φ, φ_prohibit)，裁掉侵入禁区的
  可动金属（硬保证，优于速度抑制的渐近保证）。
"""

from __future__ import annotations

import numpy as np
from matplotlib.path import Path

from eaopt.config import CaseConfig
from eaopt.geometry.levelset import LevelSet2D

__all__ = ["build_velocity_mask", "apply_min_gap", "interp_mask_at"]


def interp_mask_at(ls: LevelSet2D, points: np.ndarray, mask_grid: np.ndarray) -> np.ndarray:
    """网格掩膜双线性插值到采样点（界外为 0）。"""
    from scipy.interpolate import RegularGridInterpolator

    interp = RegularGridInterpolator((ls.xs, ls.ys), mask_grid,
                                     bounds_error=False, fill_value=0.0)
    return interp(np.asarray(points, dtype=float))


def build_velocity_mask(
    ls: LevelSet2D, cfg: CaseConfig, taper_mm: float = 1.0
) -> np.ndarray:
    """速度掩膜（1 = 允许运动，0 = 禁止）。

    注意用乘法组合（float 数组不支持 &= 位运算）。
    """
    mask = np.ones(ls.phi.shape)
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    pts = np.stack([X.ravel(), Y.ravel()], axis=1)

    # fixed_region 内及邻域（margin）禁止：只用 contains_points 会漏掉
    # 边界外侧节点，导致固定金属的 φ 边界被泄漏速度推动（实测使
    # mock 电容虚增 5 倍）。margin 取 max(min_gap, 2dx)，与最小间距
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

    # 设计区边缘 taper（与固定馈线平滑衔接）
    edge = np.minimum.reduce(
        [
            X - ls.box.x[0],
            ls.box.x[1] - X,
            Y - ls.box.y[0],
            ls.box.y[1] - Y,
        ]
    )
    mask *= np.clip(edge / taper_mm, 0.0, 1.0)
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
