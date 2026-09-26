"""形状导数（论文式 25 / 式 55）。

    δp_i = Re[ −(2jω/P_in) · ( ε E_⊥,i·E_⊥,i^back + μ0 H_∥,i·H_∥,i^back ) ]

要点：
  - n̂ 取金属外法向；δp_i 的符号物理含义（金属生长/收缩与 FoM 增减的
    关系）由第 4 步的有限差分验证裁决，本模块只忠实实现公式；
  - 复场点积不取共轭：Re[−2jω X] = 2ω·Im[X]，导数信息全部来自
    前向场与后向场之间的相位差（全实场导数为零）；
  - 量纲：式 (25) 逐点值量纲为 1/m（对位移的导数），实际使用中作为
    水准集法向速度（时间步长 δt 吸收单位），FD 验证确认数值一致性。
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
    normals: np.ndarray,
    pin: float,
    omega: float,
    eps_r: float,
) -> np.ndarray:
    """按式 (25) 计算各采样点的形状导数。

    e_fwd/h_fwd: 前向场 (N,3) complex（V/m, A/m）；
    e_back/h_back: 后向场 (N,3) complex（激励移到观测端口）；
    normals: (N,3) 金属外法向（自动归一化）；
    pin: 入射功率 (W)；omega: 角频率 (rad/s)；eps_r: 相对介电常数。
    返回 (N,) 实数组 δp。
    """
    n = normals / np.linalg.norm(normals, axis=1, keepdims=True)
    e_n = np.einsum("ij,ij->i", e_fwd, n)
    e_n_back = np.einsum("ij,ij->i", e_back, n)
    h_t = h_fwd - np.einsum("ij,ij->i", h_fwd, n)[:, None] * n
    h_t_back = h_back - np.einsum("ij,ij->i", h_back, n)[:, None] * n
    h_dot = np.einsum("ij,ij->i", h_t, h_t_back)
    integrand = EPS0 * eps_r * e_n * e_n_back + MU0 * h_dot
    return np.real(-2j * omega / pin * integrand)
