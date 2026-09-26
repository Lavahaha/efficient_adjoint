"""轮廓提取与平滑：零等值面 -> 多边形（世界坐标，mm）。

提取用 matplotlib 的 contour（网格上线性插值，等价 marching squares，
避免自写 16 种情形表的出错风险）；平滑用 scipy B 样条；
重采样按弧长均匀进行。
"""

from __future__ import annotations

import numpy as np
from scipy import interpolate

__all__ = ["extract_contours", "smooth_resample"]


def extract_contours(
    xs: np.ndarray, ys: np.ndarray, phi: np.ndarray, min_cell_length: float = 3.0
) -> list[np.ndarray]:
    """提取 φ=0 等值线。

    xs/ys: 网格坐标（mm）；phi: (nx, ny)。
    返回轮廓顶点列表（世界坐标）；短于 min_cell_length 个网格的碎屑被丢弃。
    闭合轮廓首尾重复。开放轮廓 = 可动金属穿出设计区边界时与红框相交的
    轮廓段（不是设计区外的馈线本身，馈线由 fixed_region/求解器模板负责）：
    它们用于与设计区外的固定多边形拼接成完整金属几何；
    其端点邻域在导数采样时将被速度掩膜固定（taper 到 0）。
    """
    import matplotlib.pyplot as plt

    cs = plt.contour(xs, ys, phi.T, levels=[0.0])
    min_len = min_cell_length * (xs[1] - xs[0])
    paths = []
    for seg in cs.allsegs[0]:
        v = np.asarray(seg)
        if _polyline_length(v) >= min_len:
            paths.append(v)
    plt.close(cs.figure)
    return paths


def smooth_resample(
    vertices: np.ndarray,
    spacing_mm: float,
    smoothing: float = 0.0,
    closed: bool = True,
) -> np.ndarray:
    """B 样条平滑并按弧长均匀重采样。

    vertices: (N,2) 轮廓顶点；spacing_mm: 目标点距；
    smoothing: 样条平滑因子（0 = 插值过点，>0 平滑）；
    closed: 闭合轮廓用周期样条（首尾重复）。
    返回重采样后的顶点数组。
    """
    pts = np.asarray(vertices, dtype=float)
    # 去除相邻重复点（marching squares 输出中常见，splprep 不接受）
    keep = np.ones(len(pts), dtype=bool)
    keep[1:] = np.linalg.norm(np.diff(pts, axis=0), axis=1) > 1e-12
    pts = pts[keep]
    if len(pts) < 4:
        return pts
    if closed:
        tck, _ = interpolate.splprep(pts.T, s=smoothing, per=True)
        dense_u = np.linspace(0.0, 1.0, 2000, endpoint=False)
        n = max(int(round(_closed_length(pts) / spacing_mm)), 4)
        uu = _arc_length_uniform(dense_u, tck, n, closed=True)
        out = np.column_stack(interpolate.splev(uu, tck))
        return np.vstack([out, out[0]])
    tck, _ = interpolate.splprep(pts.T, s=smoothing)
    dense_u = np.linspace(0.0, 1.0, 2000)
    n = max(int(round(_polyline_length(pts) / spacing_mm)), 2)
    uu = _arc_length_uniform(dense_u, tck, n, closed=False)
    return np.column_stack(interpolate.splev(uu, tck))


def _arc_length_uniform(dense_u, tck, n, closed: bool) -> np.ndarray:
    """在样条上按弧长均匀取 n 个参数点 u。"""
    dense = np.column_stack(interpolate.splev(dense_u, tck))
    seg = np.linalg.norm(np.diff(dense, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = cum[-1]
    target = np.linspace(0.0, total, n, endpoint=not closed)
    return np.interp(target, cum, dense_u)


def _polyline_length(pts: np.ndarray) -> float:
    if len(pts) < 2:
        return 0.0
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))


def _closed_length(pts: np.ndarray) -> float:
    if len(pts) < 3:
        return 0.0
    return _polyline_length(pts) + float(np.linalg.norm(pts[-1] - pts[0]))
