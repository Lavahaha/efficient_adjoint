"""目标函数（FoM）。

按配置的 objective 类型构造 FoM 计算函数；新类型（反射、定向性等）
在此注册。FoM 输入为 Solution，输出实数（越大越好）。

FoM 与形状导数使用同一个 S 参数下标。单频目标对 |S| 使用相位链式因子；
多频目标最大化各频点相对门槛的 dB 余量 soft-min。
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from eaopt.config import CaseConfig
from eaopt.solver.base import Solution

__all__ = ["make_fom", "fom_key",
           "multi_frequency_fom", "multi_frequency_weights"]


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
        if cfg.objective.frequencies_ghz:
            return multi_frequency_fom({f: row.s_params for f, row in sol.frequency_results.items()}, cfg)[0]
        return float(abs(sol.s_params[key]))

    return fom


def multi_frequency_weights(margins_db, beta_per_db: float) -> np.ndarray:
    """Return soft-min gradient weights for dB margins, stably normalized."""
    margins = np.asarray(margins_db, dtype=float)
    if margins.ndim != 1 or margins.size < 2 or not np.all(np.isfinite(margins)):
        raise ValueError("margins_db 必须是至少含两个有限值的一维数组")
    if not np.isfinite(beta_per_db) or beta_per_db <= 0:
        raise ValueError("beta_per_db 必须为有限正数")
    z = -float(beta_per_db) * (margins - np.min(margins))
    weights = np.exp(z)
    return weights / np.sum(weights)


def multi_frequency_fom(s_params_by_freq, cfg: CaseConfig):
    """Soft minimum of S-parameter dB margins; returns F, margins, weights.

    ``s_params_by_freq`` maps GHz float keys to ordinary ``{(to, from): S}``
    mappings. The objective maximizes the weakest margin above ``target_db``.
    """
    freqs = cfg.objective.frequencies_ghz
    if not freqs:
        raise ValueError("多频 FoM 需要 objective.frequencies_ghz")
    key = fom_key(cfg)
    magnitudes = []
    for freq in freqs:
        try:
            value = complex(s_params_by_freq[float(freq)][key])
        except (KeyError, TypeError) as exc:
            raise KeyError(f"缺少 {float(freq):g} GHz 的 S{key[0]}{key[1]}") from exc
        mag = abs(value)
        if not np.isfinite(mag) or mag <= 0:
            raise ValueError(f"S{key[0]}{key[1]}({float(freq):g} GHz) 幅值必须为有限正数")
        magnitudes.append(mag)
    db = 20.0 * np.log10(np.asarray(magnitudes))
    margins = db - float(cfg.objective.target_db)
    beta = float(cfg.objective.beta_per_db)
    weights = multi_frequency_weights(margins, beta)
    m_min = float(np.min(margins))
    fom = m_min - float(np.log(np.mean(np.exp(-beta * (margins - m_min)))) / beta)
    return float(fom), margins, weights
