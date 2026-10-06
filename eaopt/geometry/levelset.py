"""二维水准集（论文 II-C 节，Fig. 3）。

约定：
  - φ 为带符号距离函数：金属内 φ<0，介质/空气 φ>0，零等值面即金属边界；
  - 边界演化：Hamilton–Jacobi 方程 ∂φ/∂t + V|∇φ| = 0，
    Godunov 一阶上风格式；特征线 dx/dt = V·n̂（n̂ 为金属外法向），
    故 V>0 金属扩张，V<0 金属收缩；
  - 重初始化：PDE 形式 ∂φ/∂τ + S(φ)(|∇φ|−1) = 0，S 为平滑符号函数；
  - 速度延拓：∂V/∂τ + S(φ) n̂·∇V = 0（n̂ = ∇φ/|∇φ|），仅在窄带 |φ|<band 内
    进行，边界邻域节点固定为已知值。

仅依赖 numpy；轮廓提取与平滑见 contour.py。
"""

from __future__ import annotations

import numpy as np

from eaopt.config import BoxSpec

__all__ = ["LevelSet2D"]


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
    def update(self, velocity: np.ndarray, steps: int = 1, cfl: float = 0.4) -> None:
        """按 ∂φ/∂t + V|∇φ| = 0 演化 `steps` 步，每步 dt = cfl·dx/max|V|。

        velocity: (nx, ny) 法向速度场。V>0 金属扩张、V<0 金属收缩。
        """
        velocity = np.asarray(velocity, dtype=float)
        if velocity.shape != self.phi.shape:
            raise ValueError("velocity 形状与网格不一致")
        vmax = float(np.max(np.abs(velocity)))
        if vmax == 0.0:
            return
        dt = cfl * self.dx / vmax
        for _ in range(steps):
            self._advance_one_step(velocity, dt)

    def _advance_one_step(self, v: np.ndarray, dt: float) -> None:
        phi = self.phi
        inv = 1.0 / self.dx
        d = phi[1:, :] - phi[:-1, :]
        dxm = np.zeros_like(phi)
        dxm[1:, :] = d * inv  # D-（向后差分）
        dxp = np.zeros_like(phi)
        dxp[:-1, :] = d * inv  # D+（向前差分）
        d = phi[:, 1:] - phi[:, :-1]
        dym = np.zeros_like(phi)
        dym[:, 1:] = d * inv
        dyp = np.zeros_like(phi)
        dyp[:, :-1] = d * inv

        vp, vm = np.maximum(v, 0.0), np.minimum(v, 0.0)
        # Godunov 数值哈密顿量：按 V 的符号选择迎风差分方向
        g_plus = np.sqrt(
            np.maximum(dxm, 0.0) ** 2 + np.minimum(dxp, 0.0) ** 2
            + np.maximum(dym, 0.0) ** 2 + np.minimum(dyp, 0.0) ** 2
        )
        g_minus = np.sqrt(
            np.maximum(dxp, 0.0) ** 2 + np.minimum(dxm, 0.0) ** 2
            + np.maximum(dyp, 0.0) ** 2 + np.minimum(dym, 0.0) ** 2
        )
        self.phi = phi - dt * (vp * g_plus + vm * g_minus)

    # ------------------------------------------------------------------ #
    # 重初始化
    # ------------------------------------------------------------------ #
    def reinitialize(self, iters: int = 60, band: float | None = None) -> None:
        """PDE 重初始化 ∂φ/∂τ + S(φ)(|∇φ|−1) = 0，恢复带符号距离函数。

        用 Godunov 上风格式（按 S 的符号选差分方向，与主方程同一套
        差分框架）：φ ← φ − dτ·S·(g_signed − 1)，其中 g_signed 为
        _advance_one_step 中按符号选取的数值 |∇φ|。中心差分形式不稳定，
        不可使用。

        band: 仅更新 |φ| < band 的区域（None = 全区域）。
        """
        dtau = 0.5 * self.dx
        for _ in range(iters):
            phi0 = self.phi
            S = self.phi / np.sqrt(self.phi**2 + self.dx**2)
            self._advance_one_step(S, dtau)  # φ ← φ − dτ·S·g_signed
            self.phi += dtau * S  # 补回 +dτ·S，合成 S·(g_signed − 1) 更新
            if band is not None:
                mask = np.abs(phi0) < band
                self.phi = np.where(mask, self.phi, phi0)

    # ------------------------------------------------------------------ #
    # 法向提取
    # ------------------------------------------------------------------ #
    def normals_at(self, points: np.ndarray) -> np.ndarray:
        """设计平面内 φ 的梯度方向（金属外法向），双线性插值到任意点。

        points: (N,2) mm。返回 (N,2) 单位法向（设计平面内）。
        """
        from scipy.interpolate import RegularGridInterpolator

        gx = np.gradient(self.phi, self.dx, axis=0)
        gy = np.gradient(self.phi, self.dx, axis=1)
        pts = np.asarray(points, dtype=float)
        fx = RegularGridInterpolator((self.xs, self.ys), gx,
                                     bounds_error=False, fill_value=0.0)
        fy = RegularGridInterpolator((self.xs, self.ys), gy,
                                     bounds_error=False, fill_value=0.0)
        n = np.stack([fx(pts), fy(pts)], axis=1)
        length = np.linalg.norm(n, axis=1, keepdims=True)
        return n / np.where(length < 1e-30, 1.0, length)

    # ------------------------------------------------------------------ #
    # 速度延拓
    # ------------------------------------------------------------------ #
    def extend_velocity(
        self,
        v_boundary: np.ndarray,
        band: float,
        iters: int = 80,
        known: np.ndarray | None = None,
    ) -> np.ndarray:
        """把边界速度延拓到窄带 |φ|<band：∂V/∂τ + S(φ)n̂·∇V = 0（上风）。

        v_boundary: (nx, ny)，仅在边界邻域为已知值（其余忽略）。
        known: 可选布尔掩膜，True = 边界已知值节点（由调用方给出，如
        scatter 真正落过点的节点）。None = 旧规则 |φ| ≤ 0.75·dx——φ 偏离
        距离函数（|∇φ|≠1）时那条固定带会静默漏掉种子。
        返回延拓后的速度场（窄带外为 0）。
        """
        v_boundary = np.asarray(v_boundary, dtype=float)
        if v_boundary.shape != self.phi.shape:
            raise ValueError("v_boundary 形状与网格不一致")
        near = (np.abs(self.phi) <= 0.75 * self.dx if known is None
                else np.asarray(known, dtype=bool))
        v = np.where(near, v_boundary, 0.0)
        dtau = 0.5 * self.dx
        for _ in range(iters):
            gx, gy = np.gradient(self.phi, self.dx)
            gnorm = np.hypot(gx, gy) + 1e-30
            S = self.phi / np.sqrt(self.phi**2 + self.dx**2)
            ux, uy = S * gx / gnorm, S * gy / gnorm  # 特征速度 S·n̂
            v = _advect_scalar_upwind(v, ux, uy, dtau, self.dx)
            v = np.where(np.abs(self.phi) <= band, v, 0.0)
            v = np.where(near, v_boundary, v)  # 边界节点固定为已知值
        return v


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


def _advect_scalar_upwind(
    v: np.ndarray, ux: np.ndarray, uy: np.ndarray, dt: float, dx: float
) -> np.ndarray:
    """标量输运 ∂v/∂t + u·∇v = 0 的一阶上风一步。"""
    inv = 1.0 / dx
    d = v[1:, :] - v[:-1, :]
    vxm = np.zeros_like(v)
    vxm[1:, :] = d * inv
    vxp = np.zeros_like(v)
    vxp[:-1, :] = d * inv
    d = v[:, 1:] - v[:, :-1]
    vym = np.zeros_like(v)
    vym[:, 1:] = d * inv
    vyp = np.zeros_like(v)
    vyp[:, :-1] = d * inv
    return v - dt * (
        np.maximum(ux, 0.0) * vxm
        + np.minimum(ux, 0.0) * vxp
        + np.maximum(uy, 0.0) * vym
        + np.minimum(uy, 0.0) * vyp
    )
