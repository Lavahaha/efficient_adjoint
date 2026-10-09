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
    "simplify_polygon",
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


#: 送进 CST 的轮廓简化容差 = **网格步长**的这个倍数（调用方乘出来给
#: :func:`close_open_contours`）：0.2 格 = 20 µm（dx=0.1 时）。取这个值
#: 是因为它远小于提取精度（一个网格）本身，又足以吃掉"斜边擦过格点"挤出来
#: 的那类微段。
#:
#: **不做"自动容差"**：拿轮廓自己的段长统计（中位/分位）去定容差看着省事，
#: 但长短段混排的多边形会给出发散的值——3×0.1 的矩形中位段长 1.55 mm，
#: 0.2 倍就是 0.31 mm，足以把整个短边吃掉（`tests/test_cst_solver.py` 与
#: `tests/test_cst_update_script.py` 里那几个手写矩形当场被压成 3 个点）。
#: 网格步长只有调用方知道，就由调用方给。
SIMPLIFY_TOL_CELLS = 0.2

#: 简化后**每个多边形**的点数上限（超了就把容差翻倍重试）。CST 的
#: ``Extrude ... .Create`` 对点表有容量：实测 249 点 / 6.9 KB 可用、
#: 497 点 / 13.7 KB 可用、854 点 / 23.6 KB 直接报 "Profile is
#: self-intersecting"。取 250 = 已知能用里最大的那个量级，留一倍余量。
SIMPLIFY_MAX_POINTS = 250


def _rdp_keep(pts: np.ndarray, tol: float) -> np.ndarray:
    """Douglas–Peucker 的保留掩膜（首末点必留）。

    闭合折线（首末点重合）也走这条路径：首末弦退化成点，于是先挑"离起点
    最远的顶点"把环劈成两段再各自简化——起点因此必留，闭合性不变。
    """
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg = pts[b] - pts[a]
        length = float(np.hypot(seg[0], seg[1]))
        d = pts[a + 1:b] - pts[a]
        dist = (np.hypot(d[:, 0], d[:, 1]) if length < 1e-12
                else np.abs(seg[0] * d[:, 1] - seg[1] * d[:, 0]) / length)
        k = int(np.argmax(dist))
        if dist[k] > tol:
            i = a + 1 + k
            keep[i] = True
            stack.extend([(a, i), (i, b)])
    return keep


