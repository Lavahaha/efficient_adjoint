"""FieldGrid 插值（三线性 + WLS）与法/切分解测试（合成场、手算参考）。"""

import numpy as np
import pytest

from eaopt.adjoint.fields import (FieldGrid, build_wls_stencil,
                                  decompose_normal_tangential)


def make_linear_grid() -> FieldGrid:
    """线性场 f(x,y,z) = (x, 2y+1, 3z−2)，三线性插值应精确还原。"""
    x = np.arange(0.0, 2.0, 0.5)
    y = np.arange(0.0, 1.5, 0.5)
    z = np.array([0.0, 0.5])
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")
    data = np.stack([X, 2 * Y + 1, 3 * Z - 2], axis=-1).astype(complex)
    return FieldGrid(origin=(0.0, 0.0, 0.0), spacing=(0.5, 0.5, 0.5), data=data)


def test_trilinear_interp_exact_for_linear_field():
    fg = make_linear_grid()
    rng = np.random.default_rng(0)
    pts = rng.uniform([0.0, 0.0, 0.0], [1.5, 1.0, 0.5], size=(50, 3))
    got = fg.interp(pts)
    expected = np.stack([pts[:, 0], 2 * pts[:, 1] + 1, 3 * pts[:, 2] - 2], axis=1)
    assert np.allclose(got, expected, atol=1e-12)


def test_interp_out_of_bounds_returns_zero():
    fg = make_linear_grid()
    got = fg.interp(np.array([[10.0, 0.0, 0.0], [-1.0, 0.0, 0.0]]))
    assert np.allclose(got, 0.0)


def test_decompose_axis_aligned():
    E = np.array([[3.0 + 1j, 4.0, 0.0]])
    n = np.array([[1.0, 0.0, 0.0]])
    en, et = decompose_normal_tangential(E, n)
    assert np.allclose(en, [[3.0 + 1j, 0.0, 0.0]])
    assert np.allclose(et, [[0.0, 4.0, 0.0]])


def test_decompose_oblique_normal():
    n = np.array([[1.0, 1.0, 0.0]]) / np.sqrt(2.0)
    E = np.array([[2.0, 0.0, 0.0]])
    en, et = decompose_normal_tangential(E, n)
    # E·n = √2 → E_normal = √2·n = (1,1,0)；E_t = E − E_normal = (1,−1,0)
    assert np.allclose(en, [[1.0, 1.0, 0.0]])
    assert np.allclose(et, [[1.0, -1.0, 0.0]])


def test_decompose_normalizes_automatically():
    E = np.array([[3.0, 0.0, 0.0]])
    en, _ = decompose_normal_tangential(E, np.array([[5.0, 0.0, 0.0]]))
    assert np.allclose(en, [[3.0, 0.0, 0.0]])


# --------------------------------------------------------------------- #
# WLS 取场（新交点方案：采样点是亚格点、且一般不在导出网格节点上）
# --------------------------------------------------------------------- #
def linear_wls_field(x, y):
    """解析场：三个分量都是 (x, y) 的线性函数（含复系数）。

    一阶 WLS 对线性场精确（权重怎么取都对），因此是"插值器实现正确"的
    干净判据。x/y 既可以是网格矩阵、也可以是采样点向量。
    """
    return np.stack([
        1.0 + 0.5 * x - 0.25 * y + 0.0j,
        2.0 - 1.0 * x + 0.75 * y + 1j * (0.3 + x),
        3.0 + 0.2 * y - 0.4j,
    ], axis=-1)


def make_wls_grid() -> FieldGrid:
    """步长 0.1 的导出网格，原点刻意错开设计区半格（服务器实测的 y=-3.55）。"""
    x = -6.0 + 0.1 * np.arange(120)
    y = -4.0 + 0.1 * np.arange(80)
    z = np.array([-0.6985, -0.5985])
    X, Y, _ = np.meshgrid(x, y, z, indexing="ij")
    data = np.stack([linear_wls_field(X[:, :, k], Y[:, :, k]) for k in range(2)],
                    axis=2)
    return FieldGrid(origin=(float(x[0]), float(y[0]), float(z[0])),
                     spacing=(0.1, 0.1, 0.1), data=data.astype(complex))


