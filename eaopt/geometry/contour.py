"""轮廓提取与平滑：零等值面 -> 多边形（世界坐标，mm）。

提取用 matplotlib 的 contour（网格上线性插值，等价 marching squares，
避免自写 16 种情形表的出错风险）；平滑用 scipy B 样条；
重采样按弧长均匀进行。
"""

from __future__ import annotations

import numpy as np
from scipy import interpolate

__all__ = ["extract_contours", "smooth_resample", "close_open_contours"]


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


def close_open_contours(
    contours: list[np.ndarray], box, pad_mm: float = 0.05
) -> list[np.ndarray]:
    """把穿出设计区边界的开放轮廓闭合为多边形（供求解器几何重建）。

    成对闭合（两条开放轮廓围成一个金属条，如耦合臂的上下边）：
      A + 沿 box 边界从 A 终点走到 B 终点 + 反转的 B
      + 沿 box 边界从 B 起点走回 A 起点
    闭合轮廓原样保留。pad_mm：沿 box 边界向外扩的余量（与设计区外
    固定馈线重叠，同材料在求解器中自动合并）。
    """
    opens = [c for c in contours if not np.allclose(c[0], c[-1])]
    closed = [c for c in contours if np.allclose(c[0], c[-1])]
    if len(opens) % 2 != 0:
        raise ValueError(f"开放轮廓数 {len(opens)} 不是偶数，无法成对闭合")
    corners = _padded_corners(box, pad_mm)
    # 端点先沿所在边外法向延伸 pad（原始 box 边界 → 外扩周长）
    opens = [_extend_open_endpoints(c, box, pad_mm) for c in opens]
    opens.sort(key=lambda c: _perimeter_param(c[0], corners))
    out = list(closed)
    for i in range(0, len(opens), 2):
        a, b = opens[i], opens[i + 1]
        walk1 = _perimeter_walk(a[-1], b[-1], corners)
        walk2 = _perimeter_walk(b[0], a[0], corners)
        poly = np.vstack([a, walk1, b[::-1], walk2])
        if not np.allclose(poly[0], poly[-1]):
            poly = np.vstack([poly, poly[0]])
        out.append(poly)
    return out


def _extend_open_endpoints(c: np.ndarray, box, pad_mm: float) -> np.ndarray:
    """把开放轮廓两端沿所在 box 边的外法向延伸 pad_mm。"""
    x0, x1 = box.x
    y0, y1 = box.y
    orig = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    edges = [(np.asarray(orig[k]), np.asarray(orig[(k + 1) % 4])) for k in range(4)]
    out = np.asarray(c, dtype=float).copy()
    for idx in (0, -1):
        p = out[idx]
        k = next(k for k, (a, b) in enumerate(edges) if _on_segment(p, a, b))
        if k == 0:   # 下边 → 向下
            p = p + [0.0, -pad_mm]
        elif k == 1:  # 右边 → 向右
            p = p + [pad_mm, 0.0]
        elif k == 2:  # 上边 → 向上
            p = p + [0.0, pad_mm]
        else:         # 左边 → 向左
            p = p + [-pad_mm, 0.0]
        out[idx] = p
    return out


def _padded_corners(box, pad_mm: float) -> list[tuple[float, float]]:
    """box 外扩 pad_mm 后的四个角点（逆时针，自左下起）。"""
    x0, x1 = box.x
    y0, y1 = box.y
    return [
        (x0 - pad_mm, y0 - pad_mm),
        (x1 + pad_mm, y0 - pad_mm),
        (x1 + pad_mm, y1 + pad_mm),
        (x0 - pad_mm, y1 + pad_mm),
    ]


def _on_segment(p: np.ndarray, a, b, tol: float = 1e-6) -> bool:
    """p 是否在（外扩后 box 的）边 ab 上。"""
    p = np.asarray(p, dtype=float)
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    cross = abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]))
    if cross > tol * max(1.0, np.linalg.norm(b - a)):
        return False
    return bool(
        min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
        and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol
    )


def _perimeter_param(p: np.ndarray, corners) -> float:
    """端点在 box 周长上的参数（用于排序：同侧边端点成对）。"""
    p = np.asarray(p, dtype=float)
    edges = [(corners[k], corners[(k + 1) % 4]) for k in range(4)]
    for k, (a, b) in enumerate(edges):
        if _on_segment(p, a, b):
            # 边内参数：k 边 + 沿边比例
            seg = np.linalg.norm(np.asarray(b) - np.asarray(a))
            t = np.linalg.norm(p - np.asarray(a)) / max(seg, 1e-12)
            return k + t
    raise ValueError(f"点 {p} 不在设计区边界上")


def _perimeter_walk(start: np.ndarray, end: np.ndarray, corners) -> np.ndarray:
    """沿外扩 box 周长（逆时针）从 start 走到 end 的折线（不含两端）。"""
    start = np.asarray(start, dtype=float)
    end = np.asarray(end, dtype=float)
    edges = [(np.asarray(corners[k]), np.asarray(corners[(k + 1) % 4]))
             for k in range(4)]
    k0 = next(k for k, (a, b) in enumerate(edges) if _on_segment(start, a, b))
    k1 = next(k for k, (a, b) in enumerate(edges) if _on_segment(end, a, b))
    pts = []
    k = (k0 + 1) % 4
    while k != k1:
        pts.append(np.asarray(corners[k]))
        k = (k + 1) % 4
    if len(pts) == 0:
        return np.zeros((0, 2))
    return np.asarray(pts)
