"""cst_api：CST COM 结果读取候选链（用假 ResultTree 单测，不依赖 CST）。

实测背景（CST 2024）：ResultTree.GetResultItem / GetAllItems 都不存在；
可用链是 GetResultIDsFromTreeItem + GetResultFromTreeItem +
GetArray("x"/"yre"/"yim")，结果树遍历用 GetFirstChildName /
GetNextItemName（返回空串结束）。
"""

import pytest

from eaopt.solver import cst_api


class FakeResult:
    def __init__(self, arrays):
        self._arrays = arrays

    def GetArray(self, key):
        if key not in self._arrays:
            raise RuntimeError(f"没有数组 {key}")
        return self._arrays[key]


class FakeResultTree:
    """够用的假 ResultTree：只实现 cst_api 用到的四个方法。"""

    def __init__(self, ids=(), results=None, children=(), ids_raise=False):
        self.ids = list(ids)
        self.results = results or {}
        self.children = list(children)
        self.ids_raise = ids_raise

    def GetResultIDsFromTreeItem(self, path):
        if self.ids_raise:
            raise RuntimeError("GetResultIDsFromTreeItem 不可用")
        return tuple(self.ids)

    def GetResultFromTreeItem(self, path, rid):
        return self.results[rid]

    def GetFirstChildName(self, folder):
        return self.children[0] if self.children else ""

    def GetNextItemName(self, item):
        i = self.children.index(item)
        return self.children[i + 1] if i + 1 < len(self.children) else ""


def _s_param_tree():
    """x=[4,5,6]，Re=[0,0.5,1]，Im=[0,-0.25,-0.5]。"""
    return FakeResultTree(
        ids=["S2,1"],
        results={"S2,1": FakeResult({"x": [4.0, 5.0, 6.0],
                                     "yre": [0.0, 0.5, 1.0],
                                     "yim": [0.0, -0.25, -0.5]})})


def test_s_param_at_interpolates_complex_value():
    v = cst_api.s_param_at(_s_param_tree(), "1D Results\\S-Parameters\\S2,1", 4.5)
    assert v.real == pytest.approx(0.25)
    assert v.imag == pytest.approx(-0.125)


def test_s_param_at_uses_first_id_with_usable_arrays():
    """第一个 ID 的数组布局不认识时继续试下一个。"""
    tree = FakeResultTree(
        ids=["bad", "good"],
        results={"bad": FakeResult({"x": [5.0, 6.0], "phase": [1.0, 2.0]}),
                 "good": FakeResult({"x": [5.0, 6.0], "yre": [1.0, 2.0],
                                     "yim": [0.0, 0.0]})})
    assert cst_api.s_param_at(tree, "p", 5.0) == complex(1.0, 0.0)


def test_s_param_at_falls_back_to_magnitude():
    tree = FakeResultTree(ids=["S"], results={
        "S": FakeResult({"x": [5.0, 6.0], "y": [0.5, 0.25]})})
    assert cst_api.s_param_at(tree, "p", 5.0) == complex(0.5, 0.0)


def test_s_param_at_reports_raw_error_when_api_missing():
    """API 不存在时必须留下**原始报错**（区分"没有结果"与"方法名不对"）。"""
    notes: list[str] = []
    assert cst_api.s_param_at(
        FakeResultTree(ids_raise=True), "p", 5.0, notes) is None
    assert any("GetResultIDsFromTreeItem" in n and "不可用" in n for n in notes)
    assert "ids=[]" in notes


def test_s_param_at_reports_out_of_range():
    notes: list[str] = []
    v = cst_api.s_param_at(_s_param_tree(), "p", 12.0, notes)
    assert v is not None                      # 端点截断，但不静默
    assert any("超出数据范围" in n for n in notes)


def test_s_param_at_flags_implausible_magnitude():
    """读到坐标轴之类的辅助条目时 |S| 会远大于 1，要留提示。"""
    tree = FakeResultTree(ids=["x"], results={
        "x": FakeResult({"x": [4.0, 6.0], "yre": [4.0, 6.0],
                         "yim": [0.0, 0.0]})})
    notes: list[str] = []
    assert cst_api.s_param_at(tree, "p", 5.0, notes) is not None
    assert any("可疑" in n for n in notes)