def test_nearest_z_plane_snaps_to_the_export_grid():
    fg = make_wls_grid()
    iz, z_used = fg.nearest_z_plane(-0.6)
    assert (iz, z_used) == (1, -0.5985)
    assert fg.plane(iz).shape == (120, 80, 3)


def test_wls_reproduces_a_linear_field_exactly():
    fg = make_wls_grid()
    rng = np.random.default_rng(3)
    pts = np.column_stack([rng.uniform(-4.0, 3.0, 40), rng.uniform(-2.0, 2.0, 40)])
    iz, _ = fg.nearest_z_plane(-0.6)
    res = fg.sample_wls(pts, iz)
    assert res.n_fallback == 0
    assert res.n_neighbors.min() >= 6
    assert np.allclose(res.values, linear_wls_field(pts[:, 0], pts[:, 1]), atol=1e-9)


def test_wls_uses_only_the_selected_side():
    """PEC 界面两侧场不连续（金属体内 E≈0）：只用介质侧节点才拿得到空气侧的值。"""
    fg = make_wls_grid()
    xs, ys, _ = fg.axes()
    X, Y, _ = np.meshgrid(xs, ys, fg.axes()[2], indexing="ij")
    field = linear_wls_field(X, Y)
    field[Y < 0.0] = 0.0
    fg_metal = FieldGrid(fg.origin, fg.spacing, field)
    pts = np.array([[0.0, 0.0], [0.35, 0.0], [-0.7, 0.0]])  # 全在界面上
    iz, _ = fg_metal.nearest_z_plane(-0.6)
    medium = Y[:, :, 0] > 0.0
    good = fg_metal.sample_wls(pts, iz, node_mask=medium).values
    both = fg_metal.sample_wls(pts, iz).values
    want = linear_wls_field(pts[:, 0], pts[:, 1])
    assert np.allclose(good, want, atol=1e-9)
    # 混入金属侧节点会把估计拉向 0（这正是必须做单侧拟合的理由）
    assert np.abs(both - want).max() > 0.1 * np.abs(want).max()


def test_wls_weights_reproduce_constants_and_share_across_planes():
    fg = make_wls_grid()
    pts = np.array([[0.0333, -0.0777], [1.25, 0.125]])
    iz0, iz1 = 0, 1
    st = build_wls_stencil(pts, fg.axes()[:2])
    assert np.allclose(st.weight.sum(axis=1), 1.0)
    a = fg.sample_wls(pts, iz0, stencil=st).values
    b = fg.sample_wls(pts, iz1, stencil=st).values
    # 同一算子作用在两个不同 z 面上：数据随 z 变，但算子不变
    assert np.allclose(a, b)  # 本测试场与 z 无关
    with pytest.raises(ValueError, match="形状"):
        fg.sample_wls(pts, iz0, node_mask=np.ones((7, 9), dtype=bool))


def test_wls_falls_back_to_idw_when_the_stencil_is_thin():
    """邻域里可用节点太少/共线 → 退化，必须计数而不是抛出或给 NaN。"""
    fg = make_wls_grid()
    ny = fg.data.shape[1]
    # 只留 ∂Ω 外的一行节点：任何采样点周围都凑不出满秩模板
    thin = np.zeros(fg.data.shape[:2], dtype=bool)
    thin[:, ny // 2] = True
    pts = np.array([[-2.0, -0.05], [1.0, 0.05]])
    iz, _ = fg.nearest_z_plane(-0.6)
    res = fg.sample_wls(pts, iz, node_mask=thin)
    assert res.n_fallback == 2
    assert np.isfinite(res.values).all()
    # 空掩膜：没有可用节点 → 0，且不报错
    empty = np.zeros(fg.data.shape[:2], dtype=bool)
    zeros = fg.sample_wls(pts, iz, node_mask=empty)
    assert zeros.n_fallback == 2
    assert np.allclose(zeros.values, 0.0) and (zeros.n_neighbors == 0).all()
