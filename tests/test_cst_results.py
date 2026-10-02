"""结果读取层（cst_results）——**不装 CST 也能全绿**。

锁三件事：
  1. S 参数读的是 ``S_{i,激励端口}``（不是写死的 ``S_{i,1}``——正是这一层
     要参数化掉的东西），且条目名带后缀时能按前缀找到真实条目；
  2. 读不到就**抛**，绝不放行一个假值（伴随法对场/S 参数是线性的，
     一个 0 会静默污染整个梯度）；
  3. 场导出 → ASCII 解析 → 裁剪这条链的坐标/形状/数值都对得上
    （导出设置与解析器是两处独立实现的格式约定，必须端到端锁住）。
"""

import numpy as np
import pytest

from eaopt.adjoint.fields import FieldGrid
from eaopt.solver import cst_api
from eaopt.solver import cst_results as R
from eaopt.solver import vba as V


# --------------------------------------------------------------------------- #
# 假的 cst.results（形状与官方 API 一致：ProjectFile(...).get_3d() → ResultModule）
# --------------------------------------------------------------------------- #
class FakeItem:
    def __init__(self, x, y):
        self._x = np.asarray(x, dtype=float)
        self._y = np.asarray(y, dtype=complex)

    def get_xdata(self):
        return self._x

    def get_ydata(self):
        return self._y


class FakeResultModule:
    """真条目在 self.items 里；self.tree 是 get_tree_items 会吐出的条目名。"""

    def __init__(self, items, tree=()):
        self.items = dict(items)
        self.tree = list(tree)
        self.asked: list[str] = []

    def get_result_item(self, path, run_id=0, load_impedances=True):
        self.asked.append(path)
        if path not in self.items:
            raise KeyError(f"结果树里没有条目 {path!r}")
        return self.items[path]

    def get_tree_items(self, *args, **kwargs):
        return list(self.tree)


class FakeResults:
    def __init__(self, module=None, error=None):
        self._module, self._error = module, error
        self.calls: list[dict] = []

    def ProjectFile(self, path, allow_interactive=False):
        self.calls.append({"path": str(path), "allow_interactive": allow_interactive})
        if self._error is not None:
            raise self._error
        return _FakePF(self._module)


class _FakePF:
    def __init__(self, module):
        self._module = module

    def get_3d(self):
        return self._module


@pytest.fixture
def fake_cst(monkeypatch):
    """把 cst_results.load_cst 换成返回假 cst 库（不碰真实 import 机制）。"""
    def install(results):
        mods = (object(), object(), results)
        monkeypatch.setattr(R, "load_cst", lambda **kw: mods)
        return results
    return install


def _curve(re=0.1, im=0.0, re2=0.3, im2=0.2):
    return FakeItem([4.0, 6.0], [complex(re, im), complex(re2, im2)])


# --------------------------------------------------------------------------- #
# S 参数
# --------------------------------------------------------------------------- #
def test_s_param_item_uses_cst_naming():
    """CST 记法：S{响应},{激励} —— S3,1 = 端口 1 激励、端口 3 响应。"""
    assert R.s_param_item(3, 1) == "1D Results\\S-Parameters\\S3,1"
    assert R.s_param_item(1, 3) == "1D Results\\S-Parameters\\S1,3"


def test_read_s_params_reads_the_stimulus_port_and_interpolates(fake_cst):
    """激励端口是**参数**：后向工程该读 S{i},3，读成 S{i},1 就是错的模型。

    同时锁插值：5 GHz 落在 4/6 GHz 两点之间，取中点。
    """
    # 每条曲线的虚部随端口号变化：能顺带证明"读的是对的那一条"
    items = {R.s_param_item(i, 3): _curve(im2=0.2 + 0.1 * i) for i in (1, 2, 3, 4)}
    rm = FakeResultModule(items)
    res = fake_cst(FakeResults(rm))

    sp = R.read_s_params("p.cst", stimulus_port=3, response_ports=(1, 2, 3, 4),
                         freq_ghz=5.0, allow_interactive=True, log=lambda *a: None)

    assert set(sp) == {(1, 3), (2, 3), (3, 3), (4, 3)}
    for i in (1, 2, 3, 4):
        # 4/6 GHz 两点取中点：实部 (0.1+0.3)/2 = 0.2；虚部 (0 + 0.2+0.1i)/2
        assert sp[(i, 3)] == pytest.approx(0.2 + (0.1 + 0.05 * i) * 1j)
    assert res.calls[0]["allow_interactive"] is True    # 工程开着时必须交互读
    assert rm.asked == [R.s_param_item(i, 3) for i in (1, 2, 3, 4)]
    assert all("S1,1" not in p for p in rm.asked)


