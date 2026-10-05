"""假 CST 库自己的合同：先证明"假得够真"，再拿它锁生产代码。

这里测的是 ``tests/fake_cst/cst/`` 的行为与真库实测结论一致（存盘才读得到、
同一工程不重复打开、结果树遍历协议、SelectTreeItem 的布尔与静默失效、
save 形参可退化），以及 conftest 的注入确实生效——生产代码的测试全都站在
这个地基上。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import cst
import cst.interface as csti
import cst.results as cstr

TESTS_DIR = Path(__file__).resolve().parent


def test_conftest_injected_the_fake_library():
    """导入到的是 tests/fake_cst 下的假库，不是真 CST、也不是别的同名包。"""
    assert TESTS_DIR in Path(cst.__file__).resolve().parents
    assert Path(csti.__file__).resolve().parent == TESTS_DIR / "fake_cst" / "cst"
    assert cst.__version__ == "fake-2024"


def test_state_is_reset_between_tests():
    """上一个测试改过的开关不会漏到下一个（autouse fixture 生效）。"""
    assert csti.state().fail_connect is False
    assert cstr.state().asked == []


def test_configure_rejects_unknown_switches():
    with pytest.raises(AttributeError, match="没有这个开关"):
        csti.configure(no_such_switch=True)


# --------------------------------------------------------------------------- #
# 官方 API 的实测行为
# --------------------------------------------------------------------------- #
def test_results_read_from_disk_so_the_project_must_be_saved_first(tmp_path):
    """真库读磁盘：工程没存盘就读不到——假库照做，锁"求解→存盘→读"。"""
    path = tmp_path / "p.cst"
    with pytest.raises(FileNotFoundError, match="先.*存盘|不存在"):
        cstr.ProjectFile(str(path))

    path.write_text("fake", encoding="utf-8")
    item = cstr.ProjectFile(str(path), allow_interactive=True).get_3d() \
        .get_result_item("1D Results\\S-Parameters\\S3,1")
    assert item.get_xdata().tolist() == [4.0, 6.0]
    np.testing.assert_allclose(item.get_ydata(), cstr.values_of("S3,1"))


def test_item_values_depend_on_the_item_name():
    """读错端口必须能从数值上看出来（值随条目名变）。"""
    np.testing.assert_allclose(cstr.values_of("1D Results\\S-Parameters\\S1,3")[0],
                               complex(0.1, 0.15))


def test_missing_items_raise_instead_of_returning_zeros(tmp_path):
    """条目读不到必须抛（真库同样抛）——绝不返回 0，否则错数据一路静默下去。"""
    path = tmp_path / "p.cst"
    path.write_text("fake", encoding="utf-8")
    item = "1D Results\\S-Parameters\\S1,1"
    cstr.configure(missing={item})
    module = cstr.ProjectFile(str(path)).get_3d()
    with pytest.raises(RuntimeError, match="条目不存在"):
        module.get_result_item(item)
    assert cstr.state().asked == [item]


def test_a_project_cannot_be_opened_twice(tmp_path):
    """同一个 .cst 重复打开会得到两个工程对象、互相覆盖存盘——假库直接拒绝。"""
    path = tmp_path / "a.cst"
    path.write_text("x", encoding="utf-8")
    de = csti.DesignEnvironment
    de.open_project(str(path))
    with pytest.raises(RuntimeError, match="已经打开"):
        de.open_project(str(path))


def test_result_tree_walks_the_flat_item_list():
    """结果树协议照真库：GetFirstChildName/GetNextItemName 都给**全路径**。"""
    prj = csti.DesignEnvironment.new_mws()
    tree = prj.model3d.ResultTree
    assert tree.GetFirstChildName("") == "1D Results"
    assert tree.GetFirstChildName("2D/3D Results") == "2D/3D Results\\E-Field"
    assert tree.GetNextItemName("2D/3D Results\\E-Field") == \
        "2D/3D Results\\H-Field"
    assert tree.GetNextItemName("2D/3D Results\\H-Field") == ""   # 空串收尾
    assert tree.GetFirstChildName("2D/3D Results\\E-Field\\e-field (f=5)") == ""


def test_select_tree_item_returns_whether_the_item_exists():
    """照真库返回布尔；不在树上时"静默不生效"，随后 Execute 才炸。"""
    prj = csti.DesignEnvironment.new_mws()
    m3d = prj.model3d
    assert m3d.SelectTreeItem("2D/3D Results\\E-Field\\e-field (f=5)") is True
    assert m3d.SelectTreeItem("2D/3D Results\\E-Field\\e-field (f=99)") is False
    m3d.ASCIIExport.FileName("whatever.txt")
    with pytest.raises(RuntimeError, match="not available for the current view"):
        m3d.ASCIIExport.Execute()


def test_save_can_be_configured_to_reject_allow_overwrite(tmp_path):
    """真库 save() 的形参没实测过：假库要能模拟"只认 save(path)"的版本。"""
    csti.configure(save_accepts_overwrite=False)
    prj = csti.DesignEnvironment.new_mws()
    with pytest.raises(TypeError, match="allow_overwrite"):
        prj.save(str(tmp_path / "p.cst"), allow_overwrite=True)
    prj.save(str(tmp_path / "p.cst"))           # 退化形参能用
    assert (tmp_path / "p.cst").is_file()


def test_events_record_the_call_order():
    st = csti.configure(fail_connect=True)
    de = csti.DesignEnvironment
    with pytest.raises(RuntimeError):
        de.connect_to_any()
    de.new()
    prj = de.new_mws()
    prj.model3d.add_to_history("Units", "cmd")
    prj.model3d.run_solver()
    assert [e[0] for e in st.events] == ["connect_to_any", "new", "new_mws",
                                         "run_solver"]


def test_history_failure_can_be_programmed_per_block():
    prj = csti.DesignEnvironment.new_mws()
    csti.configure(history_error=("Brick substrate", RuntimeError("(&H8000ffff)")))
    assert prj.model3d.add_to_history("Units", "ok") is True
    with pytest.raises(RuntimeError, match="H8000ffff"):
        prj.model3d.add_to_history("Brick substrate", "boom")


def test_results_plus_a_non_quiet_session_reproduce_the_blocking_dialog():
    """"已有结果 + 非静默 + 重跑"= 真机那个等人点的确认框（假库里抛错）。

    这是本轮修的坑的可测代理；两道闸（静默模式 / DeleteResults）任一生效，
    这条就消失——下面各测一闸。
    """
    de = csti.DesignEnvironment
    csti.configure(initial_has_results=True)      # 从磁盘打开 = 带着上一轮结果
    prj = de.open_project("has_results.cst")
    with pytest.raises(RuntimeError, match="need to be deleted"):
        prj.model3d.run_solver()

    prj.model3d._execute_vba_code("Sub Main\nDeleteResults\nEnd Sub")
    prj.model3d.run_solver()                       # 结果没了，框无从弹起
    assert prj.model3d.has_results is True         # 求解完又有结果了


def test_quiet_mode_is_session_state_and_swallows_the_dialog():
    de = csti.DesignEnvironment
    csti.configure(initial_has_results=True)
    prj = de.open_project("has_results.cst")
    assert de.in_quiet_mode() is False
    de.set_quiet_mode(True)
    assert ("set_quiet_mode", True) in csti.state().events
    prj.model3d.run_solver()                       # 静默 = 自动点掉，不等人
    assert de.in_quiet_mode() is True


def test_a_version_without_set_quiet_mode_raises_attributeerror():
    """老版本没这个方法：生产代码用 getattr 兜着，不能因此挂掉。"""
    csti.configure(has_quiet_mode_api=False)
    with pytest.raises(AttributeError):
        csti.DesignEnvironment.set_quiet_mode(True)


def test_control_vba_runs_outside_the_history_list():
    """DeleteResults 走控制宏：执行了，但历史表不长一条（形状记录才数它）。"""
    prj = csti.DesignEnvironment.new_mws()
    before = len(prj.model3d.history)
    prj.model3d._execute_vba_code("Sub Main\nDeleteResults\nEnd Sub")
    assert len(prj.model3d.history) == before
    assert prj.model3d.vba_calls[-1].count("DeleteResults") == 1
    with pytest.raises(RuntimeError, match="只实现了 DeleteResults"):
        prj.model3d._execute_vba_code("Sub Main\nBrick.Reset\nEnd Sub")


def test_ascii_export_writes_a_parseable_grid(tmp_path):
    st = csti.configure(grid=csti.grid_for((0.0, 4.0), (-1.0, 1.0), margin=0.5))
    prj = csti.DesignEnvironment.new_mws()
    out = tmp_path / "field.txt"
    assert prj.model3d.SelectTreeItem("2D/3D Results\\E-Field\\e-field (f=5)")
    prj.model3d.ASCIIExport.Reset()
    prj.model3d.ASCIIExport.FileName(str(out))
    prj.model3d.ASCIIExport.Execute()
    assert out.is_file()
    assert st.grid[0][0] < 0.0 < st.grid[0][-1]   # 网格比设计区大一圈
