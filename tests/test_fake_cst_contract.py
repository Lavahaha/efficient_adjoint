"""假 CST 库自己的合同：先证明"假得够真"，再拿它锁生产代码。

这里测的是 ``tests/fake_cst/cst/`` 的行为与真库实测结论一致（存盘才读得到、
同一工程不重复打开、Model3D 上没有 ResultTree、save 形参可退化），以及
conftest 的注入确实生效——生产代码的测试全都站在这个地基上。
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


def test_model3d_has_no_result_tree():
    """活工程 ResultTree 回退链已删：假 Model3D 不提供它，残留旧路会当场炸。"""
    prj = csti.DesignEnvironment.new_mws()
    assert not hasattr(prj.model3d, "ResultTree")


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


def test_ascii_export_writes_a_parseable_grid(tmp_path):
    st = csti.configure(grid=csti.grid_for((0.0, 4.0), (-1.0, 1.0), margin=0.5))
    prj = csti.DesignEnvironment.new_mws()
    out = tmp_path / "field.txt"
    prj.model3d.ASCIIExport.Reset()
    prj.model3d.ASCIIExport.FileName(str(out))
    prj.model3d.ASCIIExport.Execute()
    assert out.is_file()
    assert st.grid[0][0] < 0.0 < st.grid[0][-1]   # 网格比设计区大一圈
