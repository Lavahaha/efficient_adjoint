"""VBA 命令字符串与模板宏生成测试（纯字符串，无需 CST）。"""

import numpy as np

from eaopt.solver import vba as V
from eaopt.solver.template_builder import build_macro


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


def test_build_macro_output(tmp_path):
    path = build_macro(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "Sub Main()" in text
    assert "Sub BuildProject(fname As String, portnum As Integer)" in text
    assert 'BuildProject "1", 3' not in text
    assert "coupler_fwd.cst" in text and "coupler_bwd.cst" in text
    assert ".Port portnum" in text  # 激励端口取 VBA 参数
    assert 'Rogers4350B' in text and "NewProject" in text
    assert "SaveAs fname" in text
    # 4 个端口
    assert text.count("With Port") == 4
    # 边界：zmax 电边界、其余磁边界
    assert '.Zmax "electric"' in text
    assert text.count('"magnetic"') == 5