def test_read_s_params_finds_prefixed_tree_items(fake_cst):
    """条目名会被 CST 加后缀（S1,1 → "S1,1 [run 1]"）：按叶子前缀找真条目。

    这条经验来自 COM 时代（cst_api.find_item），这里同样适用——拿名字硬拼
    路径会在求解器/监视器类型一变就失效。
    """
    real = "1D Results\\S-Parameters\\S1,3 [AC]"
    rm = FakeResultModule({real: _curve()}, tree=[real])
    fake_cst(FakeResults(rm))

    sp = R.read_s_params("p.cst", stimulus_port=3, response_ports=(1,),
                         freq_ghz=5.0, log=lambda *a: None)
    assert sp[(1, 3)] == pytest.approx(0.2 + 0.1j)
    assert real in rm.asked


def test_read_s_params_raises_instead_of_returning_zeros(fake_cst):
    """读不到 S 参数必须抛——塞 0 进伴随法是最坏的一种"成功"。"""
    fake_cst(FakeResults(error=RuntimeError("文件被锁")))
    with pytest.raises(RuntimeError) as ei:
        R.read_s_params("p.cst", stimulus_port=1, freq_ghz=5.0,
                        log=lambda *a: None)
    msg = str(ei.value)
    assert "文件被锁" in msg                      # 原始错误必须保留
    assert "工程存盘了吗" in msg                   # 并给出排查顺序


def test_read_s_params_falls_back_to_the_live_result_tree(fake_cst):
    """官方库读不到时退回 ResultTree（工程正开着才可用）——两条链都保留。"""
    fake_cst(FakeResults(error=RuntimeError("没有 cst.results")))

    class Res:
        def __init__(self, yre, yim):
            self._a = {"x": [4.0, 6.0], "yre": yre, "yim": yim}

        def GetArray(self, key):
            return self._a[key]

    class Tree:
        def GetFirstChildName(self, folder):
            return "1D Results\\S-Parameters\\S1,1"

        def GetNextItemName(self, item):
            return ""

        def GetResultIDsFromTreeItem(self, path):
            return [path.rsplit("\\", 1)[-1]]

        def GetResultFromTreeItem(self, path, rid):
            # 0.1+0j → 0.3+0.2j：5 GHz 处 = 0.2+0.1j
            return Res([0.1, 0.3], [0.0, 0.2])

    class Model:
        ResultTree = Tree()

    sp = R.read_s_params("p.cst", stimulus_port=1, response_ports=(1,),
                         freq_ghz=5.0, model3d=Model(), log=lambda *a: None)
    assert sp[(1, 1)] == pytest.approx(0.2 + 0.1j)


def test_result_tree_chain_reports_why_it_failed():
    """回退链失败时 notes 里要有 cst_api 的原始错误（否则只能靠猜）。"""
    class Tree:
        def GetFirstChildName(self, folder):
            raise RuntimeError("<unknown>.GetFirstChildName")

    class Model:
        ResultTree = Tree()

    notes: list[str] = []
    out = R.s_params_via_result_tree(Model(), 1, (1,), 5.0, notes,
                                     log=lambda *a: None)
    assert out == {}
    assert any("GetFirstChildName" in n for n in notes)


# --------------------------------------------------------------------------- #
# 场导出
# --------------------------------------------------------------------------- #
class FakeASCIIExport:
    def __init__(self, on_execute):
        self.calls: list[tuple] = []
        self._on_execute = on_execute

    def Reset(self):
        self.calls.append(("Reset",))

    def FileName(self, v):
        self.calls.append(("FileName", v))

    def Mode(self, v):
        self.calls.append(("Mode", v))

    def StepX(self, v):
        self.calls.append(("StepX", v))

    def StepY(self, v):
        self.calls.append(("StepY", v))

    def StepZ(self, v):
        self.calls.append(("StepZ", v))

    def Execute(self):
        self.calls.append(("Execute",))
        self._on_execute()


class FakeTree:
    def __init__(self, children):
        self._c = list(children)

    def GetFirstChildName(self, folder):
        return self._c[0] if self._c else ""

    def GetNextItemName(self, item):
        i = self._c.index(item)
        return self._c[i + 1] if i + 1 < len(self._c) else ""


class FakeModel3D:
    def __init__(self, item_path, export, tree=()):
        self._item = item_path
        self.ASCIIExport = export
        self.selected: list[str] = []
        self.ResultTree = FakeTree(tree)

    def SelectTreeItem(self, item):
        if item != self._item:
            raise RuntimeError(f"<unknown>.SelectTreeItem({item!r})")
        self.selected.append(item)


