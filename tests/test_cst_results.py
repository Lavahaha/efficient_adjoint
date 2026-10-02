"""结果读取层（cst_results）——**不装 CST 也能全绿**（走假 cst.results）。

锁四件事：
  1. S 参数读的是 ``S_{i,激励端口}``（不是写死的 ``S_{i,1}``——正是这一层
     要参数化掉的东西），且工程必须先存盘（真库读磁盘结果）；
  2. 读不到就**抛**，绝不放行一个假值（伴随法对场/S 参数是线性的，
     一个 0 会静默污染整个梯度）；
  3. 场导出 → ASCII 解析 → 裁剪这条链的坐标/形状/数值都对得上
     （导出设置与解析器是两处独立实现的格式约定，必须端到端锁住）；
  4. 场条目走 ``cst_model.field_result_path`` 的惯例路径（不再枚举结果树）。
"""

import numpy as np
import pytest

from cst import interface as csti          # 假 cst.interface（conftest 注入）
from cst import results as cstr            # 假 cst.results

from eaopt.adjoint.fields import FieldGrid
from eaopt.solver import cst_model as M
from eaopt.solver import cst_results as R


def _quiet(*_a, **_kw):
    pass


def _project(tmp_path, name="p.cst"):
    """建一个空工程文件——假 cst.results 照真库的规矩：文件不在就报错。"""
    p = tmp_path / name
    p.write_text("fake-cst-project", encoding="utf-8")
    return p


def _model():
    """一个假 Model3D（假 cst.interface 的常驻实例 → new_mws）。"""
    return csti.running_design_environments()[0].new_mws().model3d


# --------------------------------------------------------------------------- #
# S 参数
# --------------------------------------------------------------------------- #
def test_s_param_item_uses_cst_naming():
    """CST 记法：S{响应},{激励} —— S3,1 = 端口 1 激励、端口 3 响应。"""
    assert R.s_param_item(3, 1) == "1D Results\\S-Parameters\\S3,1"
    assert R.s_param_item(1, 3) == "1D Results\\S-Parameters\\S1,3"


def test_read_s_params_reads_the_stimulus_port_and_interpolates(tmp_path):
    """激励端口是**参数**：后向工程该读 S{i},3，读成 S{i},1 就是错的模型。

    同时锁插值：5 GHz 落在 4/6 GHz 两点之间，取中点。
    """
    # 每条曲线的虚部随端口号变化：能顺带证明"读的是对的那一条"
    cstr.configure(curves={
        R.s_param_item(i, 3): ([4.0, 6.0], [0j, complex(0.2, 0.2 + 0.1 * i)])
        for i in (1, 2, 3, 4)})
    path = _project(tmp_path)

    sp = R.read_s_params(path, stimulus_port=3, response_ports=(1, 2, 3, 4),
                         freq_ghz=5.0, log=_quiet)

    assert set(sp) == {(1, 3), (2, 3), (3, 3), (4, 3)}
    for i in (1, 2, 3, 4):
        assert sp[(i, 3)] == pytest.approx(0.1 + (0.1 + 0.05 * i) * 1j)
    st = cstr.state()
    assert st.opened == [(str(path), True)]     # 工程开着时必须交互读
    assert st.asked == [R.s_param_item(i, 3) for i in (1, 2, 3, 4)]
    assert all("S1,1" not in p for p in st.asked)


def test_read_s_params_retries_with_the_leaf_name(tmp_path):
    """条目名带后缀（S1,3 [run 1]）时全路径匹配失败 → 用叶子名再试一次。

    只重试一次、只换名字：不枚举结果树，免得"找一条能读的"掩盖真实失败。
    """
    full = R.s_param_item(1, 3)
    cstr.configure(missing={full})               # 全路径读不到
    path = _project(tmp_path)

    sp = R.read_s_params(path, stimulus_port=3, response_ports=(1,),
                         freq_ghz=5.0, log=_quiet)

    assert sp[(1, 3)] == pytest.approx(0.1 + 0.15j)   # 假库按条目名给值
    assert cstr.state().asked == [full, "S1,3"]


def test_read_s_params_raises_instead_of_returning_zeros(tmp_path):
    """读不到 S 参数必须抛——塞 0 进伴随法是最坏的一种"成功"。"""
    cstr.configure(item_error=RuntimeError("文件被锁"))
    path = _project(tmp_path)
    with pytest.raises(RuntimeError) as ei:
        R.read_s_params(path, stimulus_port=1, freq_ghz=5.0, log=_quiet)
    msg = str(ei.value)
    assert "文件被锁" in msg                      # 原始错误必须保留
    assert "工程存盘了吗" in msg                   # 并给出排查顺序


def test_read_s_params_requires_a_saved_project(tmp_path):
    """没存盘就没有结果可读（真库读磁盘）——报错要指向"先存盘"。"""
    with pytest.raises(RuntimeError) as ei:
        R.read_s_params(tmp_path / "nope.cst", stimulus_port=1, log=_quiet)
    assert "工程文件不存在" in str(ei.value)
    assert "工程存盘了吗" in str(ei.value)


