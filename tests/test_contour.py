"""轮廓提取与平滑测试。"""

import numpy as np
import pytest

from eaopt.config import BoxSpec
from eaopt.geometry.contour import (close_open_contours, extract_contours,
                                    smooth_resample)
from eaopt.geometry.levelset import LevelSet2D

RECT = np.array([[2.0, -1.0], [10.0, -1.0], [10.0, 1.0], [2.0, 1.0]])


def make_ls(dx: float = 0.1) -> LevelSet2D:
    return LevelSet2D(BoxSpec(x=(0.0, 12.0), y=(-2.0, 3.0)), dx)


def circle_polygon(r: float, cx: float, cy: float, n: int = 64) -> np.ndarray:
    th = np.linspace(0.0, 2.0 * np.pi, n)
    return np.column_stack([cx + r * np.cos(th), cy + r * np.sin(th)])


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


def test_close_open_contours_arm_strip():
    # 耦合臂上下两条开放边（穿出设计区左右边界）
    top = np.array([[0.0, 0.0], [4.0, 0.0], [8.0, 0.0], [12.0, 0.0]])
    bot = np.array([[0.0, -1.6], [4.0, -1.6], [8.0, -1.6], [12.0, -1.6]])
    box = BoxSpec(x=(0.0, 12.0), y=(-2.0, 3.0))
    polys = close_open_contours([top, bot], box)
    assert len(polys) == 1
    poly = polys[0]
    assert np.allclose(poly[0], poly[-1])  # 闭合
    # 顶点均在设计区外扩 0.05 的包络内
    assert poly[:, 0].min() >= -0.05 - 1e-9
    assert poly[:, 0].max() <= 12.05 + 1e-9
    assert poly[:, 1].min() >= -2.05 - 1e-9
    assert poly[:, 1].max() <= 3.05 + 1e-9
    # 封闭区域包含臂（面积 > 臂面积 19.2）
    from matplotlib.path import Path as MplPath

    assert MplPath(poly).contains_point((6.0, -0.8))


def test_close_open_contours_keeps_closed_and_rejects_odd():
    box = BoxSpec(x=(0.0, 12.0), y=(-2.0, 3.0))
    circle = circle_polygon(2.0, 6.0, 0.5)
    top = np.array([[0.0, 0.0], [12.0, 0.0]])
    out = close_open_contours([circle, top, top], box)  # 2 开 + 1 闭
    assert len(out) == 2
    assert any(np.allclose(c[0], c[-1]) for c in out)
    with pytest.raises(ValueError):
        close_open_contours([top], box)  # 奇数个开放轮廓


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