def _write_ascii(path, data, axes):
    """按 CST FixedWidth 导出的格式写一个场文件（解析器的输入格式）。"""
    nx, ny, nz, _ = data.shape
    vals: list[float] = []
    for c in range(3):
        vals += list(np.asarray(data[..., c]).reshape(-1, order="F").real)
    for c in range(3):
        vals += list(np.asarray(data[..., c]).reshape(-1, order="F").imag)
    lines = [f"{axes[0][0]} {axes[0][-1]} {nx}",
             f"{axes[1][0]} {axes[1][-1]} {ny}",
             f"{axes[2][0]} {axes[2][-1]} {nz}"]
    lines += [f"{v:.12g}" for v in vals]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_export_field_grid_roundtrips_the_ascii_file(tmp_path):
    """导出设置（FixedWidth + mm 步长）与解析器是两处独立实现，端到端锁住。"""
    nx, ny, nz = 3, 2, 2
    data = (np.arange(nx * ny * nz * 3, dtype=float)
            .reshape(nx, ny, nz, 3) + 0j)
    data = data + 1j * np.flip(data.real, axis=0)
    axes = (np.array([0.0, 0.5, 1.0]), np.array([-1.0, 0.0]),
            np.array([0.0, 0.25]))
    out = tmp_path / "e.txt"

    def write():
        _write_ascii(out, data, axes)

    exp = FakeASCIIExport(write)
    item = "2D/3D Results\\E-Field\\e-field (f=5) [AC]"
    m3d = FakeModel3D(item, exp, tree=[item])

    grid = R.export_field_grid(m3d, "Efield", 5.0, 0.2, out,
                               log=lambda *a: None)

    assert m3d.selected == [item]                    # 用的是结果树里找到的真实条目
    kinds = [c[0] for c in exp.calls]
    assert kinds[0] == "Reset"                       # Reset 必须最先
    assert kinds.index("FileName") < kinds.index("Mode") < kinds.index("Execute")
    assert ("Mode", "FixedWidth") in exp.calls
    assert ("StepX", "0.2") in exp.calls and ("StepZ", "0.2") in exp.calls
    assert grid.data.shape == (nx, ny, nz, 3)
    assert grid.origin == pytest.approx((0.0, -1.0, 0.0))
    assert grid.spacing == pytest.approx((0.5, 1.0, 0.25))
    assert np.allclose(grid.data, data)


def test_export_field_grid_rejects_a_single_slice_axis(tmp_path):
    """单层轴没有"步长"可言——必须指名报错，不能让插值器悄悄退化。"""
    out = tmp_path / "e.txt"
    data = np.zeros((2, 2, 1, 3), dtype=complex)
    axes = (np.array([0.0, 0.5]), np.array([0.0, 0.5]), np.array([0.0]))
    exp = FakeASCIIExport(lambda: _write_ascii(out, data, axes))
    m3d = FakeModel3D(V.field_result_path("Efield", 5.0), exp, tree=[])

    with pytest.raises(RuntimeError, match="z 轴只有 1 个采样点"):
        R.export_field_grid(m3d, "Efield", 5.0, 0.2, out, log=lambda *a: None)


def test_export_field_grid_falls_back_to_conventional_path(tmp_path):
    """结果树里找不到时退回按惯例拼的路径（并把这件事记进 notes）。"""
    out = tmp_path / "e.txt"
    nx, ny, nz = 2, 2, 2
    data = np.zeros((nx, ny, nz, 3), dtype=complex)
    axes = (np.array([0.0, 0.5]), np.array([0.0, 0.5]), np.array([0.0, 0.5]))
    exp = FakeASCIIExport(lambda: _write_ascii(out, data, axes))
    m3d = FakeModel3D("2D/3D Results\\E-Field\\e-field (f=5) [AC]", exp,
                      tree=[])                        # 结果树是空的

    notes: list[str] = []
    R.export_field_grid(m3d, "Efield", 5.0, 0.2, out, notes=notes,
                        log=lambda *a: None)
    assert m3d.selected == ["2D/3D Results\\E-Field\\e-field (f=5) [AC]"]
    assert any("按惯例拼" in n for n in notes)


def test_export_field_grid_names_the_missing_property(tmp_path):
    """ASCIIExport 属性对不上时，要指名道姓地报错（并列出实际成员）。"""
    class NoMode:
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

    m3d = FakeModel3D(V.field_result_path("Efield", 5.0), NoMode(), tree=[])
    with pytest.raises(RuntimeError, match="ASCIIExport 没有 Mode"):
        R.export_field_grid(m3d, "Efield", 5.0, 0.2, tmp_path / "e.txt",
                            log=lambda *a: None)


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


def test_result_tree_helpers_are_reused_from_cst_api():
    """回退链复用 cst_api 的鸭子类型函数（不是另写一份前缀匹配）。"""
    assert R.cst_api is cst_api
