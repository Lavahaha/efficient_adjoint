"""形状导数（论文式 25 / 附录式 54–55）的单 z 截面实现。

优化器移动的是 xy 平面内的金属轮廓，因此按二维轮廓定义边界法向与切向。法向
电场为 E_n，磁场只取 xy 平面内沿轮廓切线的分量 H_tau：

    E_n = n_x E_x + n_y E_y
    H_tau = -n_y H_x + n_x H_y
    δp = Re[ phase · (-2jω/P_in) · (ε0 εr E_n E_n^back + µ0 H_tau H_tau^back) ]

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
    if any(a.ndim != 2 or a.shape[1] != 3 for a in
           (e_fwd, h_fwd, e_back, h_back)):
        raise ValueError("e_fwd/h_fwd/e_back/h_back 必须为 (N,3) 数组")
    if any(len(a) != len(e_fwd) for a in (h_fwd, e_back, h_back)):
        raise ValueError("前向、后向 E/H 场的采样点数量必须一致")

    n = np.asarray(normals, dtype=float)
    if n.ndim != 2 or n.shape[0] != len(e_fwd) or n.shape[1] not in (2, 3):
        raise ValueError(f"normals 必须为 ({len(e_fwd)}, 2|3)，收到 {n.shape}")
    n_xy = n[:, :2]
    n_norm = np.linalg.norm(n_xy, axis=1)
    if np.any(~np.isfinite(n_norm)) or np.any(n_norm <= 0.0):
        raise ValueError("每个边界法向都必须是有限且非零的 xy 向量")
    n_xy = n_xy / n_norm[:, None]
    # xy 轮廓的单位切向，只使用 H_xy 在该方向上的投影。
    t_xy = np.column_stack((-n_xy[:, 1], n_xy[:, 0]))

    e_n_fwd = np.sum(e_fwd[:, :2] * n_xy, axis=1)
    e_n_back = np.sum(e_back[:, :2] * n_xy, axis=1)
    h_tau_fwd = np.sum(h_fwd[:, :2] * t_xy, axis=1)
    h_tau_back = np.sum(h_back[:, :2] * t_xy, axis=1)

    integrand = EPS0 * eps_r * e_n_fwd * e_n_back + MU0 * h_tau_fwd * h_tau_back
    return np.real(phase * (-2j * omega / pin) * integrand)
