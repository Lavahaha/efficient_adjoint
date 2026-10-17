"""形状导数（论文式 25 / 附录式 54–55）的单 z 截面实现。

优化器移动的是 xy 平面内的金属轮廓，因此边界法向位于 xy 平面。对沿 z
挤出的局部侧壁，法向电场为 E_n，切向磁场包含轮廓切向分量 H_tau 与 H_z：

    E_n = n_x E_x + n_y E_y
    H_t · H_t^back = H_tau H_tau^back + H_z H_z^back
    δp = Re[ phase · (-2jω/P_in) · (ε0 εr E_n E_n^back + µ0 H_t·H_t^back) ]

这里按要求只在配置指定的一个 z 截面计算，不对金属厚度方向积分。

相位因子默认取 1；FoM 为 |S| 时若要严格求其导数，应显式传入
e^{−j·arg S}。当前优化流程按用户设置不乘这个因子。
"""

from __future__ import annotations

import numpy as np

__all__ = ["EPS0", "MU0", "shape_derivative"]

EPS0 = 8.8541878128e-12  # 真空介电常数 (F/m)
MU0 = 4.0e-7 * np.pi  # 真空磁导率 (H/m)


def shape_derivative(
    e_fwd: np.ndarray,
    h_fwd: np.ndarray,
    e_back: np.ndarray,
    h_back: np.ndarray,
    pin: float,
    omega: float,
    eps_r: float,
    normals: np.ndarray,
    *,
    phase: complex = 1.0,
) -> np.ndarray:
    """按面内边界法向，计算单 z 截面上的各采样点形状导数。

    e_fwd/h_fwd: 前向场 (N,3) complex（V/m, A/m）；
    e_back/h_back: 后向场 (N,3) complex（激励移到观测端口）；
    pin: 入射功率 (W)；omega: 角频率 (rad/s)；
    eps_r: 采样面所在介质的相对介电常数；
    normals: xy 边界法向 (N,2) 或 (N,3)，按行对应场采样点；
    phase: 可选复数乘子；省略时取 1，不进行 |S| 的相位链式修正。
    返回 (N,) 实数组 δp。
    """
    e_fwd = np.asarray(e_fwd)
    h_fwd = np.asarray(h_fwd)
    e_back = np.asarray(e_back)
    h_back = np.asarray(h_back)
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    n = normals / np.where(length < 1e-30, 1.0, length)
    e_n = np.einsum("ij,ij->i", e_fwd, n)
    e_n_back = np.einsum("ij,ij->i", e_back, n)
    h_t = h_fwd - np.einsum("ij,ij->i", h_fwd, n)[:, None] * n
    h_t_back = h_back - np.einsum("ij,ij->i", h_back, n)[:, None] * n
    h_dot = np.einsum("ij,ij->i", h_t, h_t_back)
    integrand = EPS0 * eps_r * e_n * e_n_back + MU0 * h_dot
    delta = np.real(-2j * omega / pin * integrand)
    # 法向退化（几何角点/域边界处）该点导数置零，避免 0/0
    return np.where(length[:, 0] < 1e-30, 0.0, delta)
