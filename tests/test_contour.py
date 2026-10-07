"""轮廓提取与平滑测试。"""

import numpy as np
import pytest

from eaopt.config import BoxSpec
from eaopt.geometry import contour as C
from eaopt.geometry.contour import (close_open_contours, extract_contours,
                                    self_intersections, smooth_resample)
from eaopt.geometry.levelset import LevelSet2D

RECT = np.array([[2.0, -1.0], [10.0, -1.0], [10.0, 1.0], [2.0, 1.0]])


def make_ls(dx: float = 0.1) -> LevelSet2D:
    return LevelSet2D(BoxSpec(x=(0.0, 12.0), y=(-2.0, 3.0)), dx)


def circle_polygon(r: float, cx: float, cy: float, n: int = 64) -> np.ndarray:
    th = np.linspace(0.0, 2.0 * np.pi, n)
    return np.column_stack([cx + r * np.cos(th), cy + r * np.sin(th)])


def _strip(p0, p1, half_w: float) -> np.ndarray:
    """从 p0 到 p1 的等宽条带（半宽 half_w），用来拼 Y 形/多段金属。"""
    p0, p1 = np.asarray(p0, dtype=float), np.asarray(p1, dtype=float)
    u = (p1 - p0) / np.linalg.norm(p1 - p0)
    n = np.array([-u[1], u[0]])
    return np.array([p0 + n * half_w, p1 + n * half_w,
                     p1 - n * half_w, p0 - n * half_w])


def _area(poly: np.ndarray) -> float:
    """闭合多边形的面积（鞋带公式）。"""
    x, y = poly[:, 0], poly[:, 1]
    return float(abs(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1])) / 2.0)


def test_extract_contours_rectangle_vertices_on_edges():
    ls = make_ls()
    ls.init_from_polygons([RECT])
    paths = extract_contours(ls.xs, ls.ys, ls.phi)
    assert len(paths) == 1
    v = paths[0]
    assert np.allclose(v[0], v[-1])  # 闭合
    # 线性插值的零点必须精确落在矩形边上（x∈{2,10} 或 y∈{-1,1}）
    on_edge = (np.abs(np.abs(v[:, 0] - 6.0) - 4.0) < 1e-9) | (
        np.abs(np.abs(v[:, 1]) - 1.0) < 1e-9
    )
    assert on_edge.all()


def test_extract_contours_returns_open_paths_for_crossing_polygon():
    # 金属条贯穿设计区左右边界（如耦合器馈线），轮廓应为开放路径
    ls = make_ls()
    strip = np.array([[-1.0, -1.0], [13.0, -1.0], [13.0, 0.5], [-1.0, 0.5]])
    ls.init_from_polygons([strip])
    paths = extract_contours(ls.xs, ls.ys, ls.phi)
    assert len(paths) == 2
    assert all(not np.allclose(p[0], p[-1]) for p in paths)


def test_smooth_resample_circle_uniform_spacing():
    r, spacing = 2.0, 0.2
    ls = make_ls(dx=0.05)
    ls.init_from_polygons([circle_polygon(r, 6.0, 0.5)])
    raw = extract_contours(ls.xs, ls.ys, ls.phi)[0]
    out = smooth_resample(raw, spacing)
    # 闭合且首尾一致
    assert np.allclose(out[0], out[-1])
    # 点数 ≈ 周长/点距
    assert abs(len(out) - 1 - round(2.0 * np.pi * r / spacing)) <= 1
    # 相邻点距均匀
    seg = np.linalg.norm(np.diff(out, axis=0), axis=1)
    assert np.abs(seg - spacing).max() < 0.02
    # 仍接近圆：半径偏差小于一个网格
    rad = np.linalg.norm(out - np.array([6.0, 0.5]), axis=1)
    assert np.abs(rad - r).max() < 0.1


def test_dedupe_vertices_removes_zero_length_segments():
    """marching 过节点时会连续发射同一个交点（零长段）。"""
    line = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    out = C.dedupe_vertices(line)
    assert np.allclose(out, [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    # 开放折线：首尾不相等就不当闭合处理，也不改动非相邻点
    sparse = np.array([[0.0, 0.0], [0.5, 0.0], [2.0, 0.0]])
    assert np.array_equal(C.dedupe_vertices(sparse), sparse)


def test_dedupe_vertices_forces_exact_closure():
    """闭合轮廓：末点重复删掉，首尾强制**精确**相等（下游靠它判闭合）。"""
    ring = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 1.0],
                     [0.0, 1.0], [0.0, 0.0], [0.0, 0.0]])
    out = C.dedupe_vertices(ring)
    assert len(out) == 5
    assert np.array_equal(out[0], out[-1])
    assert np.allclose(out[:4], [[0, 0], [1, 0], [1, 1], [0, 1]])


