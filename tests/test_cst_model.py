"""CST 侧模型文本（cst_model）——VBA 命令串 + 模板命令块，纯字符串，无需 CST。

两层都在这里锁：

* **命令片段**（端口/监视器/挤出/ASCIIExport 参数）——版本敏感的写法与
  实测结论（哪些属性不存在、哪些必须成对）都固化在断言里；
* **模板命令块** ``template_blocks`` ——建工程的**单一事实来源**：
  端口、边界、监视器、频段错一个，两个工程就不在同一个模型上，伴随法
  直接失效（而且不会报错，只出错数据）。
"""

import math
from pathlib import Path

import pytest

from eaopt.config import CaseConfig
from eaopt.solver import cst_model as M

REPO = Path(__file__).resolve().parents[1]


# =========================================================================== #
# 一、VBA 命令片段
# =========================================================================== #
def test_polygon_extrude_contains_commands():
    s = M.polygon_extrude("arm", "design_region", "PEC",
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
    s = M.brick("sub", "c1", "Rogers4350B", -4.0, 16.0, -6.0, 6.0, -0.762, 0.0)
    assert '.Xrange "-4", "16"' in s
    assert '.Zrange "-0.762", "0"' in s


def test_port_yface():
    """ymin 面端口：Yrange 为常量，Xrange 为范围。"""
    s = M.waveguide_port(3, "p3", "ymin", -7.0, -3.2, 1.6, -0.762, 2.0)
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
    s = M.waveguide_port(1, "p1", "xmin", -5.6, -0.6, 4.2, -0.762, 2.0)
    assert '.Orientation "xmin"' in s
    assert '.Xrange "-5.6", "-5.6"' in s
    assert '.Yrange "-0.6", "4.2"' in s
    assert '.Zrange "-0.762", "2"' in s
    assert '.Coordinates "Free"' in s
    assert '.Orientation "xmax"' in M.waveguide_port(
        2, "p2", "xmax", 17.6, -0.6, 4.2, -0.762, 2.0)


def test_port_face_validation():
    """只接受 CST 的边界面名（不认 positive/negative 或轴名）。"""
    for bad in ("z", "zmin", "positive", "x", "top"):
        with pytest.raises(ValueError):
            M.waveguide_port(1, "p1", bad, 0.0, 0.0, 1.0, 0.0, 1.0)


def test_solver_stimulation_port_selects_single_port():
    """"只激励哪个端口"用 Solver.StimulationPort（不用 Excitation 对象：
    后者在 CST 2024 命令宏里报 10090，且失败静默——S 参数照样对，但场
    监视器存的是多激励叠加的场）。端口与模式必须**成对**：CST 2024 实测
    "1" + "All" 会让 Solver.Start 报 "Invalid stimulation port,
    please specify."；Dassault 教程的成对写法是 "1" + "1"。"""
    s = M.time_domain_solver_setup("1")
    assert '.StimulationPort "1"' in s
    assert '.StimulationMode "1"' in s
    assert '.Method "Hexahedral"' in s and '.CalculationType "TD-S"' in s
    all_ports = M.time_domain_solver_setup()
    assert all_ports.count('.StimulationPort "All"') == 1
    assert all_ports.count('.StimulationMode "All"') == 1
    assert '.StimulationMode "2"' in M.time_domain_solver_setup("1", "2")


def test_ascii_export_uses_only_existing_cst_properties():
    """CST 2024 实测：ASCIIExport 没有 XStart/XEnd/YStart/YEnd/ZStart/ZEnd
    （报 <unknown>.XStart）。可用属性只有 Reset / FileName / Mode /
    StepX / StepY / StepZ / Execute——导出范围是选中结果的整个包围盒，
    要限制范围只能在解析端裁剪（cst_results.crop_grid）。

    模式必须是 **FixedWidth**（步长 mm），不能是 FixedNumber（采样点数）：
    官方例程（Dassault《Scripting the CST Studio Suite with the Python》）
    用 FixedWidth + StepX/Y/Z；服务器 2026-10-04 实测的导出文件是"表头 +
    每点一行 9 列 ``x y z Re1 Im1 Re2 Im2 Re3 Im3``"——也就是
    cst_results.parse_ascii_field 解析的那一种。写成 FixedNumber 却填 mm
    步长会语义矛盾：同样的 "0.2" 在大包围盒上点数暴涨，解析端拿到的网格
    也不是我们以为的那个。
    """
    assert M.ASCII_EXPORT_MODE == "FixedWidth"
    params = M.ascii_export_params(0.2)
    assert params == [("Mode", "FixedWidth"), ("StepX", "0.2"),
                      ("StepY", "0.2"), ("StepZ", "0.2")]
    assert M.ASCII_EXPORT_EXECUTE == "Execute"      # 执行方法名（含 Reset 在
    # cst_results 里最先调用）
    assert ("Mode", "FixedNumber") in M.ascii_export_params(0.2, mode="FixedNumber")


def test_design_region_update_is_one_record_with_delete_first():
    """每轮形状更新 = **一条**历史记录：先删整个组件，再逐个挤出。

    删组件必须排在最前，且要容错（组件不存在时首轮不该失败）；
    挤出多边形的名字/组件名必须一致，否则会新建组件而把旧金属留在模型里。
    """
    polys = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)],
             [(2.0, 0.0), (3.0, 0.0), (3.0, 1.0)]]
    cmd = M.design_region_update(polys, 0.035)
    assert cmd.index("Component.Delete") < cmd.index("With Extrude")
    assert 'Component.Delete "design_region"' in cmd
    # 容错包夹成对出现，不会把错误处理状态泄漏给后面的挤出
    assert cmd.count("On Error Resume Next") == cmd.count("On Error GoTo 0") == 1
    assert cmd.count("With Extrude") == len(polys)
    assert cmd.count('.Component "design_region"') == len(polys)
    assert cmd.count('.Material "PEC"') == len(polys)
    assert '.Height "0.035"' in cmd
    for i in range(len(polys)):
        assert f'.Name "design_{i}"' in cmd
    # 多边形顶点确实写进去了
    assert '.Point "0", "0"' in cmd and '.LineTo "1", "1"' in cmd


