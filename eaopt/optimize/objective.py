"""目标函数（FoM）。

按配置的 objective 类型构造 FoM 计算函数；新类型（反射、定向性等）
在此注册。FoM 输入为 Solution，输出实数（越大越好）。
"""

from __future__ import annotations

from typing import Callable

from eaopt.config import CaseConfig
from eaopt.solver.base import Solution

__all__ = ["make_fom"]


def make_fom(cfg: CaseConfig) -> Callable[[Solution], float]:
    """按配置构造 FoM 函数。"""
    obj = cfg.objective
    if obj.type == "transmission":
        key = (obj.to_port, obj.from_port)  # S_{to,from}

        def fom(sol: Solution, key=key) -> float:
            return float(abs(sol.s_params[key]))

        return fom
    raise ValueError(f"未知目标函数类型 {obj.type}")
