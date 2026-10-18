"""步长策略（固定步长，两种归一化模式）。

normalize="max"（旧行为）：
  形状导数 δp → 归一化速度 V = sign·δp/max|δp|（|V| ≤ 1）。每轮迭代的
  **最大边界位移 = step_cells × dx**（论文 Fig. 3b：一个网格），由
  pipeline 按 steps = max(1, round(step_cells / cfl)) 折算成 HJ 演化子步数
  （V=±1 处的位移 = cfl·dx·steps ≈ step_cells·dx）。

normalize="none"（2026-10-18 试验：去掉归一化）：
  V = sign·δp 原值——**没有一个点独享 |V|=1、其余按比例缩**，各点按自身
  δp 大小比例移动。总体尺度由 :func:`plan_substeps` 决定：|δp| 的
  ``step_ref_quantile`` 分位点每轮位移恰为 step_cells·dx，其余点按比例
  放大/缩小（超出者不受限幅；实际最大位移还受速度延拓带
  level_set.extension_band_mm 的物理上限约束）。

符号开关 velocity_sign（±1）：论文式 (24)/(31) 的符号矛盾要靠有限差分
验证裁决（当前缺省 +1，**CST 端尚未复核**：先小步数跑、盯 FoM 是否单调
上升，符号错会让它反向跑且不报错；见 docs/server_runbook.md）。
"""

from __future__ import annotations

import numpy as np

__all__ = ["fixed_step_velocity", "plan_substeps", "SUBSTEP_CAP"]

#: normalize="none" 时的子步数上限：孤立大 δp（max/ref 很大）会让子步数
#: 按比例爆炸，而每个子步都是纯网格运算，封顶避免意外慢轮次。触发时打日志。
SUBSTEP_CAP = 2000


def fixed_step_velocity(
    delta_p: np.ndarray,
    velocity_sign: float = 1.0,
    active: np.ndarray | None = None,
    normalize: str = "max",
) -> np.ndarray:
    """δp → 法向速度 V。

    - ``normalize="max"``：V = sign·δp/max|δp|（无量纲，|V|≤1），
      只有 |δp| 最大的点拿到 |V|=1。
    - ``normalize="none"``：V = sign·δp（原值，量纲与 δp 相同）。位移尺度
      由调用方经 :func:`plan_substeps` 的子步数兑现。

    active: 可选布尔掩膜（True = 允许运动）。只对 active 采样点取
    max|δp| 归一化（"max" 模式），inactive 点置零。必须用：被速度掩膜
    禁掉的点（如角点奇异性区域、固定区邻域）不应劫持归一化尺度。

    边界位移正方向与 V 符号的关系：V>0 金属扩张、V<0 收缩
    （由 LevelSet2D.update 的测试固化）。
    """
    if normalize not in ("max", "none"):
        raise ValueError(f'normalize 只认 "max" / "none"，收到 {normalize!r}')
    d = np.asarray(delta_p, dtype=float)
    if active is not None:
        d = np.where(np.asarray(active, dtype=bool), d, 0.0)
    if normalize == "none":
        return velocity_sign * d
    m = float(np.max(np.abs(d)))
    if m == 0.0 or not np.isfinite(m):
        return np.zeros_like(d)
    return velocity_sign * d / m


def plan_substeps(
    delta_p: np.ndarray,
    active: np.ndarray,
    *,
    step_cells: float,
    cfl: float,
    dx: float,
    step_ref_quantile: float = 0.9,
) -> tuple[int, dict]:
    """normalize="none" 的 HJ 子步规划。

    位移公式：p 点每轮位移 = |δp(p)| · steps · cfl·dx / max|δp|（每个子步
    dt = cfl·dx/max|δp|，由 LevelSet2D.update 按 vmax 自适应，子步数决定
    总时长）。取 steps 使参考分位点位移恰为 step_cells·dx：

        steps = ceil(step_cells · max|δp| / (cfl · ref))

    返回 ``(steps, info)``；info 含 ref、vmax 与估算位移的
    p50/p90/max（mm，供日志/诊断）。δp 全零或 active 全禁时返回 (0, info)。
    """
    d = np.asarray(delta_p, dtype=float)
    keep = np.asarray(active, dtype=bool) if active is not None else np.ones(len(d), bool)
    a = np.abs(d[keep])
    info = {"ref": 0.0, "vmax": 0.0,
            "disp_p50_mm": 0.0, "disp_p90_mm": 0.0, "disp_max_mm": 0.0,
            "capped": False}
    vmax = float(np.max(a)) if len(a) else 0.0
    info["vmax"] = vmax
    if vmax == 0.0 or not np.isfinite(vmax):
        return 0, info
    ref = float(np.quantile(a, step_ref_quantile))
    info["ref"] = ref
    if ref <= 0.0:
        return 0, info
    steps = int(np.ceil(step_cells * vmax / (cfl * ref)))
    capped = steps > SUBSTEP_CAP
    if capped:
        steps = SUBSTEP_CAP
        info["capped"] = True
    # 位移 = |δp| · steps · cfl·dx / vmax（mm）
    scale = steps * cfl * dx / vmax
    disp = a * scale
    info["disp_p50_mm"] = float(np.percentile(disp, 50))
    info["disp_p90_mm"] = float(np.percentile(disp, 90))
    info["disp_max_mm"] = float(np.max(disp))
    return steps, info