# --------------------------------------------------------------------------- #
# ASCII 场文件解析（合成文件，格式与 CST ASCIIExport 对应）
# --------------------------------------------------------------------------- #
def _write_synthetic(path):
    """3×2×2 网格，线性场 f(x,y,z) = x + 2y + 3z，复场布局 A
    （每分量先全 Re 后全 Im）。"""
    nx, ny, nz = 3, 2, 2
    xs = np.linspace(0.0, 2.0, nx)
    ys = np.linspace(0.0, 1.0, ny)
    zs = np.linspace(0.0, 0.1, nz)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    f = X + 2 * Y + 3 * Z
    comps = [f, 0.1 * f, -0.2 * f]               # 三个分量
    lines = [
        "% CST Studio Suite export (synthetic)",
        f"{xs[0]} {xs[-1]} {nx}",
        f"{ys[0]} {ys[-1]} {ny}",
        f"{zs[0]} {zs[-1]} {nz}",
    ]
    for c in comps:                              # 先全部 Re 块，再全部 Im 块
        lines.append(" ".join(f"{v:.6f}" for v in c.ravel(order="F")))
    for c in comps:
        lines.append(" ".join(f"{0.5 * v:.6f}" for v in c.ravel(order="F")))
    path.write_text("\n".join(lines), encoding="utf-8")
    return xs, ys, zs


def test_parse_ascii_field_reads_layout_a(tmp_path):
    """列优先、Re 三块后 Im 三块——解析出的坐标与数值都要对得上。"""
    p = tmp_path / "e.txt"
    xs, ys, zs = _write_synthetic(p)
    data, axes = R.parse_ascii_field(str(p))
    assert data.shape == (3, 2, 2, 3)
    np.testing.assert_allclose(axes[0], xs)
    np.testing.assert_allclose(axes[1], ys)
    np.testing.assert_allclose(axes[2], zs)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    for c, scale in enumerate((1.0, 0.1, -0.2)):
        expected = (X + 2 * Y + 3 * Z) * scale
        np.testing.assert_allclose(data[..., c].real, expected, atol=1e-9)
        np.testing.assert_allclose(data[..., c].imag, 0.5 * expected, atol=1e-9)


def test_parse_ascii_field_rejects_a_truncated_file(tmp_path):
    """数值不够读时必须抛——截断的文件会解析出"少一块"的场，不报错。"""
    p = tmp_path / "bad.txt"
    p.write_text("% x\n0 1 2\n0 1 2\n0 1 2\n1 2 3\n", encoding="utf-8")
    with pytest.raises(ValueError):
        R.parse_ascii_field(str(p))


# --------------------------------------------------------------------------- #
# 场导出
# --------------------------------------------------------------------------- #
class _NoMode:
    """少一个 Mode 的 ASCIIExport（模拟 API 与预期不符）。"""

    def Reset(self):
        pass

    def FileName(self, v):
        pass

    def StepX(self, v):
        pass

    def StepY(self, v):
        pass

    def StepZ(self, v):
        pass

    def Execute(self):
        pass


def test_export_field_grid_roundtrips_the_ascii_file(tmp_path):
    """导出设置（FixedWidth + mm 步长）与解析器是两处独立实现，端到端锁住。"""
    xs = np.arange(0.0, 1.5 + 1e-9, 0.5)         # 4 点
    ys = np.arange(-1.0, 0.0 + 1e-9, 0.5)        # 3 点
    zs = np.array([0.0, 0.25])                   # 2 点
    csti.configure(grid=(xs, ys, zs))
    m3d = _model()
    out = tmp_path / "e.txt"

    grid = R.export_field_grid(m3d, "Efield", 5.0, 0.2, out, log=_quiet)

    assert m3d.selected == [M.field_result_path("Efield", 5.0)]
    kinds = [c[0] for c in m3d.ASCIIExport.calls]
    assert kinds[0] == "Reset"                   # Reset 必须最先
    assert kinds.index("FileName") < kinds.index("Mode") < kinds.index("Execute")
    assert ("Mode", "FixedWidth") in m3d.ASCIIExport.calls
    assert ("StepX", "0.2") in m3d.ASCIIExport.calls
    assert ("StepZ", "0.2") in m3d.ASCIIExport.calls
    assert m3d.ASCIIExport.file == str(out)      # FileName 就是调用方给的文件
    assert grid.data.shape == (4, 3, 2, 3)
    assert grid.origin == pytest.approx((0.0, -1.0, 0.0))
    assert grid.spacing == pytest.approx((0.5, 0.5, 0.25))
    # 数值确实是导出文件里的（假库按下标写：分量 0 = (i+1)*(1+0.5j)）
    assert grid.data[0, 0, 0, 0] == pytest.approx(1.0 + 0.5j)
    assert grid.data[3, 2, 1, 0] == pytest.approx(4.0 + 2.0j)


