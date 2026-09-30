"""VBA 命令字符串与模板宏生成测试（纯字符串，无需 CST）。"""

import numpy as np

from eaopt.solver import vba as V
from eaopt.solver.template_builder import build_all_templates, build_macro


def test_polygon_extrude_contains_commands():
    s = V.polygon_extrude("arm", "design_region", "PEC",
                          [(0.0, -1.6), (12.0, -1.6), (12.0, 0.0), (0.0, 0.0)],
                          0.035)
    assert '.Component "design_region"' in s
    assert '.Material "PEC"' in s
    assert '.Height "0.035"' in s
    assert '.LineTo "12", "-1.6"' in s
    assert '.Point "0", "-1.6"' in s


def test_brick_contains_ranges():
    s = V.brick("sub", "c1", "Rogers4350B", -4.0, 16.0, -6.0, 6.0, -0.762, 0.0)
    assert '.Xrange "-4", "16"' in s
    assert '.Zrange "-0.762", "0"' in s


def test_port_yface():
    s = V.waveguide_port_yface(1, "p1", 4.5, "negative", -3.6, 1.2, -0.762, 2.0)
    assert '.PortNumber "1"' in s
    assert '.Orientation "negative"' in s
    assert '.Yrange "4.5", "4.5"' in s  # 端口面为 y=const 平面
    assert '.Coordinates "Ranges"' in s


def test_excitation_supports_vba_variable():
    s = V.excitation("exc", "portnum")
    assert ".Port portnum" in s  # VBA 变量：不带引号


def test_build_macro_each_template_is_self_contained(tmp_path):
    """每个模板一个命令宏：全字面量、无工程级指令、自带 SaveAs。"""
    paths = build_all_templates(tmp_path)
    assert [p.name for p in paths] == ["build_coupler_fwd.mcr",
                                       "build_coupler_bwd.mcr"]
    for path, project, port in zip(paths, ("coupler_fwd", "coupler_bwd"), (1, 3)):
        assert path.suffix == ".mcr"  # 命令宏：工程级指令仅在此上下文合法
        assert path.read_bytes().count(b"\r\n") > 10  # Windows VBA 换行
        text = path.read_text(encoding="utf-8")
        assert text.startswith("'#Language \"WWB-COM\"")
        assert "Sub Main()" in text and "End Sub" in text
        # 只看可执行语句（注释里会提到 NewProject 以说明为何不用它）
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("'"))
        assert "NewProject" not in code  # 实测非法：新建工程由 GUI 完成
        assert "portnum As Integer" not in text  # 参数全部字面量
        assert 'SaveAs "' in text and f"{project}.cst" in text
        assert f'.Port "{port}"' in text
        assert "Rogers4350B" in text
        # 4 个端口
        assert text.count("With Port") == 4
        # 边界：zmax 电边界、其余磁边界
        assert '.Zmax "electric"' in text
        assert text.count('"magnetic"') == 5
    # 激励端口互不相同（fwd=1 / bwd=3）
    fwd, bwd = (p.read_text(encoding="utf-8") for p in paths)
    assert '.Port "1"' in fwd and '.Port "3"' not in fwd
    assert '.Port "3"' in bwd and '.Port "1"' not in bwd


def test_build_macro_single(tmp_path):
    path = build_macro(tmp_path, "demo", 2)
    assert path.name == "build_demo.mcr"
    assert "demo.cst" in path.read_text(encoding="utf-8")
