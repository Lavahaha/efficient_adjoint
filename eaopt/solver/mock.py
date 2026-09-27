"""MockSolver：物理自洽的玩具求解器（本地开发/验证用）。

模型：2D 拉普拉斯静电场（准静态耦合）。
  - 可动金属（耦合臂）电位 1V，固定金属（直通线）电位 0V；
  - 求解域 = 设计区 + field_margin，域边界 Neumann（∂u/∂n = 0）；
  - 后向场 = 前向场的正交相位副本（E_back = j·E_fwd），使论文式 (25)
    恰好退化为电容形状导数（Hadamard 公式 δC = ε∫|E_⊥|² v_n ds），
    可做解析一致的有限差分验证（比值理论值 = 0.3·P_in/(2ω·C0)）；
  - H = 0：H 项与 E 项结构相同（同样的投影数学），留给 CST 端验证；
  - FoM 映射：C0 = 初始电容，S31 = j·0.3·C/C0（|S31| 随耦合增强单调上升，
    初始值恰为 0.3），S21 = √(1−|S31|²)，其余 S 参数为 0。

几何来源：可动金属直接取水准集 φ（双线性栅格化到求解网格）；
build_model 的多边形参数为 CST 接口通用性保留，本类忽略 movable。
"""

from __future__ import annotations

import numpy as np
from matplotlib.path import Path
from scipy import sparse
from scipy.interpolate import RegularGridInterpolator

from eaopt.adjoint.derivative import EPS0
from eaopt.adjoint.fields import FieldGrid
from eaopt.config import CaseConfig
from eaopt.solver.base import Solution, SolverInterface

__all__ = ["MockSolver"]