def test_design_region_update_rejects_empty_or_degenerate():
    """空多边形表会把设计区金属全删掉——必须当场拒绝，而不是静默产出空模型。"""
    with pytest.raises(ValueError, match="polys 为空"):
        M.design_region_update([], 0.035)
    with pytest.raises(ValueError, match="只有 2 个点"):
        M.design_region_update([[(0.0, 0.0), (1.0, 1.0)]], 0.035)


def test_frequency_range_is_set_explicitly():
    """频段写死在宏里：两个模板必须一致（它决定自适应网格与脉冲带宽）。"""
    assert M.frequency_range(0.0, 10.0) == 'Solver.FrequencyRange "0", "10"\n'


def test_field_monitor_is_volume_with_cst_conventional_name():
    """监视器：Volume（覆盖整个计算域，无位置参数），名字取 CST 惯例。"""
    s = M.field_monitor("Efield", 5.0)
    assert '.Name "e-field (f=5)"' in s
    assert '.Dimension "Volume"' in s
    assert '.Domain "Frequency"' in s
    assert '.FieldType "Efield"' in s
    assert '.MonitorValue "5"' in s          # CST 惯例写法，不是 "5.0"
    # 子域显式关闭（录制宏同），但**不写**子域范围那几条（惰性属性）
    assert '.UseSubvolume "False"' in s
    assert ".SetSubvolume" not in s
    assert M.field_monitor("Hfield", 5.0).count('.Name "h-field (f=5)"') == 1
    for bad in ("e", "E", "efield", "E_field"):
        with pytest.raises(ValueError):
            M.field_monitor(bad, 5.0)


def test_monitor_name_and_result_path_agree():
    """创建监视器用的名字必须与导出场时认领条目用的叶子名同源。

    CST 用监视器名命名结果树条目（再往后缀，如 "<name> [AC]"），导出场时
    到结果树上按这个叶子名认领（``cst_results.resolve_field_item``）——
    两处一旦漂移，场导出就会"树上找不到条目"。
    """
    assert M.field_monitor_name("Efield", 5.0) == "e-field (f=5)"
    assert M.field_result_path("Efield", 5.0) == \
        "2D/3D Results\\E-Field\\e-field (f=5) [AC]"
    assert M.field_result_path("Hfield", 5.0) == \
        "2D/3D Results\\H-Field\\h-field (f=5) [AC]"
    name = M.field_monitor_name("Efield", 5.0)
    assert name in M.field_monitor("Efield", 5.0)
    assert name in M.field_result_path("Efield", 5.0)
    with pytest.raises(ValueError):
        M.field_result_path("efield", 5.0)


# =========================================================================== #
# 二、模板命令块
# =========================================================================== #
def _text(portnum: int) -> str:
    """把该模板的全部命令块拼成一份可搜索的文本。"""
    return "\n".join(cmd for _, cmd in M.template_blocks("coupler", portnum))


