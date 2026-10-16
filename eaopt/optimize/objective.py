"""目标函数（FoM）。

按配置的 objective 类型构造 FoM 计算函数；新类型（反射、定向性等）
在此注册。FoM 输入为 Solution，输出实数（越大越好）。

FoM 与形状导数使用同一个 S 参数下标。当前优化流程按配置直接使用复数 S
参数导数，不乘 :func:`fom_phase`；因此虽仍以 |S| 作为 FoM，更新方向不是其严格梯度。
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from eaopt.config import CaseConfig
from eaopt.solver.base import Solution

__all__ = ["make_fom", "fom_key", "fom_phase"]


def fom_key(cfg: CaseConfig) -> tuple[int, int]:
    """FoM 对应的 S 参数下标 (响应端口, 激励端口)。"""
    obj = cfg.objective
    if obj.type == "transmission":
        return (obj.to_port, obj.from_port)  # S_{to,from}
    raise ValueError(f"未知目标函数类型 {obj.type}")


def make_fom(cfg: CaseConfig) -> Callable[[Solution], float]:
    """按配置构造 FoM 函数。"""
    key = fom_key(cfg)

    def fom(sol: Solution, key=key) -> float:
        return float(abs(sol.s_params[key]))

    return fom


def fom_phase(cfg: CaseConfig, sol: Solution) -> complex:
    """|S| 目标的链式法则因子 e^{−j·arg S}（论文式 5 里的 ∂f/∂E 相位）。

    FoM = |S| 是实值函数，dFoM = Re[e^{−j·arg S}·dS]；形状导数按式 (55)
    算的是 dS 那一项，必须乘上这个因子才等价。缺它 = 在求 Re[S] 的导数，
    优化方向整体跑偏且不报错（divider 实测 arg S21 ≈ −115°）。
    """
    return complex(np.exp(-1j * np.angle(sol.s_params[fom_key(cfg)])))
