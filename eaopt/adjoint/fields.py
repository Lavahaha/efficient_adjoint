"""场数据与插值（求解器输出 -> 形状导数输入）。

求解器（CST / Mock）把 E、H 复矢量场导出在规则笛卡尔网格上
（设计区包围盒 + 余量），FieldGrid 统一承载并支持两种取场方式：

- :meth:`FieldGrid.interp`：三线性插值（旧轮廓方案用）；
- :func:`build_wls_stencil` / :meth:`FieldGrid.sample_wls`：**加权最小二乘**
  （新交点方案用）。导出网格的原点由 CST 包围盒决定、与 φ 网格不一定对齐，
  所以采样点一般不在场网格节点上；WLS 在采样点邻域内拟合局部多项式、
  取多项式在采样点处的值，比三线性插值更稳（尤其单侧邻域：PEC 两侧的场
  不连续，只用同侧节点拟合才不会被金属体内的 E≈0 拉低）。

法向/切向分解服务于形状导数（论文式 25）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.interpolate import RegularGridInterpolator

__all__ = [
    "FieldGrid",
    "WlsStencil",
    "WlsResult",
    "build_wls_stencil",
    "apply_wls_stencil",
    "decompose_normal_tangential",
]


@dataclass
class FieldGrid:
    """规则笛卡尔网格上的复矢量场（E: V/m，H: A/m）。

    origin: (x0, y0, z0) mm；spacing: (dx, dy, dz) mm；
    data: (nx, ny, nz, 3) complex。z 轴与设计平面法向一致。
    """

    origin: tuple[float, float, float]
    spacing: tuple[float, float, float]
    data: np.ndarray  # (nx, ny, nz, 3) complex

    def __post_init__(self):
        self.data = np.asarray(self.data)
        if self.data.ndim != 4 or self.data.shape[3] != 3:
            raise ValueError("data 形状必须为 (nx, ny, nz, 3)")
        self._interp = None

    def axes(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """各轴坐标（mm）。"""
        return tuple(
            self.origin[k] + self.spacing[k] * np.arange(self.data.shape[k])
            for k in range(3)
        )

    def interp(self, points: np.ndarray) -> np.ndarray:
        """三线性插值到 points (N,3) mm，返回 (N,3) complex。

        网格外返回 0（bounds_error=False, fill_value=0）。
        插值器按需构建并缓存（每次迭代大量采样点复用）。
        """
        if self._interp is None:
            self._interp = RegularGridInterpolator(
                self.axes(), self.data, bounds_error=False, fill_value=0.0
            )
        return self._interp(np.asarray(points, dtype=float))

    def plane(self, iz: int) -> np.ndarray:
        """第 ``iz`` 个 z 面的场（nx, ny, 3）complex。"""
        return self.data[:, :, int(iz), :]

    def nearest_z_plane(self, z_mm: float) -> tuple[int, float]:
        """吸附到最近的导出 z 面，返回 ``(iz, z_used)``。

        z 面由 CST 包围盒原点决定，一般不在 z=0 上；调用方须用返回的
        ``z_used`` 做"有没有落进金属体内"的判据（见 pipeline）。
        """
        ax = self.axes()[2]
        iz = int(np.argmin(np.abs(ax - float(z_mm))))
        return iz, float(ax[iz])

    def sample_wls(
        self,
        points: np.ndarray,
        iz: int,
        *,
        node_mask: np.ndarray | None = None,
        stencil: WlsStencil | None = None,
        **kw,
    ) -> "WlsResult":
        """WLS 取场：points (N,2|3) mm → (N,3) complex。

        ``stencil`` 可传入共享的算子（fwd/bwd 四个场必须用**同一套**算子，
        这是伴随法"同一离散"的前提）；不给则按 ``node_mask`` 现建。
        """
        pts = np.asarray(points, dtype=float)
        xy = pts[:, :2] if pts.ndim == 2 and pts.shape[1] >= 3 else pts
        if stencil is None:
            stencil = build_wls_stencil(xy, self.axes()[:2], node_mask, **kw)
        values = apply_wls_stencil(self.plane(iz), stencil)
        return WlsResult(
            values=values,
            n_fallback=stencil.n_fallback,
            n_neighbors=stencil.n_neighbors,
            radius_mm=stencil.radius_mm,
        )


@dataclass
class WlsStencil:
    """移动最小二乘插值的**线性算子**：``f(x_p) ≈ Σ_j λ_j f_j``。

    权重 λ 在建算子时一次解出，因此

    - 一次构建、**四个场共用**（fwd/bwd 的 E/H 用同一套 λ → 与伴随法
      "同一离散"的要求一致）；
    - 作用只是"按下标取值 + 加权求和"，与场的具体数值无关。

    idx: (N, M) 场平面数组（C 序展开）下标；-1 = 空槽（对应权重恒为 0）；
    weight: (N, M) float，每行和 = 1（常数场被精确复现）；
    n_neighbors: (N,) 实际参与拟合的节点数；
    radius_mm: (N,) 实际使用的邻域半径（放大后）；
    point_ok: (N,) False = 最小二乘未成功、退回同侧 0 阶 IDW。
    """

    idx: np.ndarray
    weight: np.ndarray
    n_neighbors: np.ndarray
    radius_mm: np.ndarray
    point_ok: np.ndarray
    shape: tuple[int, int]
    order: int

    @property
    def n_fallback(self) -> int:
        """走 0 阶 IDW 回退的点数（0 = 全部点都完成了局部最小二乘拟合）。"""
        return int((~np.asarray(self.point_ok, dtype=bool)).sum())


@dataclass
class WlsResult:
    """WLS 取场结果（``values`` 之外的字段供诊断/日志用）。"""

    values: np.ndarray
    n_fallback: int
    n_neighbors: np.ndarray
    radius_mm: np.ndarray


def build_wls_stencil(
    points: np.ndarray,
    axes: tuple[np.ndarray, np.ndarray],
    node_mask: np.ndarray | None = None,
    *,
    radius_cells: float = 2.0,
    order: int = 1,
    min_neighbors: int = 6,
    radius_max_cells: float = 4.0,
    eta: float = 0.1,
) -> WlsStencil:
    """在采样点 ``points`` (N,2) 上构建 WLS 线性算子（场网格由 ``axes`` 给出）。

    数学（Σ 只遍历**可用邻域** ``N(p)``）::

        邻域  N(p) = { j : |x_j − x_p| ≤ R , node_mask[j] }
        基    order=1: [1, u, v]；order=2: [1, u, v, u², uv, v²]
              u = (x_j − x_p)/R, v = (y_j − y_p)/R（按 R 归一化 → 条件数与尺度无关）
        权重  w_j = 1 / (|x_j − x_p|² + (η·h)²)，h = min(hx, hy)（软化因子：采样点
              正好落在节点上也不除零）
        解    min_c Σ_j w_j |A_j·c − f_j|²  →  (AᵀWA) c = AᵀW f
        估计  f(x_p) ≈ c₀ = λ·f，λᵀ = e₀ᵀ(AᵀWA)⁻¹AᵀW

    邻域不足（节点数 < ``min_neighbors`` 或正规方程病态 cond > 1e10）时把半径
    依次放大 1.5×、2×（封顶 ``radius_max_cells``）；仍不行则该点退回 0 阶 IDW
    （λ ∝ 1/d²，非负、永不奇异）并在 ``point_ok`` 里记一笔。

    ``node_mask``（(nx,ny) bool）用于**只取一侧节点**：PEC 界面两侧的场不连续、
    金属体内 E≈0，两侧混着拟合会把 δp 系统性压低。掩膜由采样侧决定，见
    ``sampling.field_node_mask``。
    """
    from scipy.spatial import cKDTree

    if order not in (1, 2):
        raise ValueError(f"WLS 阶数只支持 1 或 2，收到 {order}")
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] < 2:
        raise ValueError("points 形状必须为 (N, 2)（mm）")
    pts = pts[:, :2]
    xs = np.asarray(axes[0], dtype=float)
    ys = np.asarray(axes[1], dtype=float)
    h = min(abs(float(xs[1] - xs[0])), abs(float(ys[1] - ys[0])))
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    shape = X.shape
    flat = np.arange(X.size).reshape(shape)
    if node_mask is None:
        keep = np.ones(shape, dtype=bool)
    else:
        keep = np.asarray(node_mask, dtype=bool)
        if keep.shape != shape:
            raise ValueError(
                f"node_mask 形状 {keep.shape} 与场平面 {shape} 不符"
            )
    nodes = np.column_stack([X.ravel(), Y.ravel()])[keep.ravel()]
    flat_idx = flat.ravel()[keep.ravel()]

    n_param = 6 if order == 2 else 3
    need = max(int(min_neighbors), n_param)
    r0 = float(radius_cells) * h
    rmax = float(radius_max_cells) * h
    radii: list[float] = []
    for f in (1.0, 1.5, 2.0):
        r = min(r0 * f, rmax)
        if not radii or r > radii[-1] + 1e-12:
            radii.append(r)

    tree = cKDTree(nodes) if len(nodes) else None
    idx_rows: list[np.ndarray] = []
    w_rows: list[np.ndarray] = []
    n_nb = np.zeros(len(pts), dtype=int)
    r_used = np.full(len(pts), radii[-1], dtype=float)
    ok_rows = np.zeros(len(pts), dtype=bool)
    for k, p in enumerate(pts):
        lam = None
        j: list[int] = []
        if tree is not None:
            for r in radii:
                j = tree.query_ball_point(p, r)
                if len(j) < need:
                    continue
                lam = _wls_weights(p, nodes[j], r, order, h, eta)
                if lam is not None:
                    r_used[k] = r
                    ok_rows[k] = True
                    break
            if lam is None:  # 回退：同侧 0 阶 IDW（最大半径邻域）
                j = tree.query_ball_point(p, radii[-1])
                lam = _idw_weights(p, nodes[j], h, eta)
        if lam is None or len(j) == 0:
            idx_rows.append(np.zeros(0, dtype=int))
            w_rows.append(np.zeros(0))
            continue
        idx_rows.append(flat_idx[np.asarray(j, dtype=int)])
        w_rows.append(lam)
        n_nb[k] = len(j)
    width = max((len(a) for a in idx_rows), default=0)
    stencil_idx = np.full((len(pts), width), -1, dtype=int)
    stencil_w = np.zeros((len(pts), width))
    for k, (a, b) in enumerate(zip(idx_rows, w_rows)):
        stencil_idx[k, : len(a)] = a
        stencil_w[k, : len(b)] = b
    return WlsStencil(
        idx=stencil_idx, weight=stencil_w, n_neighbors=n_nb, radius_mm=r_used,
        point_ok=ok_rows, shape=shape, order=order,
    )


def apply_wls_stencil(plane: np.ndarray, stencil: WlsStencil) -> np.ndarray:
    """把算子作用到一个 z 平面上：(nx, ny, 3) complex → (N, 3) complex。"""
    p = np.asarray(plane)
    if p.shape[:2] != stencil.shape:
        raise ValueError(f"场平面形状 {p.shape[:2]} 与算子 {stencil.shape} 不符")
    vals = p.reshape(-1, p.shape[-1])[stencil.idx]  # (N, M, 3)
    return np.einsum("nm,nmc->nc", stencil.weight, vals)


def _wls_weights(
    p: np.ndarray, nb: np.ndarray, radius: float, order: int, h: float, eta: float
) -> np.ndarray | None:
    """解一次局部最小二乘，返回 c₀ 对应的权重 λ（(m,)）；病态返回 None。"""
    d = nb - p
    dist2 = np.einsum("ij,ij->i", d, d)
    w = 1.0 / (dist2 + (eta * h) ** 2)
    u = d[:, 0] / radius
    v = d[:, 1] / radius
    cols = [np.ones_like(u), u, v]
    if order == 2:
        cols += [u * u, u * v, v * v]
    A = np.column_stack(cols)
    G = A.T @ (w[:, None] * A)
    if np.linalg.cond(G) > 1e10:
        return None
    try:
        return np.linalg.solve(G, (w[:, None] * A).T)[0]
    except np.linalg.LinAlgError:  # pragma: no cover - cond 已先拦一遍
        return None


def _idw_weights(
    p: np.ndarray, nb: np.ndarray, h: float, eta: float
) -> np.ndarray | None:
    """0 阶反距离平方权重（回退用）：λ ∝ 1/(d² + (ηh)²)，归一化。"""
    if len(nb) == 0:
        return None
    d = nb - p
    w = 1.0 / (np.einsum("ij,ij->i", d, d) + (eta * h) ** 2)
    return w / w.sum()


def decompose_normal_tangential(
    field: np.ndarray, normals: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """把场分解为法向与切向分量。

    field: (N,3) complex；normals: (N,3)（自动归一化）。
    复矢量点积不取共轭（与论文推导一致；符号问题由 FD 验证裁决）。
    返回 (field_normal, field_tangential)，均为 (N,3)。
    """
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    n = normals / np.where(length < 1e-30, 1.0, length)
    dot = np.einsum("ij,ij->i", field, n)
    normal_part = dot[:, None] * n
    return normal_part, field - normal_part