def test_dedupe_vertices_handles_near_closure():
    """首尾只差浮点残差（1e-9）也算闭合——matplotlib 的闭合点不总是逐位相等。"""
    ring = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0],
                     [1e-9, -1e-9]])
    out = C.dedupe_vertices(ring)
    assert np.array_equal(out[0], out[-1])


def _arm_box() -> BoxSpec:
    return BoxSpec(x=(0.0, 12.0), y=(-2.0, 3.0))


# 耦合臂横段的上/下边（开放轮廓，穿出设计区左右边界）
ARM_TOP = np.array([[0.0, 0.0], [4.0, 0.0], [8.0, 0.0], [12.0, 0.0]])
ARM_BOT = np.array([[0.0, -1.6], [4.0, -1.6], [8.0, -1.6], [12.0, -1.6]])


def test_close_open_contours_arm_strip():
    """同向的两条开放边：闭合段必须是左右两条竖线（x = ∓0.05）。

    输出从"首点沿外扩周长更靠前"的那条边起走（这里是上边，逆时针自左下角
    数 y=0 在 y=-1.6 之前），绕向因此是顺时针——CST 的 Extrude 不看绕向。
    """
    polys = close_open_contours([ARM_TOP, ARM_BOT], _arm_box())
    assert len(polys) == 1
    poly = polys[0]
    assert np.allclose(poly[0], poly[-1])  # 闭合
    want = np.array([
        [-0.05, 0.0], [4.0, 0.0], [8.0, 0.0], [12.05, 0.0],
        [12.05, -1.6], [8.0, -1.6], [4.0, -1.6], [-0.05, -1.6], [-0.05, 0.0],
    ])
    assert np.allclose(poly, want)
    assert self_intersections(poly) == []
    # 顶点均在设计区外扩 0.05 的包络内，内部点判在条带内
    assert poly[:, 0].min() >= -0.05 - 1e-9
    assert poly[:, 0].max() <= 12.05 + 1e-9
    assert poly[:, 1].min() >= -2.05 - 1e-9
    assert poly[:, 1].max() <= 3.05 + 1e-9
    from matplotlib.path import Path as MplPath

    p = MplPath(poly)
    assert p.contains_point((6.0, -0.8))  # 条带内
    assert not p.contains_point((6.0, 1.0))  # 条带上方（设计区内的空气）


def test_close_open_contours_arm_strip_bottom_reversed():
    """服务器实测的失败输入：下边是右→左（提取器不保证走向）。

    修前这里把 a 的尾接到 b 的首、闭合路径从条带内部斜穿过去，CST 报
    `(&H8000ffff) Profile is self-intersecting, please check (.Create)`。
    修后闭合段仍是两条竖线。
    """
    bot_rev = ARM_BOT[::-1].copy()  # 右→左
    polys = close_open_contours([ARM_TOP, bot_rev], _arm_box())
    poly = polys[0]
    want = np.array([
        [12.05, -1.6], [8.0, -1.6], [4.0, -1.6], [-0.05, -1.6],
        [-0.05, 0.0], [4.0, 0.0], [8.0, 0.0], [12.05, 0.0], [12.05, -1.6],
    ])
    assert np.allclose(poly, want)
    assert self_intersections(poly) == []


def test_perimeter_walk_takes_the_short_way_and_keeps_the_corner():
    """闭合路径沿外扩周长走较短一侧，且必须含终点所在边的角点。

    不含角点会让最后一段从上一个角斜切到终点（自交）；旧的实现碰到
    "起点、终点在相邻边上"还会死循环。
    """
    corners = C._padded_corners(_arm_box(), 0.05)
    # 同一条边（左边 → 左边）：直接连，没有中间点
    walk = C._perimeter_walk(np.array([-0.05, -1.6]), np.array([-0.05, 1.0]), corners)
    assert len(walk) == 0
    # 相邻边（左边 → 下边）：绕左下角，且含角点 (-0.05, -2.05)
    walk = C._perimeter_walk(np.array([-0.05, 1.0]), np.array([3.0, -2.05]), corners)
    assert np.allclose(walk, [[-0.05, -2.05]])
    # 相对边（左边 → 右边）：走下边（较短），含下边两个角
    walk = C._perimeter_walk(np.array([-0.05, 1.0]), np.array([12.05, 2.0]), corners)
    assert np.allclose(walk, [[-0.05, -2.05], [12.05, -2.05]])


