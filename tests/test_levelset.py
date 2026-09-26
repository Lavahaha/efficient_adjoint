"""水准集数值测试：SDF 初始化、HJ 演化、重初始化、速度延拓。

参照物均为有解析解的简单几何（矩形），可精确验证。
"""

import numpy as np
import pytest

from eaopt.config import BoxSpec
from eaopt.geometry.contour import extract_contours
from eaopt.geometry.levelset import LevelSet2D

# 矩形金属 [2,10] x [-1,1]（世界坐标 mm）
RECT = np.array([[2.0, -1.0], [10.0, -1.0], [10.0, 1.0], [2.0, 1.0]])


def make_ls(dx: float = 0.1) -> LevelSet2D:
    return LevelSet2D(BoxSpec(x=(0.0, 12.0), y=(-2.0, 3.0)), dx)


def rect_sdf(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """矩形 [2,10]x[-1,1] 的解析带符号距离（金属内为负）。"""
    d_out_x = np.maximum.reduce([2.0 - x, x - 10.0, np.zeros_like(x)])
    d_out_y = np.maximum.reduce([-1.0 - y, y - 1.0, np.zeros_like(y)])
    d_out = np.hypot(d_out_x, d_out_y)
    d_in = np.minimum.reduce([x - 2.0, 10.0 - x, y + 1.0, 1.0 - y])
    inside = (x >= 2.0) & (x <= 10.0) & (y >= -1.0) & (y <= 1.0)
    return np.where(inside, -d_in, d_out)


def test_init_sdf_matches_analytic_rectangle():
    ls = make_ls()
    ls.init_from_polygons([RECT])
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    assert np.abs(ls.phi - rect_sdf(X, Y)).max() < 1e-10


def test_update_expands_metal_with_positive_velocity():
    ls = make_ls(dx=0.05)
    ls.init_from_polygons([RECT])
    v = np.full_like(ls.phi, 1.0)  # V>0 -> 金属扩张（沿外法向移动）
    t = 0.5  # 各边期望外扩 0.5 mm
    steps = int(round(t / (0.4 * ls.dx)))
    ls.update(v, steps=steps, cfl=0.4)
    paths = extract_contours(ls.xs, ls.ys, ls.phi)
    assert len(paths) == 1
    vtx = paths[0]
    tol = 2.0 * ls.dx  # 一阶上风 + 角点圆化容差
    assert vtx[:, 0].min() == pytest.approx(2.0 - t, abs=tol)
    assert vtx[:, 0].max() == pytest.approx(10.0 + t, abs=tol)
    assert vtx[:, 1].min() == pytest.approx(-1.0 - t, abs=tol)
    assert vtx[:, 1].max() == pytest.approx(1.0 + t, abs=tol)


def test_update_contracts_metal_with_negative_velocity():
    ls = make_ls(dx=0.05)
    ls.init_from_polygons([RECT])
    v = np.full_like(ls.phi, -1.0)  # V<0 -> 金属收缩
    t = 0.5
    steps = int(round(t / (0.4 * ls.dx)))
    ls.update(v, steps=steps, cfl=0.4)
    paths = extract_contours(ls.xs, ls.ys, ls.phi)
    assert len(paths) == 1
    vtx = paths[0]
    tol = 2.0 * ls.dx
    assert vtx[:, 0].min() == pytest.approx(2.0 + t, abs=tol)
    assert vtx[:, 0].max() == pytest.approx(10.0 - t, abs=tol)
    assert vtx[:, 1].min() == pytest.approx(-1.0 + t, abs=tol)
    assert vtx[:, 1].max() == pytest.approx(1.0 - t, abs=tol)


def test_reinitialize_restores_sdf():
    """矩形：远离尖角处 |∇φ| ≈ 1。

    注：PDE 重初始化在凸角附近收敛慢（粘度解固有特性，~O(1/τ)），
    角点影响半径约 1 mm（1.0 mm 排除后实测偏差 0.037）。
    """
    ls = make_ls()
    ls.init_from_polygons([RECT])
    ls.phi *= 0.3  # 破坏距离函数性质（|∇φ|=0.3）
    ls.reinitialize(iters=300)
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    near_corner_x = (np.abs(X - 2.0) < 1.0) | (np.abs(X - 10.0) < 1.0)
    near_corner_y = (np.abs(Y - 1.0) < 1.0) | (np.abs(Y + 1.0) < 1.0)
    mask = (np.abs(ls.phi) < 0.8) & ~(near_corner_x & near_corner_y)
    assert mask.sum() > 100
    gx, gy = np.gradient(ls.phi, ls.dx)
    gnorm = np.hypot(gx, gy)
    assert np.abs(gnorm[mask] - 1.0).max() < 0.08


def test_reinitialize_smooth_circle():
    """光滑几何（圆）无尖角，带内 |∇φ| 应整体收敛到 ≈ 1。"""
    r, cx, cy = 2.0, 6.0, 0.5
    th = np.linspace(0.0, 2.0 * np.pi, 64)
    circle = np.column_stack([cx + r * np.cos(th), cy + r * np.sin(th)])
    ls = make_ls()
    ls.init_from_polygons([circle])
    ls.phi *= 0.3
    ls.reinitialize(iters=300)
    gx, gy = np.gradient(ls.phi, ls.dx)
    gnorm = np.hypot(gx, gy)
    mask = np.abs(ls.phi) < 0.8
    assert np.abs(gnorm[mask] - 1.0).max() < 0.06


def test_normals_at_rectangle():
    ls = make_ls()
    ls.init_from_polygons([RECT])
    # 上边外侧中点：法向应 (0,1)
    n = ls.normals_at(np.array([[6.0, 1.3]]))
    assert np.allclose(n, [[0.0, 1.0]], atol=0.05)
    # 右边外侧中点：法向应 (1,0)
    n = ls.normals_at(np.array([[10.3, 0.0]]))
    assert np.allclose(n, [[1.0, 0.0]], atol=0.05)


def test_extend_velocity_fills_band():
    ls = make_ls()
    ls.init_from_polygons([RECT])
    v_known = np.where(np.abs(ls.phi) <= 0.5 * ls.dx, 3.0, 0.0)
    v = ls.extend_velocity(v_known, band=1.0, iters=150)
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    # 远离四角处，窄带内延拓值应等于边界已知值
    near_corner_x = (np.abs(X - 2.0) < 0.7) | (np.abs(X - 10.0) < 0.7)
    near_corner_y = (np.abs(Y - 1.0) < 0.7) | (np.abs(Y + 1.0) < 0.7)
    mask = (np.abs(ls.phi) <= 0.8) & ~(near_corner_x & near_corner_y)
    assert mask.sum() > 100
    assert np.abs(v[mask] - 3.0).max() < 0.3
    # 窄带外应为 0
    assert np.abs(v[np.abs(ls.phi) > 1.0 + 1e-9]).max() == 0.0