def test_export_field_grid_rejects_a_single_slice_axis(tmp_path):
    """单层轴没有"步长"可言——必须指名报错，不能让插值器悄悄退化。"""
    csti.configure(grid=(np.array([0.0, 0.5]), np.array([0.0, 0.5]),
                         np.array([0.0])))
    with pytest.raises(RuntimeError, match="z 轴只有 1 个采样点"):
        R.export_field_grid(_model(), "Efield", 5.0, 0.2, tmp_path / "e.txt",
                            log=_quiet)


def test_export_field_grid_names_the_missing_property(tmp_path):
    """ASCIIExport 属性对不上时，要指名道姓地报错（并列出实际成员）。"""
    m3d = _model()
    m3d.ASCIIExport = _NoMode()
    with pytest.raises(RuntimeError, match="ASCIIExport 没有 Mode"):
        R.export_field_grid(m3d, "Efield", 5.0, 0.2, tmp_path / "e.txt",
                            log=_quiet)


def test_export_field_grid_reports_a_failed_selection(tmp_path):
    """选不中条目 ⇒ 导出必然读出旧文件/报错，所以当场抛并指向条目名。"""
    csti.configure(select_error=RuntimeError("<unknown>.SelectTreeItem"))
    with pytest.raises(RuntimeError) as ei:
        R.export_field_grid(_model(), "Hfield", 5.0, 0.2, tmp_path / "h.txt",
                            log=_quiet)
    assert "SelectTreeItem" in str(ei.value)
    assert M.field_monitor_name("Hfield", 5.0) in str(ei.value)


def test_export_field_cropped_cuts_to_the_design_region(tmp_path):
    """导出即裁剪：交出去的就是设计区 ± 余量那一块（整域网格不保留）。"""
    box = Box((2.0, 10.0), (2.0, 10.0))
    csti.configure(grid=csti.grid_for(box.x, box.y, margin=1.0))
    m3d = _model()

    cut = R.export_field_cropped(m3d, "Efield", 5.0, 0.2, tmp_path / "e.txt",
                                 box, margin_mm=0.5, log=_quiet)

    ax = cut.axes()
    assert box.x[0] - 0.5 - 1e-9 <= ax[0][0] and ax[0][-1] <= box.x[1] + 0.5 + 1e-9
    assert box.y[0] - 0.5 - 1e-9 <= ax[1][0] and ax[1][-1] <= box.y[1] + 0.5 + 1e-9
    assert cut.data.shape[3] == 3
    assert np.any(cut.data != 0)                 # 数值来自导出文件，不是空片


# --------------------------------------------------------------------------- #
# 裁剪
# --------------------------------------------------------------------------- #
def _grid(origin, spacing, shape):
    nx, ny, nz = shape
    data = np.zeros((nx, ny, nz, 3), dtype=complex)
    data[..., 0] = np.arange(nx)[:, None, None]
    data[..., 1] = np.arange(ny)[None, :, None]
    data[..., 2] = np.arange(nz)[None, None, :]
    return FieldGrid(origin=origin, spacing=spacing, data=data)


class Box:
    def __init__(self, x, y):
        self.x, self.y = x, y


def test_crop_grid_keeps_design_region_plus_margin():
    """裁掉设计区之外的部分，但保留 ±margin 的余量（采样点在边界外侧）。"""
    g = _grid((0.0, 0.0, -1.0), (0.5, 0.5, 0.5), (20, 20, 4))   # x,y ∈ [0,9.5]
    box = Box((2.0, 4.0), (2.0, 4.0))

    c = R.crop_grid(g, box, margin_mm=0.5)
    ax = c.axes()
    assert ax[0][0] == pytest.approx(1.5) and ax[0][-1] == pytest.approx(4.5)
    assert ax[1][0] == pytest.approx(1.5) and ax[1][-1] == pytest.approx(4.5)
    assert c.data.shape == (7, 7, 4, 3)              # 7 个 x × 7 个 y，z 不动
    assert c.spacing == pytest.approx(g.spacing)
    # 数值确实是原网格的那一块（不是重新插值出来的）
    assert c.data[0, 0, 0, 0] == pytest.approx(3.0)  # x 下标 3（=1.5 mm）
    assert c.data[0, 0, 0, 1] == pytest.approx(3.0)  # y 下标 3


def test_crop_grid_without_margin_is_exactly_the_box():
    g = _grid((0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (10, 10, 2))
    c = R.crop_grid(g, Box((2.0, 4.0), (3.0, 5.0)))
    assert c.axes()[0] == pytest.approx([2.0, 3.0, 4.0])
    assert c.axes()[1] == pytest.approx([3.0, 4.0, 5.0])


def test_crop_grid_rejects_a_disjoint_box():
    """区域与网格不相交时必须报错——静默返回空网格会让后续插值全 0。"""
    g = _grid((0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (4, 4, 1))
    with pytest.raises(ValueError, match="不相交"):
        R.crop_grid(g, Box((100.0, 102.0), (0.0, 2.0)))
