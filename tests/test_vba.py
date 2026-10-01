"""VBA 命令字符串与模板宏生成测试（纯字符串，无需 CST）。"""

import numpy as np
import pytest

from eaopt.solver import vba as V
from eaopt.solver.template_builder import (build_all_templates, build_macro,
                                           build_polygon_test_macro)


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
    """ymin 面端口：Yrange 为常量，Xrange 为范围。"""
    s = V.waveguide_port(3, "p3", "ymin", -7.0, -3.2, 1.6, -0.762, 2.0)
    assert '.PortNumber "3"' in s
    assert '.Orientation "ymin"' in s
    assert '.Yrange "-7", "-7"' in s
    assert '.Xrange "-3.2", "1.6"' in s
    # CST 2024 实测 .Coordinates 只认 Free/Full/Picks（"Ranges" 报
    # "Invalid coordinate type"）；微带类端口用 Free + 显式范围
    assert '.Coordinates "Free"' in s
    assert "Ranges" not in s
    assert '.PortOnBound "True"' in s


def test_port_xface():
    """xmin/xmax 面端口：Xrange 为常量，Yrange 为范围。"""
    s = V.waveguide_port(1, "p1", "xmin", -5.6, -0.6, 4.2, -0.762, 2.0)
    assert '.Orientation "xmin"' in s
    assert '.Xrange "-5.6", "-5.6"' in s
    assert '.Yrange "-0.6", "4.2"' in s
    assert '.Zrange "-0.762", "2"' in s
    assert '.Coordinates "Free"' in s
    assert '.Orientation "xmax"' in V.waveguide_port(
        2, "p2", "xmax", 17.6, -0.6, 4.2, -0.762, 2.0)


def test_port_face_validation():
    """只接受 CST 的边界面名（不认 positive/negative 或轴名）。"""
    for bad in ("z", "zmin", "positive", "x", "top"):
        with pytest.raises(ValueError):
            V.waveguide_port(1, "p1", bad, 0.0, 0.0, 1.0, 0.0, 1.0)


def test_solver_stimulation_port_selects_single_port():
    """"只激励哪个端口"用 Solver.StimulationPort（不用 Excitation 对象：
    后者在 CST 2024 命令宏里报 10090，且失败静默——S 参数照样对，但场
    监视器存的是多激励叠加的场）。端口与模式必须**成对**：CST 2024 实测
    "1" + "All" 会让 Solver.Start 报 "Invalid stimulation port,
    please specify."；Dassault 教程的成对写法是 "1" + "1"。"""
    s = V.time_domain_solver_setup("1")
    assert '.StimulationPort "1"' in s
    assert '.StimulationMode "1"' in s
    assert '.Method "Hexahedral"' in s and '.CalculationType "TD-S"' in s
    all_ports = V.time_domain_solver_setup()
    assert all_ports.count('.StimulationPort "All"') == 1
    assert all_ports.count('.StimulationMode "All"') == 1
    assert '.StimulationMode "2"' in V.time_domain_solver_setup("1", "2")


def test_ascii_export_uses_only_existing_cst_properties():
    """CST 2024 实测：ASCIIExport 没有 XStart/XEnd/YStart/YEnd/ZStart/ZEnd
    （报 <unknown>.XStart）。可用属性只有 Reset / FileName / Mode /
    StepX / StepY / StepZ / Execute——导出范围是选中结果的整个包围盒，
    要限制范围只能在解析端裁剪。"""
    params = V.ascii_export_params(0.2)
    assert params == [("Mode", "FixedNumber"), ("StepX", "0.2"),
                      ("StepY", "0.2"), ("StepZ", "0.2")]
    s = V.ascii_export_field("f.txt", 0.2)
    assert '.FileName "f.txt"' in s
    for prop, val in params:
        assert f'.{prop} "{val}"' in s
    assert ".Execute" in s
    for bad in ("XStart", "XEnd", "YStart", "YEnd", "ZStart", "ZEnd",
                ".Export"):
        assert bad not in s
    # 顺序：Reset → FileName → 参数（Reset 必须最先，清掉上一次的残留）
    assert s.index(".Reset") < s.index(".FileName") < s.index(".Mode")
    # 模式可覆盖（smoke 用 FixedWidth 候选兜底）
    assert ("Mode", "FixedWidth") in V.ascii_export_params(0.2, mode="FixedWidth")


