"""步长策略（先固定步长，后续可扩展自适应）。

形状导数 δp（量纲 1/m）→ 归一化速度：|V| ≤ 1。
每轮迭代的边界最大位移 = step_size（mm），由 pipeline 按
  steps = max(1, round(step_size / (cfl·dx)))
折算成 HJ 演化步数（V=±1 处的位移 = cfl·dx·steps ≈ step_size）。

符号开关 velocity_sign（±1）：论文式 (24)/(31) 的符号矛盾由
有限差分验证裁决，mock 世界已验证 sign=+1 使 FoM 上升。
"""

from __future__ import annotations

import numpy as np

__all__ = ["fixed_step_velocity"]


def fixed_step_velocity(
    delta_p: np.ndarray, velocity_sign: float = 1.0, active: np.ndarray | None = None
) -> np.ndarray:
    """δp → 归一化法向速度：V = sign·δp/max|δp|（无量纲，|V|≤1）。

    active: 可选布尔掩膜（True = 允许运动）。只对 active 采样点取
    max|δp| 归一化，inactive 点置零。必须用：被速度掩膜禁掉的点
    （如角点奇异性区域、固定区邻域）不应劫持归一化尺度。

    边界位移正方向与 V 符号的关系：V>0 金属扩张、V<0 收缩
    （由 LevelSet2D.update 的测试固化）。
    """
    d = np.asarray(delta_p, dtype=float)
    if active is not None:
        d = np.where(np.asarray(active, dtype=bool), d, 0.0)
    m = float(np.max(np.abs(d)))
    if m == 0.0 or not np.isfinite(m):
        return np.zeros_like(d)
    return velocity_sign * d / m
