"""Initial signed-distance geometry and grid storage for CST constraints."""

from __future__ import annotations

import numpy as np

from eaopt.config import BoxSpec, CaseConfig

__all__ = ["LevelSet2D", "make_level_set"]


class LevelSet2D:
    """设计平面 x-y 上的二维水准集（均匀笛卡尔网格）。"""

    def __init__(self, box: BoxSpec, grid_step_mm: float):
        self.box = box
        self.dx = float(grid_step_mm)
        self.nx = int(round(box.width / self.dx)) + 1
        self.ny = int(round(box.height / self.dx)) + 1
        self.xs = np.linspace(box.x[0], box.x[1], self.nx)
        self.ys = np.linspace(box.y[0], box.y[1], self.ny)
        self.phi = np.full((self.nx, self.ny), 1.0)  # 缺省：全介质

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def init_from_polygons(self, polygons: list[np.ndarray]) -> "LevelSet2D":
        """由金属多边形（世界坐标 mm，自动闭合）构造带符号距离函数。

        polygons: list of (N,2) 顶点数组。金属内 φ<0。
        """
        from matplotlib.path import Path

        X, Y = np.meshgrid(self.xs, self.ys, indexing="ij")
        pts = np.stack([X.ravel(), Y.ravel()], axis=1)
        dist = np.full(len(pts), np.inf)
        inside = np.zeros(len(pts), dtype=bool)
        for poly in polygons:
            p = np.asarray(poly, dtype=float)
            if not np.allclose(p[0], p[-1]):
                p = np.vstack([p, p[0]])
            dist = np.minimum(dist, _polygon_distance(pts, p))
            inside |= Path(p).contains_points(pts)
        # 边界上的节点算金属。``contains_points`` 对正好落在边上的点是随机
        # 判的，而本算例的臂顶边 y=0 恰好压着一条节点行——不显式判一下，
        # 那一行的 φ 就是 ±1e-15 的噪声，画出来是一排假毛刺（几何其实没错：
        # 轮廓提取在两行之间插值，界面照样正好落在 y=0）。
        inside |= dist <= _ON_INTERFACE_MM
        s = np.where(inside, -1.0, 1.0)
        self.phi = (s * dist).reshape(self.nx, self.ny)
        return self

    # ------------------------------------------------------------------ #
    # HJ 演化
    # ------------------------------------------------------------------ #


    # ------------------------------------------------------------------ #
    # 重初始化
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 法向提取
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 速度延拓
    # ------------------------------------------------------------------ #


# ---------------------------------------------------------------------- #
# 内部工具
# ---------------------------------------------------------------------- #
#: 多边形的边界节点归金属的判据（mm）。1e-9 是"浮点残差"量级——线段距离在
#: 边界点上算出的是 0 或 ~1e-15 的残差，而真实几何的最小间距是 0.1 mm 量级。
_ON_INTERFACE_MM = 1e-9


def _segment_distance(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """pts (N,2) 到线段 ab 的距离。"""
    ab = b - a
    t = np.clip((pts - a) @ ab / (ab @ ab), 0.0, 1.0)
    proj = a + t[:, None] * ab
    return np.linalg.norm(pts - proj, axis=1)


def _polygon_distance(pts: np.ndarray, p: np.ndarray) -> np.ndarray:
    """pts (N,2) 到多边形 p (M,2，已闭合) 边界的最小距离。"""
    d = np.full(len(pts), np.inf)
    for i in range(len(p) - 1):
        d = np.minimum(d, _segment_distance(pts, p[i], p[i + 1]))
    return d


def make_level_set(cfg: CaseConfig) -> LevelSet2D:
    """Construct initial signed distance from configured metal polygons."""
    ls = LevelSet2D(cfg.design_region.box, cfg.design_region.grid_step_mm)
    return ls.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg.initial_metal])