class MockSolver(SolverInterface):
    def __init__(self, cfg: CaseConfig, ls):
        self.cfg = cfg
        self.ls = ls
        box = cfg.design_region.box
        m = cfg.design_region.field_margin_mm
        self.dx = float(cfg.design_region.grid_step_mm)
        # 网格整体偏移 dx/2（半格偏移）：使单元中心永不与初始金属
        # 边界重合，避免 φ 栅格化的浮点歧义（曾使 FD 校准失真 2 倍）
        self.x0 = box.x[0] - m - self.dx / 2.0
        self.y0 = box.y[0] - m - self.dx / 2.0
        self.nx = int(round((box.x[1] - box.x[0] + 2.0 * m) / self.dx)) + 2
        self.ny = int(round((box.y[1] - box.y[0] + 2.0 * m) / self.dx)) + 2
        self.x1 = self.x0 + (self.nx - 1) * self.dx
        self.y1 = self.y0 + (self.ny - 1) * self.dx
        self._c0 = None
        self._fwd: Solution | None = None
        self._back: Solution | None = None

    # ------------------------------------------------------------------ #
    # SolverInterface
    # ------------------------------------------------------------------ #
    def build_model(self, movable: list | None = None, fixed: list | None = None) -> None:
        if fixed is None:
            fixed = [np.asarray(p.vertices) for p in self.cfg.fixed_region]
        u, mov, air = self._solve_potential(fixed)
        c = self._capacitance(u, mov, air)
        if self._c0 is None:
            self._c0 = c
        self._make_solutions(u, mov, air, c)
        return None

    def solve_forward(self) -> Solution:
        if self._fwd is None:
            raise RuntimeError("先调用 build_model")
        return self._fwd

    def solve_backward(self) -> Solution:
        if self._back is None:
            raise RuntimeError("先调用 build_model")
        return self._back

    @property
    def capacitance(self) -> float:
        """当前几何的电容（F/m，2D 单位长度）。"""
        if self._fwd is None:
            raise RuntimeError("先调用 build_model")
        return float(abs(self._fwd.s_params[(3, 1)]) / 0.3 * self._c0)

    def movable_extent(self) -> tuple[float, float]:
        """可动金属的 y 范围（mock 栅格，mm）。

        供 FD 验证做栅格化量化校准（有效位移 = 扰动前后顶行位移差）。
        按行单元数阈值过滤固定金属边界上的浮点"尘埃"单元。
        """
        fixed = [np.asarray(p.vertices, dtype=float) for p in self.cfg.fixed_region]
        _, mov, _ = self._solve_potential(fixed)
        ys = np.linspace(self.y0, self.y1, self.ny)
        bulk = mov.sum(axis=0) > 5
        rows = ys[bulk]
        return float(rows.min()), float(rows.max())

    # ------------------------------------------------------------------ #
    # 拉普拉斯求解
    # ------------------------------------------------------------------ #
    def _solve_potential(self, fixed: list[np.ndarray]):
        xs = np.linspace(self.x0, self.x1, self.nx)
        ys = np.linspace(self.y0, self.y1, self.ny)
        X, Y = np.meshgrid(xs, ys, indexing="ij")
        pts = np.stack([X.ravel(), Y.ravel()], axis=1)

        fixed_mask = np.zeros(self.nx * self.ny, dtype=bool)
        for poly in fixed:
            fixed_mask |= Path(poly).contains_points(pts)
        fixed_mask = fixed_mask.reshape(self.nx, self.ny)

        # 可动金属 = φ<0 且不在固定金属内
        phi_interp = RegularGridInterpolator(
            (self.ls.xs, self.ls.ys), self.ls.phi, bounds_error=False, fill_value=1.0
        )
        mov = (phi_interp(pts) < 0.0).reshape(self.nx, self.ny)
        mov &= ~fixed_mask

        metal = mov | fixed_mask
        air = ~metal

        # 组装五点差分系统（Dirichlet 金属；Neumann 域边界）
        n_unk = int(air.sum())
        idx_map = np.full(self.nx * self.ny, -1, dtype=int)
        unk = np.flatnonzero(air.ravel())
        idx_map[unk] = np.arange(n_unk)
        ui, uj = np.unravel_index(unk, (self.nx, self.ny))

        rows, cols, vals = [], [], []
        diag = np.full(n_unk, 4.0)
        b = np.zeros(n_unk)
        r_all = np.arange(n_unk)
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ni, nj = ui + di, uj + dj
            inside = (ni >= 0) & (ni < self.nx) & (nj >= 0) & (nj < self.ny)
            diag[~inside] -= 1.0  # Neumann：幽灵节点 = 自身
            ii, jj = ni[inside], nj[inside]
            k = ii * self.ny + jj
            air_nb = air[ii, jj]
            r = r_all[inside][air_nb]
            rows.extend(r.tolist())
            cols.extend(idx_map[k[air_nb]].tolist())
            vals.extend([-1.0] * len(r))
            r_m = r_all[inside][~air_nb]
            b[r_m] += np.where(mov[ii[~air_nb], jj[~air_nb]], 1.0, 0.0)

        A = sparse.csr_matrix((vals, (rows, cols)), shape=(n_unk, n_unk))
        A += sparse.diags(diag)
        u_sol = sparse.linalg.spsolve(A.tocsc(), b)
        u = np.zeros(self.nx * self.ny)
        u[unk] = u_sol
        u[np.flatnonzero(mov.ravel())] = 1.0
        u[np.flatnonzero(fixed_mask.ravel())] = 0.0
        return u.reshape(self.nx, self.ny), mov, air

    def _capacitance(self, u, mov, air) -> float:
        """C = ε·∮E_⊥ dl（2D 单位长度电容，F/m）。

        臂表面外法向电场 E_⊥ = (u_arm − u_air)/dx = (1 − u_air)/dx，
        各界面边贡献 ε·(1 − u_air)。
        """
        eps = EPS0 * self.cfg.substrate.eps_r
        total = 0.0
        mi = np.argwhere(mov)
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ni, nj = mi[:, 0] + di, mi[:, 1] + dj
            ok = (ni >= 0) & (ni < self.nx) & (nj >= 0) & (nj < self.ny)
            ni, nj = ni[ok], nj[ok]
            sel = air[ni, nj]
            total += float(np.sum(1.0 - u[ni[sel], nj[sel]]))
        return eps * total

    # ------------------------------------------------------------------ #
    # 解 -> Solution
    # ------------------------------------------------------------------ #
    def _make_solutions(self, u, mov, air, c) -> None:
        ex, ey = _electric_field(u, mov | ~air, self.dx)
        ez = np.zeros_like(ex)
        e_fwd = np.stack([ex, ey, ez], axis=-1).astype(complex)
        thickness = float(self.cfg.metal.thickness_mm)
        # z 方向两平面复制（插值域 z∈[0, thickness]）
        e3 = np.stack([e_fwd, e_fwd], axis=2)
        h3 = np.zeros_like(e3)
        grid_fwd = FieldGrid(
            origin=(self.x0, self.y0, 0.0),
            spacing=(self.dx, self.dx, thickness),
            data=e3,
        )
        grid_h = FieldGrid(
            origin=(self.x0, self.y0, 0.0),
            spacing=(self.dx, self.dx, thickness),
            data=h3,
        )
        pin = float(self.cfg.solver.port_power_w)
        s31 = 1j * 0.3 * c / self._c0
        s21 = np.sqrt(max(0.0, 1.0 - abs(s31) ** 2))
        sp = {(3, 1): s31, (2, 1): s21}
        self._fwd = Solution(s_params=dict(sp), e_field=grid_fwd, h_field=grid_h, pin=pin)
        grid_back = FieldGrid(grid_fwd.origin, grid_fwd.spacing, 1j * e3)
        self._back = Solution(
            s_params=dict(sp), e_field=grid_back, h_field=grid_h, pin=pin
        )


def _electric_field(u: np.ndarray, metal: np.ndarray, dx: float):
    """E = −∇u（仅空气单元定义）。

    金属界面单元用单侧差分（法向分量精确），金属单元置零。
    """
    up = np.pad(u, 1, mode="edge")
    mp = np.pad(metal, 1, constant_values=False)
    airp = ~mp
    gx = np.zeros_like(up)
    gy = np.zeros_like(up)
    gx[1:-1, 1:-1] = (up[2:, 1:-1] - up[:-2, 1:-1]) / (2 * dx)
    gy[1:-1, 1:-1] = (up[1:-1, 2:] - up[1:-1, :-2]) / (2 * dx)
    # 单侧修正：空气单元的邻居为金属时，用 (u_金属 − u_空气)/dx
    mask = airp & np.roll(mp, -1, 0)
    gx[mask] = (np.roll(up, -1, 0)[mask] - up[mask]) / dx
    mask = airp & np.roll(mp, 1, 0)
    gx[mask] = (up[mask] - np.roll(up, 1, 0)[mask]) / dx
    mask = airp & np.roll(mp, -1, 1)
    gy[mask] = (np.roll(up, -1, 1)[mask] - up[mask]) / dx
    mask = airp & np.roll(mp, 1, 1)
    gy[mask] = (up[mask] - np.roll(up, 1, 1)[mask]) / dx
    air = ~metal
    return -gx[1:-1, 1:-1] * air, -gy[1:-1, 1:-1] * air
