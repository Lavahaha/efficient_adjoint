"""轮廓提取与平滑：零等值面 -> 多边形（世界坐标，mm）。

提取用 matplotlib 的 contour（网格上线性插值，等价 marching squares，
避免自写 16 种情形表的出错风险）；平滑用 scipy B 样条；
重采样按弧长均匀进行。
"""

from __future__ import annotations

import numpy as np
from scipy import interpolate

__all__ = [
    "extract_contours",
    "dedupe_vertices",
    "smooth_resample",
    "close_open_contours",
    "self_intersections",
]


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


def dedupe_vertices(vertices: np.ndarray, tol_mm: float = 1e-6) -> np.ndarray:
    """去掉相邻重复/极近顶点（闭合轮廓保留首尾重复一个）。

    零等值线正好穿过网格节点时，marching 会在相邻单元里连续发射同一个
    交点（真实 φ 快照实测约 19% 的顶点是这种零长段）。退化边让下游的
    scatter 出现零长段、也让 CST 的 ``.Create`` 报 "Profile is
    self-intersecting"。容差取 1e-6 mm：远小于任何物理尺度，又远大于
    插值残差（~1e-14）。

    闭合判定 = 首尾距离 ≤ tol；是闭合则删去末点后按环处理重复，最后把
    首点复制到末尾（下游靠首尾**精确**相等判闭合）。
    """
    pts = np.asarray(vertices, dtype=float)
    if len(pts) < 2:
        return pts.copy()
    closed = bool(np.linalg.norm(pts[-1] - pts[0]) <= tol_mm)
    body = pts[:-1] if closed else pts
    if len(body) < 2:
        return body.copy()
    keep = np.ones(len(body), dtype=bool)
    keep[1:] = np.linalg.norm(np.diff(body, axis=0), axis=1) > tol_mm
    if closed and keep[-1] and np.linalg.norm(body[-1] - body[0]) <= tol_mm:
        keep[-1] = False  # 末点与首点重合（闭合点重复）
    out = body[keep]
    if closed:
        out = np.vstack([out, out[0]])
    return out


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

    返回的多边形保证**无自交**（有自交时抛 ValueError 并指出相交的边）：
    CST 的 `Extrude ... .Create` 对自交轮廓只报 "Profile is self-intersecting"，
    在这里拦下能给出可定位的诊断。
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
        # 两条开放轮廓的走向是提取器给的、不保证一致：必须让"末端对末端、
        # 首端对首端"再连，否则把 a 的头接到 b 的尾、闭合路径会从条带内部
        # 斜穿过去（自交）。
        if _pair_cost(a, b) > _pair_cost(a, b[::-1]):
            b = b[::-1]
        walk1 = _perimeter_walk(a[-1], b[-1], corners)
        walk2 = _perimeter_walk(b[0], a[0], corners)
        poly = np.vstack([a, walk1, b[::-1], walk2])
        if not np.allclose(poly[0], poly[-1]):
            poly = np.vstack([poly, poly[0]])
        out.append(poly)
    for k, poly in enumerate(out):
        bad = self_intersections(poly)
        if bad:
            i, j = bad[0]
            raise ValueError(
                f"闭合后的第 {k} 个多边形自交：边 {i} {_fmt(poly[i])}→{_fmt(poly[i + 1])} "
                f"与边 {j} {_fmt(poly[j])}→{_fmt(poly[j + 1])} 相交"
                f"（共 {len(bad)} 处）。检查零等值面轮廓是否成对、走向是否一致。"
            )
    return out


def _pair_cost(a: np.ndarray, b: np.ndarray) -> float:
    """把 a 的末端接 b 的末端、a 的首端接 b 的首端的路径长度。"""
    return float(
        np.linalg.norm(a[-1] - b[-1]) + np.linalg.norm(a[0] - b[0])
    )


def _fmt(p) -> str:
    return f"({p[0]:.4g}, {p[1]:.4g})"