def test_self_intersections_flags_a_bowtie_and_allows_a_touch():
    square = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 2.0], [0.0, 2.0], [0.0, 0.0]])
    assert self_intersections(square) == []
    bowtie = np.array([[0.0, 0.0], [4.0, 2.0], [4.0, 0.0], [0.0, 2.0], [0.0, 0.0]])
    assert self_intersections(bowtie) == [(0, 2)]
    # 顶点擦过非相邻边（数值退化时常见）不算自交——只有真正穿过或共线重叠才报
    touch = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 2.0], [2.0, 0.0], [0.0, 2.0],
                      [0.0, 0.0]])
    assert self_intersections(touch) == []
    # 共线重叠要报（非相邻的两条边实打实地叠在一起）
    slit = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [2.0, 3.0], [2.0, 0.0],
                     [3.0, 0.0], [3.0, 3.0], [0.0, 3.0], [0.0, 0.0]])
    assert self_intersections(slit) != []


def test_close_open_contours_rejects_a_mismatched_pair():
    """两条互不相关的开放轮廓凑成一对 → 闭合边共线重叠，应当场拦下。

    CST 对自交轮廓只回一句 "Profile is self-intersecting"，这里抛出的
    ValueError 会指出是哪两条边、交在哪。
    """
    tooth = np.array([[10.0, -2.0], [10.5, -1.0], [11.0, -2.0]])  # 下边的小齿
    slant = np.array([[0.0, 0.0], [2.0, -2.0]])  # 左边到下边的斜线
    with pytest.raises(ValueError, match="自交"):
        close_open_contours([tooth, slant], _arm_box())


def test_close_open_contours_keeps_closed_and_closes_a_single_open():
    """闭合轮廓原样保留；**单条开放轮廓**（金属只从一个开口伸出）也能闭合。

    "开放轮廓数必须是偶数"是旧实现的限制（两两配对）。

    金属只从左边伸出（x 从 0 到 6 的短截线）：轮廓是一条开放的"C"形，
    两个端点都在左边的同一个开口上 → 自配对成一段封口。
    """
    box = BoxSpec(x=(0.0, 12.0), y=(-2.0, 3.0))
    circle = circle_polygon(2.0, 6.0, 0.5)
    stub = np.array([[-1.0, -0.5], [6.0, -0.5], [6.0, 0.5], [-1.0, 0.5]])
    ls = LevelSet2D(box, 0.05)
    ls.init_from_polygons([stub])
    arcs = [p for p in extract_contours(ls.xs, ls.ys, ls.phi)
            if not np.allclose(p[0], p[-1])]
    assert len(arcs) == 1                       # 只有一个开口
    out = close_open_contours([circle] + arcs, box)
    assert len(out) == 2
    poly = out[-1]
    assert np.allclose(poly[0], poly[-1])
    assert self_intersections(poly) == []
    assert _area(poly) == pytest.approx(6.05 * 1.0, rel=0.02)   # 含 pad 0.05
    from matplotlib.path import Path as MplPath

    p = MplPath(poly)
    assert p.contains_point((3.0, 0.0))
    assert not p.contains_point((8.0, 0.0))


def test_close_open_contours_y_shape_three_openings():
    """Y 形（输入 + 两条臂 = **3 个开口**，奇数）：闭合后是一个无自交多边形。

    Y 形功分器的可动金属：输入线从左边穿入、两条臂从右边穿出。6 个端点
    沿周长排序后两两相邻 → 3 条封口段 → 一个多边形（旧实现在这里抛
    "开放轮廓数 3 不是偶数"）。
    """
    box = BoxSpec(x=(0.0, 12.0), y=(-4.0, 4.0))
    ls = LevelSet2D(box, 0.05)
    ls.init_from_polygons([_strip((-1.0, 0.0), (6.0, 0.0), 0.5),
                           _strip((6.0, 0.0), (13.0, 2.0), 0.5),
                           _strip((6.0, 0.0), (13.0, -2.0), 0.5)])
    arcs = [p for p in extract_contours(ls.xs, ls.ys, ls.phi)
            if not np.allclose(p[0], p[-1])]
    assert len(arcs) == 3                       # 3 个开口 = 3 条开放轮廓
    polys = close_open_contours(arcs, box)
    assert len(polys) == 1                      # 一条连通的金属 = 一个多边形
    poly = polys[0]
    assert np.allclose(poly[0], poly[-1])
    assert self_intersections(poly) == []
    from matplotlib.path import Path as MplPath

    p = MplPath(poly)
    assert p.contains_point((3.0, 0.0))         # 输入线
    assert p.contains_point((11.5, 1.57))       # 上臂（臂上 x=11.5 处的中心线）
    assert p.contains_point((11.5, -1.57))      # 下臂
    assert not p.contains_point((8.5, 0.0))     # 两臂之间的开口（空气）
    # 顶点不越出外扩 0.05 的包络
    assert poly[:, 0].min() >= -0.05 - 1e-9
    assert poly[:, 0].max() <= 12.05 + 1e-9
    assert poly[:, 1].min() >= -4.05 - 1e-9
    assert poly[:, 1].max() <= 4.05 + 1e-9