def _code(text: str) -> str:
    """去掉注释行（注释里会提到 NewProject/Excitation 以说明为何不用）。"""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("'"))


def test_block_headers_are_readable():
    """历史表标题从命令文本推出来（Brick substrate / Extrude leg_left …）。"""
    blocks = dict(M.template_blocks("coupler_fwd", 1))
    assert M.block_header(M.UNITS_BLOCK) == "Units"
    assert set(blocks) >= {"Units", "Brick substrate", "Extrude arm_init",
                           "Extrude leg_left", "Port p1"}
    assert blocks["Units"] == M.UNITS_BLOCK
    assert '    .Name "substrate"' in blocks["Brick substrate"]


def test_every_project_gets_the_same_model_but_its_own_stimulus():
    """双模板的硬不变量：几何/端口/监视器/边界完全一致，只差激励端口。"""
    fwd, bwd = _text(1), _text(3)
    assert fwd.replace('.StimulationPort "1"', ".StimulationPort X") == \
        bwd.replace('.StimulationPort "3"', ".StimulationPort X")
    assert '.StimulationPort "1"' in fwd and '.StimulationPort "3"' not in fwd
    assert '.StimulationPort "3"' in bwd and '.StimulationPort "1"' not in bwd
    # 端口与模式成对（实测 "x" + "All" 会被 Solver.Start 拒绝）
    assert '.StimulationMode "1"' in fwd


def test_blocks_contain_the_full_model():
    """几何 + 端口 + 边界 + 求解器设置，一个都不能少。"""
    text = _text(1)
    # 几何：基板 + 接地 + 空气盒 + 3 个挤出（左右腿 + 设计区初始臂）
    assert "Rogers4350B" in text
    assert text.count("With Extrude") == 3
    assert "With Polygon" not in text          # 不需要单独的曲线对象
    assert '    .Name "arm_init"' in text
    assert '    .Component "design_region"' in text
    # 4 个端口，全部 Free 坐标系；端口面名与几何一致
    assert text.count("With Port") == 4
    assert '.Coordinates "Free"' in text and "Ranges" not in text
    assert text.count('.Orientation "xmin"') == 1   # 端口 1
    assert text.count('.Orientation "xmax"') == 1   # 端口 2
    assert text.count('.Orientation "ymin"') == 2   # 端口 3/4
    # 端口面：下缘贴合接地板底面（域 zmin），上缘到空气盒顶（域 zmax）
    assert text.count('.Zrange "-0.797", "4"') == 4
    # 边界：zmax 电边界、其余磁边界
    assert '.Zmax "electric"' in text
    assert text.count('"magnetic"') == 5
    # 求解器/频段/监视器：时域求解器跑宽带，5 GHz 处各存一份 E/H 场
    # （伴随法只用 5 GHz 那两张监视器结果）
    assert text.count("With Monitor") == 2
    assert text.count('.MonitorValue "5"') == 2
    assert 'Solver.FrequencyRange "0", "10"' in text
    assert "Mesh.SetCreator" in text


def test_blocks_do_not_use_the_excitation_object():
    """激励走 ``StimulationPort``（端口自带），**不用 Excitation 对象**：
    CST 2024 实测它报 "(10090)"，且失败是静默的——S 参数照样对，但监视器
    存的是多个激励叠加的场，会悄悄毁掉伴随梯度。"""
    code = _code(_text(1))
    assert "With Excitation" not in code
    assert "Excitation.Reset" not in code


def test_blocks_hold_no_project_level_instructions():
    """NewProject/SaveAs 是工程级指令：由程序自己负责（新建/存盘），
    命令块里出现它们会被 CST 拒绝或把历史表搞乱。"""
    code = _code(_text(1))
    assert "NewProject" not in code
    assert "SaveAs" not in code


def test_command_text_is_ascii():
    """VBA 命令文本按 ANSI 执行：可执行语句必须全 ASCII（非 ASCII 字节
    可能吞掉引号 → 语法错误）。"""
    _text(1).encode("ascii")


