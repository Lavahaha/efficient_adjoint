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


def test_s_param_at_reports_missing_api_and_out_of_range():
    notes: list[str] = []
    assert cst_api.s_param_at(
        FakeResultTree(ids_raise=True), "p", 5.0, notes) is None
    assert notes and notes[0] == "ids=[]"

    notes2: list[str] = []
    v = cst_api.s_param_at(_s_param_tree(), "p", 12.0, notes2)
    assert v is not None                      # 端点截断，但不静默
    assert any("超出数据范围" in n for n in notes2)


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