def test_close_open_contours_two_strips_side_by_side():
    """两条互不相连的金属条各穿出左右边界：4 个开口 → 2 个多边形。"""
    box = BoxSpec(x=(0.0, 12.0), y=(-4.0, 4.0))
    ls = LevelSet2D(box, 0.05)
    ls.init_from_polygons([_strip((-1.0, -2.0), (13.0, -2.0), 0.5),
                           _strip((-1.0, 2.0), (13.0, 2.0), 0.5)])
    arcs = [p for p in extract_contours(ls.xs, ls.ys, ls.phi)
            if not np.allclose(p[0], p[-1])]
    assert len(arcs) == 4
    polys = close_open_contours(arcs, box)
    assert len(polys) == 2
    from matplotlib.path import Path as MplPath

    boxes = sorted((MplPath(p).get_extents().y0 for p in polys))
    assert boxes[0] < -1.0 < 1.0 < boxes[1]     # 一条在上、一条在下
    for poly in polys:
        assert self_intersections(poly) == []
        assert _area(poly) == pytest.approx(12.1 * 1.0, rel=0.02)


def test_close_open_contours_rejects_an_air_hole():
    """金属里围出空气洞 → 报错（洞的轮廓会被 CST 挤成一块悬空金属岛）。

    仿真把每条轮廓各挤出一块金属："外圈 + 洞"会变成"外圈 + 洞里一块实心
    岛屿"，而伴随法算的是有洞的几何——两边不是同一个器件，且都不报错。
    带洞轮廓需要布尔减才能交给 CST，所以这里直接炸。
    """
    ring = _strip((-1.0, 0.0), (13.0, 0.0), 1.5)          # 外圈：穿出左右边界
    ls = LevelSet2D(BoxSpec(x=(0.0, 12.0), y=(-4.0, 4.0)), 0.05)
    ls.init_from_polygons([ring])
    # 抠掉中间一块 → 环状金属（空气洞的零等值线是一条闭合轮廓）
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    ls.phi[(X > 5.0) & (X < 7.0) & (np.abs(Y) < 0.5)] = 1.0
    contours = list(extract_contours(ls.xs, ls.ys, ls.phi))
    assert len(contours) == 3                                     # 外圈 2 条开放 + 洞 1 条闭合
    assert sum(np.allclose(c[0], c[-1]) for c in contours) == 1

    with pytest.raises(ValueError, match="空气洞"):
        close_open_contours(contours, ls.box)


def test_close_open_contours_allows_two_disjoint_pieces():
    """两块互不相干的金属（优化把结构切开）是合法的，不许被洞穴检查误伤。"""
    box = BoxSpec(x=(0.0, 12.0), y=(-4.0, 4.0))
    ls = LevelSet2D(box, 0.05)
    ls.init_from_polygons([circle_polygon(1.5, 3.0, 0.0),
                           circle_polygon(1.5, 9.0, 0.0)])
    closed = [p for p in extract_contours(ls.xs, ls.ys, ls.phi) if np.allclose(p[0], p[-1])]
    assert len(closed) == 2
    assert len(close_open_contours(closed, box)) == 2


def test_smooth_resample_open_line_keeps_endpoints():
    ls = make_ls()
    strip = np.array([[-1.0, -1.0], [13.0, -1.0], [13.0, 0.5], [-1.0, 0.5]])
    ls.init_from_polygons([strip])
    open_path = [p for p in extract_contours(ls.xs, ls.ys, ls.phi)
                 if not np.allclose(p[0], p[-1])][0]
    out = smooth_resample(open_path, 0.3, closed=False)
    # 开放样条两端点应保持在设计区边界上（x≈0 或 x≈12）
    for pt in (out[0], out[-1]):
        on_boundary = min(abs(pt[0] - 0.0), abs(pt[0] - 12.0)) < 0.05
        assert on_boundary