# =========================================================================== #
# 三、布局常量（论文 Fig. 5）
# =========================================================================== #
def test_layout_matches_paper_fig5():
    """几何关系锁死论文 Fig. 5 的参数：w=1.6, d=12, g=1。"""
    assert M.W == 1.6 and M.D == 12.0 and M.G == 1.0
    approx = pytest.approx
    assert M.ARM_HI - M.ARM_LO == approx(M.W)          # 耦合臂厚 = w
    assert M.THRU_HI - M.THRU_LO == approx(M.W)        # 直通线宽 = w
    assert M.THRU_LO - M.ARM_HI == approx(M.G)         # 耦合间距 = g
    assert M.LEG_R_IN - M.LEG_L_IN == approx(M.D)      # 两腿内边缘间距 = d（设计区宽）
    assert M.LEG_L_IN - M.LEG_L_OUT == approx(M.W)     # 腿宽 = w
    assert M.LEG_R_OUT - M.LEG_R_IN == approx(M.W)
    assert M.LEG_BOT < M.ARM_LO               # 腿向下延伸到板底
    assert M.THRU_X0 < M.LEG_L_OUT and M.THRU_X1 > M.LEG_R_OUT  # 直通线贯通


def test_fillet_geometry_is_concentric_strip_bend():
    """拐弯过渡：等宽条带圆角（外 R_OUT / 内 R_IN 同心，R_OUT-R_IN=W）。"""
    assert M.R_OUT - M.R_IN == pytest.approx(M.W)
    assert M.R_OUT + M.R_IN == pytest.approx(2 * M.R_BEND)

    cx, cy = M.LEG_L_IN + M.R_IN, M.ARM_LO - M.R_IN      # 左拐弯中心
    leg = M._left_leg_polygon()
    arm = M._arm_design_polygon()

    # 腿外缘圆弧：所有弧点到拐弯中心 = R_OUT
    for x, y in leg[2:-1]:
        assert math.hypot(x - cx, y - cy) == pytest.approx(M.R_OUT, abs=1e-3)
    # 外弧与外缘相切：起点在腿外缘 x 上、切点 y = 拐弯中心 y
    assert leg[1] == pytest.approx((M.LEG_L_OUT, cy))
    # 臂内缘圆弧：所有弧点到同一中心 = R_IN（同心）
    for x, y in arm[1:7]:
        assert math.hypot(x - cx, y - cy) == pytest.approx(M.R_IN, abs=1e-3)
    # 内弧切点落在设计区边角：(0,-2) 与 (0.4,-1.6)
    assert arm[0] == pytest.approx((M.LEG_L_IN, cy))
    assert arm[6] == pytest.approx((cx, M.ARM_LO))
    # 臂轮廓范围 = 设计区（x∈[0,12]、y∈[-2,0]）
    xs = [p[0] for p in arm]
    ys = [p[1] for p in arm]
    assert (min(xs), max(xs)) == pytest.approx((M.LEG_L_IN, M.LEG_R_IN))
    assert (min(ys), max(ys)) == pytest.approx((cy, M.ARM_HI))


def test_config_initial_metal_matches_template():
    """配置里的初始金属必须与模板 arm_init 是同一轮廓（否则 0 次迭代
    时 CST 里的结构与水准集表示不一致）。"""
    cfg = CaseConfig.from_yaml(REPO / "configs" / "coupler.yaml")
    yaml_pts = list(cfg.initial_metal[0].vertices)
    code_pts = M._arm_design_polygon()
    assert len(yaml_pts) == len(code_pts)          # YAML 中坐标保留 4 位小数
    for (yx, yy), (cx, cy) in zip(yaml_pts, code_pts):
        assert (yx, yy) == pytest.approx((cx, cy), abs=1e-3)


def test_config_design_box_agrees_with_template():
    """配置文件里的设计区必须与模板布局一致（否则重建的金属对不上腿）。

    设计区（= 论文 Fig.5 红框）= 可动金属的活动范围，一个域：x 由两条腿的
    内边缘定，上界 = 直通线下边缘 − 最小间距（论文"臂上边缘不得超过直通线，
    最小间距 0.1 mm"），下界留出向下生长的余量。
    """
    cfg = CaseConfig.from_yaml(REPO / "configs" / "coupler.yaml")
    box = cfg.design_region.box
    assert list(box.x) == [M.LEG_L_IN, M.LEG_R_IN]   # 设计区宽 = d
    assert box.y[1] == M.THRU_LO - cfg.constraints.min_gap_mm  # 论文约束
    assert box.y[1] > M.ARM_HI                       # 上界在臂顶之上（臂能上长）
    assert box.y[1] < M.THRU_LO                      # 且不越过直通线
    assert box.y[0] < min(M._arm_design_polygon(), key=lambda p: p[1])[1]
    assert cfg.constraints.allowed_region is None     # 两域合一
    assert cfg.constraints.taper_edges == "x"         # 只在腿那两条边 taper