def test_frequency_range_is_set_explicitly():
    """频段写死在宏里：两个模板必须一致（它决定自适应网格与脉冲带宽）。"""
    assert V.frequency_range(0.0, 10.0) == 'Solver.FrequencyRange "0", "10"\n'


def test_guarded_wraps_block_and_reports():
    """guarded()：块内错误记入 errLog 而不抛出（宏继续跑完）。"""
    s = V.guarded(V.set_boundaries(*["magnetic"] * 5, "electric"),
                  "Boundary", indent="")
    assert s.startswith("Err.Clear\nWith Boundary")
    assert "If Err.Number <> 0 Then" in s
    assert 'errLog = errLog & "Boundary: (" & Err.Number & ") "' in s
    assert s.rstrip().endswith("End If")
    # 缩进：块内每行都缩进，便于阅读生成的宏
    ind = V.guarded(V.set_boundaries(*["magnetic"] * 5, "electric"), "Boundary")
    assert "\n    With Boundary" in ind and "\n        .Xmin" in ind


def test_field_monitor_is_volume_with_cst_conventional_name():
    """监视器：Volume（覆盖整个计算域，无位置参数），名字取 CST 惯例。"""
    s = V.field_monitor("Efield", 5.0)
    assert '.Name "e-field (f=5)"' in s
    assert '.Dimension "Volume"' in s
    assert '.Domain "Frequency"' in s
    assert '.FieldType "Efield"' in s
    assert '.MonitorValue "5"' in s          # CST 惯例写法，不是 "5.0"
    # 子域显式关闭（录制宏同），但**不写**子域范围那几条（惰性属性）
    assert '.UseSubvolume "False"' in s
    assert ".SetSubvolume" not in s
    assert V.field_monitor("Hfield", 5.0).count('.Name "h-field (f=5)"') == 1
    for bad in ("e", "E", "efield", "E_field"):
        with pytest.raises(ValueError):
            V.field_monitor(bad, 5.0)


def test_monitor_name_and_result_path_agree():
    """创建监视器用的名字必须与导出场时选中的结果树条目同源。

    CST 用监视器名命名结果树条目（"<name> [AC]"）——两处一旦漂移，
    CstSolver 导出场就会 SelectTreeItem 失败。
    """
    assert V.field_monitor_name("Efield", 5.0) == "e-field (f=5)"
    assert V.field_result_path("Efield", 5.0) == \
        "2D/3D Results\\E-Field\\e-field (f=5) [AC]"
    assert V.field_result_path("Hfield", 5.0) == \
        "2D/3D Results\\H-Field\\h-field (f=5) [AC]"
    name = V.field_monitor_name("Efield", 5.0)
    assert name in V.field_monitor("Efield", 5.0)
    assert name in V.field_result_path("Efield", 5.0)
    with pytest.raises(ValueError):
        V.field_result_path("efield", 5.0)


def test_select_field_monitor_vba_has_single_backslashes():
    """VBA 字符串不转义反斜杠：路径原样写入（早期版本多写一层会选不中）。"""
    s = V.select_field_monitor("Efield", 5.0)
    assert s == ('SelectTreeItem("2D/3D Results\\E-Field\\'
                 'e-field (f=5) [AC]")\n')
    assert V.field_result_path("Efield", 5.0) in s


def test_guarded_rejects_non_ascii_label():
    """label 进字符串字面量：必须 ASCII（CST 宏按 ANSI 解码）。"""
    for bad in ("激励", "Monitör"):
        with pytest.raises(ValueError):
            V.guarded("", bad)
        with pytest.raises(ValueError):
            V.guarded_alternatives(["Cmd"], bad)


