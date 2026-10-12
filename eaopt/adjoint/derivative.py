"""形状导数（论文式 25 / 附录式 54–55）。

    δp_i = Re[ e^{−j·arg S} · (−2jω/P_in) · ( ε0 εr E_⊥·E_⊥^back + µ0 H_∥·H_∥^back ) ]

**配对取式 (54) 原文**（"the tangential electric field and normal magnetic
field are zero" ⇒ E_⊥ 是金属面法向分量、H_∥ 是金属面切向分量）。设计区是
xy 平面内的金属薄片，面法向为 ẑ，于是

    E_⊥ = E_z，   H_∥ = (H_x, H_y)（完整面内磁场）

注意这与"把边界当竖直侧壁、按面内轮廓法向 n̂ 读值"（ε E_n E_n^b + μ0 H_∥侧壁
H_∥侧壁^b，侧壁切向 = H_z + H_τ）**不是**同一个配对，后者在顶面/底面（真正
承载电流的 PEC 面）上取的是切向电场与法向磁场——式 (54) 里恰为零的两项。
divider iter_000 实测（TL 敏感度目标）：错误配对 r = −0.48，式 (54) 配对
r = +0.84（2026-10-12）。

**相位因子**：目标 FoM = |S| 时，由 (5) 式 dFoM/dp_i = 2Re[(∂f/∂E)(dE/dp_i)]
中的 ∂f/∂E 带出 e^{−j·arg S}。缺它等于在求 Re[S] 的导数，方向会整体跑偏，
且**不会报错**——因此本函数把 phase 设为必填关键字参数。

要点：
  - 复场点积不取共轭：Re[−2jω X] = 2ω·Im[X]，导数信息全部来自前向场与
    后向场之间的相位差（全实场导数为零）；
  - 量纲：逐点值量纲为 1/m（对位移的导数），实际使用中作为水准集法向速度
    （时间步长 δt 吸收单位），FD 验证确认数值一致性。
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
    *,
    phase: complex,
) -> np.ndarray:
    """按式 (54)–(55) 计算各采样点的形状导数。

    e_fwd/h_fwd: 前向场 (N,3) complex（V/m, A/m）；
    e_back/h_back: 后向场 (N,3) complex（激励移到观测端口）；
    pin: 入射功率 (W)；omega: 角频率 (rad/s)；
    eps_r: 采样面所在介质的相对介电常数（基板内 = 基板 εr）；
    phase: |S| 目标的链式法则因子 e^{−j·arg S}，S 取 FoM 用的那个 S 参数
        （见 optimize/objective.py::fom_phase）。别给它默认值：默认 1 就是
        "漏乘相位"那条静默 bug 本身。
    返回 (N,) 实数组 δp。
    """
    e_perp = e_fwd[:, 2] * e_back[:, 2]
    h_par = h_fwd[:, 0] * h_back[:, 0] + h_fwd[:, 1] * h_back[:, 1]
    integrand = EPS0 * eps_r * e_perp + MU0 * h_par
    return np.real(phase * (-2j * omega / pin) * integrand)