def test_s_param_at_falls_back_to_result1d_complex():
    """候选链 1 拿不到 ID 时，试 GetFileFromTreeItem + Result1DComplex。"""
    class OnlyFile(FakeResultTree):
        def GetResultIDsFromTreeItem(self, path):
            raise RuntimeError("<unknown>.GetResultIDsFromTreeItem")

        def GetFileFromTreeItem(self, path):
            return "S1,1.sig"

    class FakeResult1D:
        def GetClosestIndexFromX(self, f):
            return 2

        def GetY(self, i):
            return 0.25

        def GetYImag(self, i):
            return -0.5

    class Proj:
        def Result1DComplex(self, f):
            return FakeResult1D()

    notes: list[str] = []
    v = cst_api.s_param_at(OnlyFile(), "p", 5.0, notes, project=Proj())
    assert v == complex(0.25, -0.5)
    assert any("Result1DComplex" in n for n in notes)


def test_s_param_at_without_project_does_not_try_result1d():
    """没给工程对象时不该去碰 Result1DComplex（COM 里它挂在工程上）。"""
    notes: list[str] = []
    assert cst_api.s_param_at(FakeResultTree(), "p", 5.0, notes) is None
    assert not any("Result1DComplex" in n for n in notes)


def test_find_item_matches_leaf_prefix_and_reports_misses():
    kids = ["2D/3D Results\\E-Field\\e-field (f=5) [AC]",
            "2D/3D Results\\E-Field\\e-field (f=5) [pw]"]
    tree = FakeResultTree(children=kids)
    assert cst_api.find_item(tree, "f", "e-field (f=5)") == kids[0]
    notes: list[str] = []
    assert cst_api.find_item(tree, "f", "h-field (f=5)", notes) is None
    assert notes and "h-field (f=5)" in notes[0]


def test_tree_children_reports_raw_error():
    class Bad:
        def GetFirstChildName(self, folder):
            raise RuntimeError("<unknown>.GetFirstChildName")

    notes: list[str] = []
    assert cst_api.tree_children(Bad(), "1D Results", notes) == []
    assert notes and "<unknown>.GetFirstChildName" in notes[0]


def test_tree_children_walks_until_empty():
    kids = ["2D/3D Results\\E-Field\\a [AC]", "2D/3D Results\\E-Field\\b [AC]"]
    assert cst_api.tree_children(FakeResultTree(children=kids), "f") == kids
    assert cst_api.tree_children(FakeResultTree(), "f") == []


def test_tree_children_tolerates_missing_api():
    class NoTreeApi:
        def GetFirstChildName(self, folder):
            raise RuntimeError("nope")

    assert cst_api.tree_children(NoTreeApi(), "x") == []


def test_tree_children_breaks_on_self_referencing_next():
    """防御：万一 GetNextItemName 返回自身，不能死循环。"""
    class Stuck(FakeResultTree):
        def GetNextItemName(self, item):
            return item

    assert cst_api.tree_children(Stuck(children=["a"]), "f") == ["a"]


class _FakeTypeAttr:
    cFuncs = 3


class _FakeTypeInfo:
    """假 IDispatch 类型库：三个成员，其中一个 memid 重复（去重）、
    一个 GetFuncDesc 抛错（要跳过、不能整体失败）。"""

    _names = {1: ("AddToHistory",), 2: ("GetActiveProject",),
              3: ("AddToHistory",)}

    def GetTypeAttr(self):
        return _FakeTypeAttr()

    def GetFuncDesc(self, i):
        if i == 3:
            raise RuntimeError("bad index")
        return type("FD", (), {"memid": i + 1})()

    def GetNames(self, memid):
        return self._names[memid]


class _FakeOle:
    def GetTypeInfo(self):
        return _FakeTypeInfo()


