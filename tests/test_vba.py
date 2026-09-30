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
    # 录制式：轮廓点内联在 Extrude 里，方向由 Uvector/Vvector 表达
    assert '.Mode "Pointlist"' in s
    assert '.Uvector "1.0", "0.0", "0.0"' in s
    assert '.Vvector "0.0", "1.0", "0.0"' in s
    assert "PlaneNormal" not in s  # CST 2024 无此属性（实测报错）
    assert "With Polygon" not in s  # 不再需要单独的曲线对象


def test_brick_contains_ranges():
    s = V.brick("sub", "c1", "Rogers4350B", -4.0, 16.0, -6.0, 6.0, -0.762, 0.0)
    assert '.Xrange "-4", "16"' in s
    assert '.Zrange "-0.762", "0"' in s


def test_port_yface():
    """y=const 端口面：Yrange 为常量，Xrange 为范围。"""
    s = V.waveguide_port(3, "p3", "y", -7.0, "positive", -3.2, 1.6, -0.762, 2.0)
    assert '.PortNumber "3"' in s
    assert '.Orientation "positive"' in s
    assert '.Yrange "-7", "-7"' in s
    assert '.Xrange "-3.2", "1.6"' in s
    assert '.Coordinates "Ranges"' in s


def test_port_xface():
    """x=const 端口面：Xrange 为常量，Yrange 为范围。"""
    s = V.waveguide_port(1, "p1", "x", -5.6, "positive", -0.6, 4.2, -0.762, 2.0)
    assert '.Xrange "-5.6", "-5.6"' in s
    assert '.Yrange "-0.6", "4.2"' in s
    assert '.Zrange "-0.762", "2"' in s


def test_port_axis_validation():
    import pytest

    with pytest.raises(ValueError):
        V.waveguide_port(1, "p1", "z", 0.0, "positive", 0.0, 1.0, 0.0, 1.0)


def test_excitation_supports_vba_variable():
    s = V.excitation("exc", "portnum")
    assert ".Port portnum" in s  # VBA 变量：不带引号


def test_layout_matches_paper_fig5():
    """几何关系锁死论文 Fig. 5 的参数：w=1.6, d=12, g=1。"""
    import pytest

    from eaopt.solver import template_builder as T

    assert T.W == 1.6 and T.D == 12.0 and T.G == 1.0
    approx = pytest.approx
    assert T.ARM_HI - T.ARM_LO == approx(T.W)          # 耦合臂厚 = w
    assert T.THRU_HI - T.THRU_LO == approx(T.W)        # 直通线宽 = w
    assert T.THRU_LO - T.ARM_HI == approx(T.G)         # 耦合间距 = g
    assert T.LEG_R_IN - T.LEG_L_IN == approx(T.D)      # 两腿内边缘间距 = d（设计区宽）
    assert T.LEG_L_IN - T.LEG_L_OUT == approx(T.W)     # 腿宽 = w
    assert T.LEG_R_OUT - T.LEG_R_IN == approx(T.W)
    assert T.LEG_BOT < T.ARM_LO               # 腿向下延伸到板底
    assert T.THRU_X0 < T.LEG_L_OUT and T.THRU_X1 > T.LEG_R_OUT  # 直通线贯通


def test_config_design_box_agrees_with_template():
    """配置文件里的设计区必须与模板布局一致（否则重建的金属对不上腿）。"""
    from pathlib import Path

    from eaopt.config import CaseConfig
    from eaopt.solver import template_builder as T

    cfg = CaseConfig.from_yaml(Path(__file__).resolve().parents[1]
                               / "configs" / "coupler.yaml")
    box = cfg.design_region.box
    assert list(box.x) == [T.LEG_L_IN, T.LEG_R_IN]   # 设计区宽 = d
    assert box.y[0] < T.ARM_LO                       # 覆盖臂下缘（可向下生长）
    assert box.y[1] > T.THRU_HI                      # 覆盖直通线（作为固定障碍）
    allowed = cfg.constraints.allowed_region
    assert list(allowed.x) == [T.LEG_L_IN, T.LEG_R_IN]
    assert allowed.y[1] == T.THRU_LO                 # 臂不得越过直通线下缘


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