def test_guarded_alternatives_nests_and_reports():
    """多种写法嵌套尝试：第一个不报错的即止，全失败才记 errLog。"""
    s = V.guarded_alternatives(['SaveAs "p", "False"', 'SaveAs "p", "True"'],
                               "SaveAs")
    assert s.startswith('    Err.Clear\n    SaveAs "p", "False"\n'
                        '    If Err.Number <> 0 Then\n        Err.Clear\n'
                        '        SaveAs "p", "True"\n'
                        '        If Err.Number <> 0 Then\n')
    assert '            errLog = errLog & "SaveAs: (" & Err.Number & ") "' in s
    assert s.rstrip().endswith("        End If\n    End If")   # 2 种写法 → 2 个 If
    assert s.count("Err.Clear") == 4      # 进入前清 1 + 每种写法失败后清 1
    with pytest.raises(ValueError):
        V.guarded_alternatives([], "SaveAs")


def test_layout_matches_paper_fig5():
    """几何关系锁死论文 Fig. 5 的参数：w=1.6, d=12, g=1。"""
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


def test_fillet_geometry_is_concentric_strip_bend():
    """拐弯过渡：等宽条带圆角（外 R_OUT / 内 R_IN 同心，R_OUT-R_IN=W）。"""
    import math

    from eaopt.solver import template_builder as T

    assert T.R_OUT - T.R_IN == pytest.approx(T.W)
    assert T.R_OUT + T.R_IN == pytest.approx(2 * T.R_BEND)

    cx, cy = T.LEG_L_IN + T.R_IN, T.ARM_LO - T.R_IN      # 左拐弯中心
    leg = T._left_leg_polygon()
    arm = T._arm_design_polygon()

    # 腿外缘圆弧：所有弧点到拐弯中心 = R_OUT
    for x, y in leg[2:-1]:
        assert math.hypot(x - cx, y - cy) == pytest.approx(T.R_OUT, abs=1e-3)
    # 外弧与外缘相切：起点在腿外缘 x 上、切点 y = 拐弯中心 y
    assert leg[1] == pytest.approx((T.LEG_L_OUT, cy))
    # 臂内缘圆弧：所有弧点到同一中心 = R_IN（同心）
    for x, y in arm[1:7]:
        assert math.hypot(x - cx, y - cy) == pytest.approx(T.R_IN, abs=1e-3)
    # 内弧切点落在设计区边角：(0,-2) 与 (0.4,-1.6)
    assert arm[0] == pytest.approx((T.LEG_L_IN, cy))
    assert arm[6] == pytest.approx((cx, T.ARM_LO))
    # 臂轮廓范围 = 设计区（x∈[0,12]、y∈[-2,0]）
    xs = [p[0] for p in arm]
    ys = [p[1] for p in arm]
    assert (min(xs), max(xs)) == pytest.approx((T.LEG_L_IN, T.LEG_R_IN))
    assert (min(ys), max(ys)) == pytest.approx((cy, T.ARM_HI))


def test_config_initial_metal_matches_template():
    """配置里的初始金属必须与模板 arm_init 是同一轮廓（否则 0 次迭代
    时 CST 里的结构与水准集表示不一致）。"""
    from pathlib import Path

    from eaopt.config import CaseConfig
    from eaopt.solver import template_builder as T

    cfg = CaseConfig.from_yaml(Path(__file__).resolve().parents[1]
                               / "configs" / "coupler.yaml")
    yaml_pts = list(cfg.initial_metal[0].vertices)
    code_pts = T._arm_design_polygon()
    assert len(yaml_pts) == len(code_pts)          # YAML 中坐标保留 4 位小数
    for (yx, yy), (cx, cy) in zip(yaml_pts, code_pts):
        assert (yx, yy) == pytest.approx((cx, cy), abs=1e-3)


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


def _model_macros(paths):
    """从 build_all_templates 的产物里挑出建模宏（.mcs）。"""
    return [p for p in paths if p.suffix == ".mcs"]