class _FakeCom:
    _oleobj_ = _FakeOle()


def test_member_names_reads_type_library_and_dedupes():
    assert cst_api.member_names(_FakeCom()) == ["AddToHistory", "GetActiveProject"]


def test_member_names_filters_by_keyword_case_insensitively():
    assert cst_api.member_names(_FakeCom(), "history") == ["AddToHistory"]
    assert cst_api.member_names(_FakeCom(), "save") == []


def test_member_names_raises_when_type_info_unavailable():
    """拿不到类型信息要抛原始异常（调用方决定怎么报），不静默返回空表。"""
    class NoTypeInfo:
        def GetTypeInfo(self):
            raise RuntimeError("<unknown>.GetTypeInfo")

    with pytest.raises(RuntimeError, match="GetTypeInfo"):
        cst_api.member_names(NoTypeInfo())


class _FakeFuncDesc:
    def __init__(self, memid, cparams):
        self.memid = memid
        self.cParams = cparams


class _SigTypeInfo:
    """带形参名与帮助串的假类型库：AddToHistory(header, contents)。"""

    def GetTypeAttr(self):
        return type("TA", (), {"cFuncs": 1})()

    def GetFuncDesc(self, i):
        return _FakeFuncDesc(7, 2)

    def GetNames(self, memid):
        return ("AddToHistory", "header", "contents")

    def GetDocumentation(self, memid):
        return ("AddToHistory", " Adds an entry to the history list. ", "", 0)


class _SigCom:
    _oleobj_ = type("Ole", (), {
        "GetTypeInfo": lambda self: _SigTypeInfo()})()


def test_member_signatures_reports_param_names_and_help():
    sigs = cst_api.member_signatures(_SigCom())
    assert len(sigs) == 1
    s = sigs[0]
    assert s["name"] == "AddToHistory"
    assert s["params"] == ["header", "contents"]   # 形参名 = 参数顺序的权威答案
    assert s["n_params"] == 2
    assert "history list" in s["help"]


def test_member_names_is_the_name_column_of_signatures():
    assert cst_api.member_names(_SigCom()) == ["AddToHistory"]
    assert cst_api.member_names(_SigCom(), "HISTORY") == ["AddToHistory"]


def _fake_win32com(monkeypatch, dispatch, get_active):
    """装上假的 win32com.client（不装真 pywin32 也能测连接逻辑）。"""
    import sys
    import types

    pkg = types.ModuleType("win32com")
    pkg.__path__ = []
    client = types.ModuleType("win32com.client")
    client.Dispatch = dispatch
    client.GetActiveObject = get_active
    pkg.client = client
    monkeypatch.setitem(sys.modules, "win32com", pkg)
    monkeypatch.setitem(sys.modules, "win32com.client", client)


def test_connect_app_falls_back_to_get_active_object(monkeypatch):
    """Dispatch 失败要退回 GetActiveObject；两个都必须带上 PROGID。"""
    calls = []

    def dispatch(progid):
        calls.append(("Dispatch", progid))
        raise RuntimeError("no running instance")

    def get_active(progid):
        calls.append(("GetActiveObject", progid))
        return "APP"

    _fake_win32com(monkeypatch, dispatch, get_active)
    assert cst_api.connect_app() == "APP"
    assert [c[0] for c in calls] == ["Dispatch", "GetActiveObject"]
    assert all(c[1] == cst_api.PROGID for c in calls)


def test_connect_app_uses_dispatch_when_it_works(monkeypatch):
    def get_active(progid):
        raise AssertionError("Dispatch 成功就不该再试 GetActiveObject")

    _fake_win32com(monkeypatch, lambda progid: "APP", get_active)
    assert cst_api.connect_app() == "APP"


def test_connect_app_raises_with_both_errors(monkeypatch):
    def boom(progid):
        raise RuntimeError("nope")

    _fake_win32com(monkeypatch, boom, boom)
    with pytest.raises(RuntimeError, match="无法连接 CST"):
        cst_api.connect_app()
