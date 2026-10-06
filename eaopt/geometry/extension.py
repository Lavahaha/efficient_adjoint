"""速度窄带延拓：把边界节点上的速度沿法向铺满窄带（论文 II-C 的延拓步）。

形状导数只定义在边界点（本方案 = marching 交点）上，Hamilton–Jacobi 更新
却需要整条窄带的节点速度：把边界值按"沿法向常值"（∇V·∇φ = 0）铺开。

两条实现：

- **skfmm**（scikit-fmm 的 ``extension_velocities``）：快速行进法同时解
  |∇d|=1 与 ∇V·∇d=0，把界面上的速度延拓到全网格（我们只取窄带内的值）。
- **pde**：``LevelSet2D.extend_velocity`` 的上风对流实现（旧路径，留作对照）。

scikit-fmm 是核心依赖，但这里**懒 import**：没装（或装不上，如新 Python
没有轮子）时 ``auto`` 回退到 PDE 并打印一行告警——延拓方法不同会让同一份
场数据给出不同的速度场，静默降级会让服务器上的结果对不上本地复现。
"""

from __future__ import annotations

import numpy as np

__all__ = ["extend_velocity", "skfmm_available"]

#: 缺库告警只打一次（每轮都打印会把日志淹掉）
_WARNED = {"skfmm": False}


def skfmm_available() -> bool:
    """scikit-fmm 是否可导入（不真的 import，避免拖慢启动）。"""
    import importlib.util

    return importlib.util.find_spec("skfmm") is not None


def extend_velocity(
    ls,
    v_boundary: np.ndarray,
    band_mm: float,
    *,
    method: str = "auto",
    known: np.ndarray | None = None,
    order: int = 2,
    iters: int = 80,
) -> tuple[np.ndarray, str]:
    """窄带速度延拓：返回 ``(V, 实际方法名 ∈ {"skfmm", "pde", "none"})``。

    ``v_boundary``：φ 网格上的速度（只在边界邻域非零）；
    ``band_mm``：只保留 |φ| ≤ band 内的延拓值，带外严格为 0（与旧契约一致）；
    ``known``：可选布尔掩膜 = "边界已知值节点"（scatter 落过点的节点），
    PDE 回退用它当种子（skfmm 不需要：它按 φ 的零等值线自己认界面）；
    ``method``：``auto``（有 skfmm 就用）| ``skfmm``（缺库直接报错）| ``pde``。
    """
    v = np.asarray(v_boundary, dtype=float)
    if v.shape != ls.phi.shape:
        raise ValueError("v_boundary 形状与网格不一致")
    if method not in ("auto", "skfmm", "pde"):
        raise ValueError(
            f'extension_method={method!r} 未知：只认 "auto" / "skfmm" / "pde"')
    if not np.any(v != 0.0) or not _has_interface(ls.phi):
        # 速度全零（全部被掩膜吃掉）或域内没有界面（φ 不换号）→ 无需延拓
        return np.zeros_like(v), "none"
    if method in ("auto", "skfmm"):
        try:
            f_ext = _skfmm_extension(ls, v, order)
        except ImportError:
            if method == "skfmm":
                raise
            _warn_fallback()
        else:
            return np.where(np.abs(ls.phi) <= band_mm, f_ext, 0.0), "skfmm"
    return ls.extend_velocity(v, band_mm, iters=iters, known=known), "pde"


def _skfmm_extension(ls, v: np.ndarray, order: int) -> np.ndarray:
    """调 skfmm.extension_velocities：φ 的零等值线 = 界面，界面上的速度向外铺。"""
    import skfmm  # 懒 import：没装也要能用（auto 回退 PDE）

    res = skfmm.extension_velocities(ls.phi, v, dx=ls.dx, order=order)
    # 2022.2.2 起返回 (d, f_ext)；更老的版本只返回 f_ext
    return np.asarray(res[1] if isinstance(res, tuple) else res, dtype=float)


def _has_interface(phi: np.ndarray) -> bool:
    return bool((phi > 0.0).any() and (phi < 0.0).any())


def _warn_fallback() -> None:
    if _WARNED["skfmm"]:
        return
    _WARNED["skfmm"] = True
    print("[采样] 未安装 scikit-fmm，速度延拓回退到 PDE 上风格式"
          "（pip install scikit-fmm 可用快速行进法；两者的速度场不完全相同）")