def simplify_polygon(points, tol_mm, max_points=SIMPLIFY_MAX_POINTS):
    """把网格描线简化成"真实顶点"多边形：偏离相邻弦 ≤ tol_mm 的顶点全删掉。

    **为什么必须简化**（2026-10-09 功分器 Stage 1 实测）：零等值线来自
    marching squares，每个格子发射 1~2 个顶点；边界是斜的时候（功分器两臂
    斜 27.9°），斜边擦过某个格点时这一两个交点会挤成几微米的小段——Y 形
    初始轮廓 854 点里 56 段短于 20 µm、最短 4.4 µm。这份折线在 Python 侧
    **无自交**（``self_intersections`` 逐对检查通过，容差放到 1e-3 也没有
    重合/接触），但 CST 的 ``Extrude ... .Create`` 只回一句
    "Profile is self-intersecting"。对照三种真实命令：249 点 / 6.9 KB 可用、
    497 点 / 13.7 KB 可用、854 点 / 23.6 KB 被拒——**点表容量**是与
    "能用 / 不能用"完全吻合的那个变量（微段不是：旧耦合器带着 2 µm 的微段
    实跑了 6 轮都没事）。超限后 CST 截断点表、再自动闭合出的斜切段，才是
    它眼里的自交。所以**送进 CST 的轮廓不该是网格描线，而是简化后的多边形**：
    初始 Y 形 854 → 18 点、命令 23.6 KB → 不到 1 KB。

    tol_mm：必给（没有"自动"档，理由见 :data:`SIMPLIFY_TOL_CELLS`）。取
    :data:`SIMPLIFY_TOL_CELLS` × 网格步长，即 0.2 格；≤0 = 原样返回
    （调试/回归对比用）。
    max_points：简化后仍超预算（形状过碎）就把容差翻倍重试，最多 8 轮——
    宁可多丢一点远小于一格的细节，也不让 CST 跑到一半才炸。
    None = 不设上限。

    闭合折线（首末点相同）与开放折线同一条路径；首末点必留，故输入闭合
    则输出闭合。返回新数组，不动调用方的数据。
    """
    pts = np.asarray(points, dtype=float)
    if len(pts) < 4:
        return pts.copy()
    tol = float(tol_mm)
    if tol <= 0.0:
        return pts.copy()
    keep = _rdp_keep(pts, tol)
    if max_points is not None:
        for _ in range(8):
            if int(keep.sum()) <= max_points:
                break
            tol *= 2.0
            keep = _rdp_keep(pts, tol)
    return pts[keep]


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
    contours: list[np.ndarray], box, pad_mm: float = 0.05,
    simplify_tol_mm: float = 0.0,
    max_points: int | None = SIMPLIFY_MAX_POINTS,
) -> list[np.ndarray]:
    """把穿出设计区边界的开放轮廓闭合为多边形（供求解器几何重建）。

    **按端点配对**：开放轮廓与设计区边界的每个交点都是边界上的一个端点；
    沿（外扩 pad 后的）边界周长排序后，**相邻的两个端点之间就是这个开口的
    封口段**，于是"沿边直线段 → 下一条轮廓 → 再一段"串一圈即得闭合多边形。
    这条规则对任意条数都成立，也不关心提取器给的走向（走向由配对决定）：

      - 一条线穿出两条边（耦合臂）：4 个端点、2 个开口 → 两条竖线封口；
      - Y 形功分器（输入 + 两臂）：6 个端点、**3 个开口**（旧的"必须偶数"
        在这里直接抛错）→ 三条封口段、一个多边形；
      - 只从一个开口伸出的短截线：一条轮廓的两个端点在同一个开口上 →
        自配成一段封口；
      - 两条互不相关的轮廓被错配 → 封口段共线重叠，由下面的自交检查拦下。

    闭合轮廓原样保留。pad_mm：端点沿所在边界的外法向向外延伸的余量
    （与设计区外的固定馈线重叠，同材料在求解器中自动合并）。

    simplify_tol_mm：返回前按 :func:`simplify_polygon` 简化用的容差。
    **默认 0 = 不简化**（本函数只管"闭合"这一件事，闭合的正确性与点的疏密
    无关，回归对比也要看原样输出）；送 CST 的调用方必须显式传
    :data:`SIMPLIFY_TOL_CELLS` × 网格步长——CST 的 Extrude 点表吃得下 497
    点、吃不下 854 点（实测），而网格描线在斜边界上每格发射 1~2 点，Y 形
    功分器一开始就 854 点。这一步是**送进 CST 的必要条件**，不是可选的洁癖。

    返回的多边形保证**无自交**（有自交时抛 ValueError 并指出相交的边）：
    CST 的 `Extrude ... .Create` 对自交轮廓只报 "Profile is self-intersecting"，
    在这里拦下能给出可定位的诊断。简化前后各查一次——简化若把两条只差
    不到两倍容差的边并到一起，会单独报"简化后自交"。
    """
    opens = [c for c in contours if not np.allclose(c[0], c[-1])]
    closed = [c for c in contours if np.allclose(c[0], c[-1])]
    out = list(closed)
    if opens:
        corners = _padded_corners(box, pad_mm)
        # 端点先沿所在边外法向延伸 pad（原始 box 边界 → 外扩周长）
        opens = [_extend_open_endpoints(c, box, pad_mm) for c in opens]
        partner = _pair_endpoints(opens, corners)
        # 链的起点取"首端点沿周长最靠前"的那条：只是为了让输出的起点/绕向
        # 稳定（同一份轮廓每次得到同一个多边形），配对与它无关。
        order = sorted(range(len(opens)),
                       key=lambda i: _perimeter_param(opens[i][0], corners))
        done: set[int] = set()
        for i0 in order:
            if i0 not in done:
                out.append(_chain_contour(opens, i0, partner, corners, done))
    for k, poly in enumerate(out):
        bad = self_intersections(poly)
        if bad:
            i, j = bad[0]
            raise ValueError(
                f"闭合后的第 {k} 个多边形自交：边 {i} {_fmt(poly[i])}→{_fmt(poly[i + 1])} "
                f"与边 {j} {_fmt(poly[j])}→{_fmt(poly[j + 1])} 相交"
                f"（共 {len(bad)} 处）。检查零等值面轮廓的端点是否两两成对"
                "（每条开放轮廓的两个端点都该落在设计区边界上）。"
            )
    _check_no_nested(out)
    if simplify_tol_mm <= 0.0:
        return out
    simple = [simplify_polygon(p, simplify_tol_mm, max_points) for p in out]
    for k, poly in enumerate(simple):
        bad = self_intersections(poly)
        if bad:
            i, j = bad[0]
            raise ValueError(
                f"第 {k} 个多边形**简化后**自交：边 {i} {_fmt(poly[i])}→"
                f"{_fmt(poly[i + 1])} 与边 {j} {_fmt(poly[j])}→{_fmt(poly[j + 1])} "
                "相交。原始折线本来无自交，是简化把两条只差不到两倍容差的边"
                "并到了一起——把 close_open_contours 的 simplify_tol_mm 调小，"
                "或检查 φ 是否长出了亚网格细缝。"
            )
    return simple


