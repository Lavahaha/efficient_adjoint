"""CST ASCII 场文件解析测试（合成文件，格式与 CST ASCIIExport 对应）。"""

import numpy as np

from eaopt.solver.ascii_fields import parse_ascii_field


def _write_synthetic(path):
    """3×2×2 网格，线性场 f(x,y,z) = x + 2y + 3z，复场布局 A
    （每分量先全 Re 后全 Im）。"""
    nx, ny, nz = 3, 2, 2
    xs = np.linspace(0.0, 2.0, nx)
    ys = np.linspace(0.0, 1.0, ny)
    zs = np.linspace(0.0, 0.1, nz)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    f = X + 2 * Y + 3 * Z
    comps = [f, 0.1 * f, -0.2 * f]  # 三个分量
    lines = [
        "% CST Studio Suite export (synthetic)",
        f"{xs[0]} {xs[-1]} {nx}",
        f"{ys[0]} {ys[-1]} {ny}",
        f"{zs[0]} {zs[-1]} {nz}",
    ]
    # 布局 A：先全部 Re 块（Re_x, Re_y, Re_z），再全部 Im 块
    for c in comps:
        lines.append(" ".join(f"{v:.6f}" for v in c.ravel(order="F")))
    for c in comps:
        lines.append(" ".join(f"{0.5 * v:.6f}" for v in c.ravel(order="F")))
    path.write_text("\n".join(lines), encoding="utf-8")
    return xs, ys, zs, f


def test_parse_synthetic(tmp_path):
    p = tmp_path / "e.txt"
    xs, ys, zs, f = _write_synthetic(p)
    data, axes = parse_ascii_field(str(p))
    assert data.shape == (3, 2, 2, 3)
    np.testing.assert_allclose(axes[0], xs)
    np.testing.assert_allclose(axes[1], ys)
    np.testing.assert_allclose(axes[2], zs)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    for c, scale in enumerate((1.0, 0.1, -0.2)):
        expected = (X + 2 * Y + 3 * Z) * scale
        np.testing.assert_allclose(data[..., c].real, expected, atol=1e-9)
        np.testing.assert_allclose(data[..., c].imag, 0.5 * expected, atol=1e-9)


def test_parse_too_few_values_raises(tmp_path):
    p = tmp_path / "bad.txt"
    p.write_text("% x\n0 1 2\n0 1 2\n0 1 2\n1 2 3\n", encoding="utf-8")
    try:
        parse_ascii_field(str(p))
    except ValueError:
        pass
    else:
        raise AssertionError("数值不足应报错")