def self_intersections(poly: np.ndarray, tol: float = 1e-9) -> list[tuple[int, int]]:
    """折线中**非相邻**边段的相交对（自交检测）。

    相邻边共享端点、共线接续都属正常，不报；只报真正穿过或共线重叠的
    边对。返回 [(i, j), ...]（边 i = 顶点 i→i+1；闭合折线按环处理）。
    """
    pts = np.asarray(poly, dtype=float)
    if len(pts) < 4:
        return []
    if np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]  # 闭合：末边与首边相邻，去重后按环处理
    n = len(pts)
    segs = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    boxes = [  # 包围盒预筛：绝大多数边对在这里就被排除
        (min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1]))
        for a, b in segs
    ]
    bad = []
    for i in range(n):
        xi0, xi1, yi0, yi1 = boxes[i]
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # 相邻边
            xj0, xj1, yj0, yj1 = boxes[j]
            if xi0 > xj1 + tol or xj0 > xi1 + tol or yi0 > yj1 + tol or yj0 > yi1 + tol:
                continue
            if _segments_cross(*segs[i], *segs[j], tol):
                bad.append((i, j))
    return bad


def _segments_cross(p1, p2, p3, p4, tol: float) -> bool:
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    if (d1 > tol and d2 < -tol or d1 < -tol and d2 > tol) and (
        d3 > tol and d4 < -tol or d3 < -tol and d4 > tol
    ):
        return True  # 严格穿过
    if abs(d1) <= tol and abs(d2) <= tol and abs(d3) <= tol and abs(d4) <= tol:
        return _collinear_overlap(p1, p2, p3, p4, tol)
    return False


def _cross(o, a, b) -> float:
    return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))


def _collinear_overlap(p1, p2, p3, p4, tol: float) -> bool:
    """两条共线线段是否有长度 > tol 的公共部分（仅端点相触不算）。"""
    d = np.asarray(p2) - np.asarray(p1)
    axis = 0 if abs(d[0]) >= abs(d[1]) else 1
    a0, a1 = sorted((p1[axis], p2[axis]))
    b0, b1 = sorted((p3[axis], p4[axis]))
    return float(min(a1, b1) - max(a0, b0)) > tol


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


def _edge_index(p: np.ndarray, corners) -> int:
    """点在外扩 box 第几条边上（角点取序号小的那条）。"""
    p = np.asarray(p, dtype=float)
    edges = [(corners[k], corners[(k + 1) % 4]) for k in range(4)]
    for k, (a, b) in enumerate(edges):
        if _on_segment(p, a, b):
            return k
    raise ValueError(f"点 {_fmt(p)} 不在设计区边界上")


def _perimeter_param(p: np.ndarray, corners) -> float:
    """端点在 box 周长上的参数（用于排序：同侧边端点成对）。"""
    p = np.asarray(p, dtype=float)
    k = _edge_index(p, corners)
    a, b = np.asarray(corners[k]), np.asarray(corners[(k + 1) % 4])
    t = np.linalg.norm(p - a) / max(float(np.linalg.norm(b - a)), 1e-12)
    return k + t


def _perimeter_walk(start: np.ndarray, end: np.ndarray, corners) -> np.ndarray:
    """沿外扩 box 周长从 start 走到 end 的折线（不含两端），取较短的一侧。

    start/end 必须落在周长上。同一侧边上直接连（弦就在那条边上）；跨边时
    绕过中间的角点——**终点所在边的起点角必须包含在内**，否则最后一段会
    从上一个角斜切到终点，斜切段正好横穿金属条带（自交）。
    """
    start = np.asarray(start, dtype=float)
    end = np.asarray(end, dtype=float)
    k0 = _edge_index(start, corners)
    k1 = _edge_index(end, corners)
    if k0 == k1:
        return np.zeros((0, 2))
    ccw = _ccw_corners(k0, k1, corners)
    cw = _ccw_corners(k1, k0, corners)
    return ccw if _walk_length(start, ccw, end) <= _walk_length(start, cw, end) else cw


def _ccw_corners(k0: int, k1: int, corners) -> np.ndarray:
    """沿周长逆时针从边 k0 走到边 k1 途经的角点（含 corners[k1]）。"""
    ks = []
    k = (k0 + 1) % 4
    while True:
        ks.append(k)
        if k == k1:
            break
        k = (k + 1) % 4
    return np.asarray([corners[k] for k in ks], dtype=float)


def _walk_length(start, mids: np.ndarray, end) -> float:
    pts = np.vstack([np.asarray(start, dtype=float), mids, np.asarray(end, dtype=float)])
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))