def _check_no_nested(polys: list[np.ndarray]) -> None:
    """多边形不得互相嵌套（= 金属里有个空气洞），有则报错。

    洞的零等值线本身也是一条**闭合**轮廓，于是 ``out`` 里会同时有它的外圈
    和洞：求解器把每条轮廓各挤出一块金属，洞被当成"洞里悬着的一块金属岛"
    （金属里还是实心）；而伴随法算的仍是"有洞"的几何。两边描述的不是同一个
    器件，且都不报错——静默失效里最贵的一类。

    真正带洞的几何需要"外圈 + 布尔减"才能交给 CST，不在本项目的实现范围内，
    所以这里直接炸（时间点上也是对的：洞一旦出现，多半是优化在把结构掐断，
    该看连通性约束/步长，而不是让洞悄悄变成岛）。两块互不相干的金属（优化
    把结构切开了、或有意的多块设计金属）是合法的，它们互不嵌套，不受影响。
    """
    from matplotlib.path import Path

    for i, inner in enumerate(polys):
        for j, outer in enumerate(polys):
            if i == j:
                continue
            # 无自交的两条闭合折线：inner 的顶点**全**落在 outer 内 ⟺ inner
            # 被 outer 包住（此时 outer 的顶点必然在 inner 之外，互为内外）。
            if np.all(Path(np.asarray(outer, dtype=float)).contains_points(inner)):
                raise ValueError(
                    f"第 {i} 个闭合多边形整个落在第 {j} 个里面：设计金属里出现了"
                    "空气洞。CST 会把这条轮廓单独挤成一块悬空的金属（洞变实心），"
                    "与 φ 描述的几何不一致且不报错。请检查优化是否在掐断结构"
                    "（连通性约束 / optimizer.step_cells），或先实现带洞轮廓的"
                    "布尔减再放开。"
                )


def _pair_endpoints(
    opens: list[np.ndarray], corners
) -> dict[tuple[int, int], tuple[int, int]]:
    """沿外扩周长把开放轮廓的端点两两配对，返回 {(轮廓号, 端号): (轮廓号, 端号)}。

    端号 0 = 该轮廓的首点，1 = 末点。沿周长相邻的两个端点属于同一个开口
    （一个开口 = 金属穿过边界的一段截面，它的两端就是轮廓与边界的两个交点），
    所以排序后相邻即配对。设计区外扩 pad 后的角点落在金属之外，没有开口会
    跨过周长起点，故不需要考虑首尾环绕。
    """
    ends: list[tuple[float, int, int]] = []
    for i, c in enumerate(opens):
        ends.append((_perimeter_param(c[0], corners), i, 0))
        ends.append((_perimeter_param(c[-1], corners), i, 1))
    ends.sort(key=lambda e: e[0])
    partner: dict[tuple[int, int], tuple[int, int]] = {}
    for j in range(0, len(ends), 2):
        _, i0, k0 = ends[j]
        _, i1, k1 = ends[j + 1]
        partner[(i0, k0)] = (i1, k1)
        partner[(i1, k1)] = (i0, k0)
    return partner


def _chain_contour(
    opens: list[np.ndarray], i0: int,
    partner: dict[tuple[int, int], tuple[int, int]], corners, done: set[int],
) -> np.ndarray:
    """从轮廓 i0 的**首点**出发绕一圈：轮廓 → 封口段 → 下一条轮廓 → …

    每条轮廓按"从进入它的那个端点走向另一端"的方向遍历（由配对决定），
    因此不需要像旧实现那样猜两条轮廓的走向是否一致。封口段是沿外扩周长的
    折线（同一条边上直接连；跨边时绕过角点），不含两端点，避免与相邻轮廓
    的首末点重复。
    """
    pts: list = []
    i, k = i0, 0
    while True:
        c = opens[i] if k == 0 else opens[i][::-1]
        pts.extend(c)
        done.add(i)
        j, k2 = partner[(i, 1 - k)]
        pts.extend(_perimeter_walk(
            c[-1], opens[j][0] if k2 == 0 else opens[j][-1], corners))
        if j == i0 and k2 == 0:
            break
        i, k = j, k2
        if len(done) > len(opens):          # 配对是双射 → 不会走到这里
            raise RuntimeError("开放轮廓端点配对不成环（内部错误）")
    poly = np.asarray(pts, dtype=float)
    if not np.allclose(poly[0], poly[-1]):
        poly = np.vstack([poly, poly[0]])
    return poly


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
