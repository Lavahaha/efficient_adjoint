"""FieldGrid 插值与法/切分解测试（合成场、手算参考）。"""

import numpy as np

from eaopt.adjoint.fields import FieldGrid, decompose_normal_tangential


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