def test_build_macro_each_template_is_self_contained(tmp_path):
    """每个模板一个**结构宏**（.mcs）：全字面量、无工程级指令、建模齐全。"""
    paths = build_all_templates(tmp_path)
    runs = _model_macros(paths)
    assert [p.name for p in runs] == ["build_coupler_fwd.mcs",
                                      "build_coupler_bwd.mcs"]
    for path, project, port in zip(runs, ("coupler_fwd", "coupler_bwd"), (1, 3)):
        assert path.read_bytes().count(b"\r\n") > 10  # Windows VBA 换行
        text = path.read_text(encoding="utf-8")
        assert text.startswith("'#Language \"WWB-COM\"")
        assert "Sub Main()" in text and "End Sub" in text
        # 只看可执行语句（注释里会提到 NewProject/SaveAs 以说明为何不用）
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("'"))
        assert "NewProject" not in code  # 实测非法：新建工程由 GUI 完成
        assert "SaveAs" not in code      # 工程级指令 → 交给 save_*.mcr
        assert "portnum As Integer" not in text  # 参数全部字面量
        # 只激励本模板的端口，且端口与模式成对（实测 "x"+"All" 会被
        # Solver.Start 拒绝）
        assert f'.StimulationPort "{port}"' in text
        assert '.StimulationMode "1"' in text
        assert "Rogers4350B" in text
        # 4 个端口，全部 Free 坐标系；端口面名与几何一致
        assert text.count("With Port") == 4
        assert ".Coordinates \"Free\"" in text and "Ranges" not in text
        assert text.count('.Orientation "xmin"') == 1   # 端口 1
        assert text.count('.Orientation "xmax"') == 1   # 端口 2
        assert text.count('.Orientation "ymin"') == 2   # 端口 3/4
        # 端口面：下缘贴合接地板底面（域 zmin），上缘到空气盒顶（域 zmax）
        assert text.count('.Zrange "-0.797", "4"') == 4
        # 3 个挤出对象：左腿 / 右腿 / 设计区初始臂（带圆角过渡）
        assert text.count("With Extrude") == 3
        assert "With Polygon" not in text          # 不需要单独的曲线对象
        assert '    .Name "arm_init"' in text
        assert '    .Component "design_region"' in text
        # 边界：zmax 电边界、其余磁边界
        assert '.Zmax "electric"' in text
        assert text.count('"magnetic"') == 5
    # 激励端口互不相同（fwd=1 / bwd=3），且不使用 Excitation 对象
    fwd, bwd = (p.read_text(encoding="utf-8") for p in runs)
    assert '.StimulationPort "1"' in fwd and '.StimulationPort "3"' not in fwd
    assert '.StimulationPort "3"' in bwd and '.StimulationPort "1"' not in bwd
    # 只看可执行语句（注释里会提到 Excitation 以说明为何不用它）
    code_only = lambda t: "\n".join(l for l in t.splitlines()          # noqa: E731
                                    if not l.lstrip().startswith("'"))
    # 不用 Excitation **对象**（"SuperimposePLWExcitation" 只是个布尔属性，
    # 名字里含 Excitation，不能拿子串一刀切）
    assert "With Excitation" not in code_only(fwd)
    assert "Excitation.Reset" not in code_only(fwd)
    assert "With Excitation" not in code_only(bwd)
    assert 'Solver.FrequencyRange "0", "10"' in fwd


def test_save_macros_are_control_macros(tmp_path):
    """另存必须是**控制宏**（.mcr）：工程级指令只在控制宏上下文合法。"""
    paths = build_all_templates(tmp_path)
    saves = [p for p in paths if p.suffix == ".mcr"]
    assert [p.name for p in saves] == ["save_coupler_fwd.mcr",
                                       "save_coupler_bwd.mcr"]
    for path, project in zip(saves, ("coupler_fwd", "coupler_bwd")):
        text = path.read_text(encoding="utf-8")
        assert 'SaveAs "' in text and f"{project}.cst" in text
        # SaveAs 必须带第二个参数（实测只给路径报 10097），两种写法都试
        assert '.cst", "False"' in text and '.cst", "True"' in text
        # 不改模型：没有建模指令
        assert "With Brick" not in text and "With Port" not in text
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("'"))
        assert "NewProject" not in code


def test_templates_differ_only_in_stimulation_and_save_path(tmp_path):
    """双模板设计的硬不变量：两个工程必须是同一套几何/端口/监视器/边界，
    只差"激励哪个端口"和另存路径。否则伴随法的正/反向场不在同一个模型上。"""
    paths = _model_macros(build_all_templates(tmp_path))
    texts = [p.read_text(encoding="utf-8") for p in paths]

    def strip(text):
        out = text.replace('.StimulationPort "1"', '.StimulationPort "X"')
        out = out.replace('.StimulationPort "3"', '.StimulationPort "X"')
        # 只差激励端口与结尾报告框里的工程名
        return [l for l in out.splitlines()
                if "MsgBox" not in l and not l.lstrip().startswith("'")]
    assert strip(texts[0]) == strip(texts[1])


