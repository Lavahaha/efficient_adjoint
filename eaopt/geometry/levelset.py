"""Initial signed-distance geometry and grid storage for CST constraints."""

from __future__ import annotations

import numpy as np
from shapely import contains_xy, distance, points

from eaopt.config import BoxSpec, CaseConfig
from eaopt.geometry.design import polygon_union

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
        X, Y = np.meshgrid(self.xs, self.ys, indexing="ij")
        pts = np.stack([X.ravel(), Y.ravel()], axis=1)
        metal = polygon_union(polygons)
        if metal.is_empty:
            raise ValueError("至少需要一个初始金属多边形")
        dist = distance(metal.boundary, points(pts))
        inside = contains_xy(metal, pts[:, 0], pts[:, 1])
        # 边界节点统一归金属，避免浮点残差造成符号抖动。
        inside |= dist <= _ON_INTERFACE_MM
        s = np.where(inside, -1.0, 1.0)
        self.phi = (s * dist).reshape(self.nx, self.ny)
        return self

# ---------------------------------------------------------------------- #
# 内部工具
# ---------------------------------------------------------------------- #
#: 多边形的边界节点归金属的判据（mm）。1e-9 是"浮点残差"量级——线段距离在
#: 边界点上算出的是 0 或 ~1e-15 的残差，而真实几何的最小间距是 0.1 mm 量级。
_ON_INTERFACE_MM = 1e-9


def make_level_set(cfg: CaseConfig) -> LevelSet2D:
    """Construct initial signed distance from configured metal polygons."""
    ls = LevelSet2D(cfg.design_region.box, cfg.design_region.grid_step_mm)
    return ls.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg.initial_metal])