def test_settings_blocks_are_error_guarded(tmp_path):
    """设置类块（激励/频段/监视器/边界/求解器）逐块容错：设置块报错
    （CST 2024 实测过 10090/10097）时只记入结尾报告，不中止宏。几何与
    端口则必须失败即中止。"""
    text = build_macro(tmp_path, "demo", 1).read_text(encoding="utf-8")
    guard_at = text.index("On Error Resume Next")
    release_at = text.index("On Error GoTo 0")
    # 保护区之外：几何与端口（失败就该中止，不吞错）
    assert guard_at > text.rindex("With Extrude")
    assert guard_at > text.rindex("With Port")
    # 保护区之内：6 个设置块（Mesh/Solver/频段/两个监视器/边界；
    # 每块 2 次 Err 操作）
    assert text.count("If Err.Number <> 0 Then") == 6
    assert text.count("Err.Clear") == 12
    for label in ("Mesh", "Solver", "FrequencyRange", "Monitor Efield",
                  "Monitor Hfield", "Boundary"):
        assert f'errLog = errLog & "{label}: ("' in text
    assert "Dim errLog As String" in text
    # 顺序：各设置块 → 保护结束 → 报告框
    assert guard_at < release_at < text.index("MsgBox")


def test_save_macro_reports_failure_instead_of_aborting(tmp_path):
    """另存失败（实测 10097 参数个数）要写进报告框，而不是抛出去。"""
    from eaopt.solver.template_builder import build_save_macro

    text = build_save_macro(tmp_path, "demo").read_text(encoding="utf-8")
    assert text.count("On Error Resume Next") == 1
    assert text.count("If Err.Number <> 0 Then") == 2      # 两种写法
    assert 'errLog = errLog & "SaveAs: ("' in text
    assert "MsgBox" in text and "SaveAs FAILED" in text


def test_macro_code_is_ascii_comments_may_be_chinese(tmp_path):
    """CST 宏按 ANSI 解码：可执行语句必须全 ASCII（字符串字面量里的
    非 ASCII 字节可能吞掉引号 → 语法错误）；注释里的中文不影响解析。"""
    if not str(tmp_path).isascii():
        pytest.skip("输出目录非 ASCII：SaveAs 路径本身就会含非 ASCII")
    text = build_macro(tmp_path, "demo", 1).read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("'"))
    code.encode("ascii")  # 非 ASCII 抛 UnicodeEncodeError


def test_build_macro_single(tmp_path):
    path = build_macro(tmp_path, "demo", 2)
    assert path.name == "build_demo.mcs"
    assert '.StimulationPort "2"' in path.read_text(encoding="utf-8")


def test_model_macro_is_structure_macro_not_control(tmp_path):
    """建模宏必须是 .mcs：.mcr（控制宏）的动作不进 History List，
    存盘重开就是空工程（服务器实测）。"""
    from eaopt.solver.template_builder import build_save_macro

    assert build_macro(tmp_path, "demo", 1).suffix == ".mcs"
    assert build_save_macro(tmp_path, "demo").suffix == ".mcr"
    text = build_macro(tmp_path, "demo", 1).read_text(encoding="utf-8")
    # 文档/注释里说明为什么必须是结构宏、以及必须从 Macros 菜单运行
    assert "History List" in text
    assert "Macros" in text


def test_polygon_test_macro_has_two_extrudes(tmp_path):
    """诊断宏：L 形（非凸 6 点）+ 方形（4 点），用于核对直边。"""
    from eaopt.solver import template_builder as T

    path = build_polygon_test_macro(tmp_path)
    assert path.name == "polygon_test.mcs"    # 建模宏 → 结构宏
    text = path.read_text(encoding="utf-8")
    assert text.count("With Extrude") == 2
    assert text.count('.Mode "Pointlist"') == 2
    assert "Sub Main()" in text and "End Sub" in text
    # L 形的内角点必须在点列里（非凸轮廓）
    assert '.Point "0", "0"' in text and '.LineTo "1", "1"' in text
