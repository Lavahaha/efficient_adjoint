"""CST 双模板宏（.mcr 命令宏）生成（本地可单测，无需 CST）。

命名与类型的两点约束（均为 CST 2024 实测/文档结论）：
  1. Import Macro 对话框只列 "CST Macro Files (*.mcs; *.mcr)"，.bas 不可见；
  2. CST 宏分两类——命令宏（.mcr，控制类指令）/ 结构宏（.mcs，建模类指令，
     动作进 History List）。**工程级指令（新建/打开/另存工程）只在命令宏
     上下文合法**：在 .mcs 里执行 NewProject 实测报 "Invalid instruction"。
     故本宏不含 NewProject，只做"建模 + SaveAs"，新建工程由用户在 GUI 中
     File → New 完成；且所有参数写字面量，不依赖 VBA 变量传参。
  3. **SaveAs 必须带两个参数**：命令宏里 `SaveAs "<路径>"` 实测报
     "(10097) ActiveX Automation: wrong number of parameters"，需再给一个
     布尔（见 vba.guarded_alternatives 调用处）。
  4. 设置类块（激励/监视器/边界/求解器/另存）逐个容错，失败只记入
     结尾的报告框——CST 2024 实测 `Excitation.Reset` 报 "(10090)"，
     未加保护会让整个宏中止。

用法（每个模板一次，共两次）：
  CST → File → New（模板 <None>）→ 导入并运行 build_coupler_fwd.mcr
  → 生成 <输出目录>/coupler_fwd.cst；再 File → New → 运行
  build_coupler_bwd.mcr → coupler_bwd.cst。
另有诊断宏 polygon_test.mcr（可选，见 build_polygon_test_macro）。

几何布局（单位 mm，z=0 为基板顶面；按论文 Fig. 5 与 w/d/g 参数）：
    直通线（固定，端口 1-2）：y∈[1.0,2.6]（w=1.6），x 贯通整块板
    耦合臂（"⊓"形）：横段 y∈[−1.6,0]（w=1.6）位于两腿之间，
        两端各一条腿 x∈[−1.6,0] / [12,13.6] 垂直下到板底；
        设计区 x∈[0,12]（= 两腿内边缘之间 = d）内的横段可动，
        腿与直通线固定
    拐弯过渡（论文 Fig.5）：耦合臂是等宽条带以圆角拐弯，外缘 R_OUT=2.0、
        内缘 R_IN=0.4（同心，中心线半径 R_BEND=1.2）；腿带外侧圆角，
        设计区内的臂带内侧圆角，切点落在设计区边角上
    设计区初始金属（component design_region，pipeline 每轮删除重建）
        为"横段 + 两端内侧圆角"，与 configs/coupler.yaml 的
        initial_metal 同一轮廓（测试锁定）
    耦合间距 g = 1.0（直通线下边缘 y=1.0 与臂上边缘 y=0 之间）
    基板 Rogers4350B 30mil：x∈[−5.6,17.6] y∈[−7,5.6] z∈[−0.762,0]
    接地 PEC：z∈[−0.797,−0.762]；空气盒（Vacuum）z∈[0.035,2.0]
        —— 用于把计算域撑到端口面所需高度（磁/电边界下计算域 =
        几何包围盒）
    端口：1/2 在直通线两端（xmin/xmax 面），3/4 在两腿底（ymin 面）——
        CST 的 .Orientation 只认边界面名（xmin/xmax/ymin/ymax），
        不认 positive/negative；.Coordinates 必须 "Free"
    边界：x/y/zmin 磁边界，zmax 电边界（论文设定）
    监视器：5 GHz E/H 场；求解器：时域 TD-S

TODO(架构)：本模块的布局常量目前与算例耦合（耦合器）。后续应把 CST 侧
布局（馈线/端口/空气盒）搬进 YAML，使其与 configs/ 的算例一一对应。

服务器步骤见 scripts/build_cst_template.py 头部注释。
"""

from __future__ import annotations

import math
from pathlib import Path

from eaopt.solver import vba as V

# ---- 布局常量（论文 Fig. 5；单位 mm，z=0 为基板顶面）----
SUB_H, EPS_R, TAND = 0.762, 3.66, 0.0037   # Rogers4350B 30 mil
METAL_T = 0.035
W = 1.6                     # 线宽（直通线 / 耦合臂 / 腿）
G = 1.0                     # 耦合间距
D = 12.0                    # 耦合长度 = 设计区宽（两腿内边缘之间）

LEG_L_IN, LEG_R_IN = 0.0, D                      # 两腿内边缘 = 设计区左右界
LEG_L_OUT, LEG_R_OUT = LEG_L_IN - W, LEG_R_IN + W  # −1.6 / 13.6
FEED_EXT = 4.0              # 直通线在腿外侧的延伸（Fig. 5 约 4 mm）
THRU_X0, THRU_X1 = LEG_L_OUT - FEED_EXT, LEG_R_OUT + FEED_EXT   # −5.6 / 17.6

ARM_HI, ARM_LO = 0.0, -W                        # 耦合臂横段上下边缘
THRU_LO, THRU_HI = ARM_HI + G, ARM_HI + G + W   # 直通线上下边缘 1.0 / 2.6
LEG_BOT = -7.0              # 腿底 = 板下边缘 = 端口 3/4 所在面

# ---- 拐弯过渡（论文 Fig.5：设计区两端的四分之一圆形过渡）----
# 论文图中耦合臂是等宽(w=1.6)条带以圆角拐弯：实测外缘 R≈1.97 mm、
# 内缘 R≈0.31 mm，二者与"中心线半径 R_BEND=1.2 的同心圆弧"自洽
# （R_OUT = R_BEND + w/2 = 2.0，R_IN = R_BEND − w/2 = 0.4）。
# 拐弯中心 (LEG_*_IN + R_IN, ARM_LO − R_IN) = (0.4, −2.0) / (11.6, −2.0)；
# 内圆角的切点正好落在设计区边角上，外圆角在 x=0/12 处被设计区裁掉
# （裁掉的只是一片最厚 0.04 mm 的薄片，可忽略）。
R_BEND = 1.2                # 条带中心线拐弯半径
R_OUT = R_BEND + W / 2      # 外缘圆角半径 = 2.0（腿外边 → 臂上边）
R_IN = R_BEND - W / 2       # 内缘圆角半径 = 0.4（腿内边 → 臂下边）
ARC_SEGS = 6                # 每段四分之一圆的折线段数（弦高误差 ~0.013 mm）
SUB_TOP = THRU_HI + 3.0     # 板上边缘
# 空气盒顶 = 计算域 zmax（电边界）。高度按 CST 微带端口经验取
# h_port ≈ h + 5h ≈ 4.6 mm（端口太高会引入高次模，太低漏场、
# 阻抗不准，见 docs/server_runbook.md 端口一节）
AIR_H = 4.0
FREQ = 5.0


def _arc(cx: float, cy: float, r: float, a0_deg: float, a1_deg: float,
         n: int = ARC_SEGS) -> list[tuple[float, float]]:
    """圆弧折线采样点（不含起点，便于与上一条边相接）。"""
    angs = [math.radians(a0_deg + (a1_deg - a0_deg) * i / n)
            for i in range(1, n + 1)]
    return [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angs]


def _left_leg_polygon() -> list[tuple[float, float]]:
    """左腿轮廓（含外侧四分之一圆过渡，在设计区左界 x=0 处裁断）。"""
    cx, cy = LEG_L_IN + R_IN, ARM_LO - R_IN              # 拐弯中心 (0.4, -2.0)
    a_cross = math.degrees(math.acos((LEG_L_IN - cx) / R_OUT))   # 外弧与 x=0 的交角
    y_cross = cy + R_OUT * math.sin(math.radians(a_cross))       # 交点 y ≈ -0.04
    pts = [(LEG_L_OUT, LEG_BOT), (LEG_L_OUT, cy)]
    pts += _arc(cx, cy, R_OUT, 180.0, a_cross)
    pts[-1] = (LEG_L_IN, y_cross)                        # 用解析交点替换采样末点
    pts += [(LEG_L_IN, LEG_BOT)]
    return pts


def _right_leg_polygon() -> list[tuple[float, float]]:
    """右腿轮廓（左腿关于设计中线镜像，反转绕向保持逆时针）。"""
    mid = (LEG_L_IN + LEG_R_IN) / 2                      # 6.0
    return [(2 * mid - x, y) for x, y in reversed(_left_leg_polygon())]


def _arm_design_polygon() -> list[tuple[float, float]]:
    """设计区初始金属轮廓（耦合臂横段 + 两端内侧四分之一圆过渡）。

    两端内圆角与腿的外圆角同心（同一条带拐弯），切点落在设计区边角：
    左 (0,-2.0)→(0.4,-1.6)，右 (12,-2.0)→(11.6,-1.6)。
    """
    cx, cy = LEG_L_IN + R_IN, ARM_LO - R_IN               # (0.4, -2.0)
    cx2 = LEG_R_IN - R_IN                                 # 11.6
    return ([(LEG_L_IN, cy)]
            + _arc(cx, cy, R_IN, 180.0, 90.0)
            + [(cx2, ARM_LO)]
            + _arc(cx2, cy, R_IN, 90.0, 0.0)      # 末点即 (LEG_R_IN, cy)
            + [(LEG_R_IN, ARM_HI), (LEG_L_IN, ARM_HI)])


def _substrate_parts() -> list[str]:
    """基板 + 接地 + 空气盒（空气盒只用于撑大计算域）。"""
    return [
        V.material_normal("Rogers4350B", EPS_R, 1.0, TAND),
        V.brick("substrate", "component1", "Rogers4350B",
                THRU_X0, THRU_X1, LEG_BOT, SUB_TOP, -SUB_H, 0.0),
        V.brick("ground", "component1", "PEC",
                THRU_X0, THRU_X1, LEG_BOT, SUB_TOP, -SUB_H - METAL_T, -SUB_H),
        # z 从金属顶面起，避免与金属实体重叠
        V.brick("air", "component1", "Vacuum",
                THRU_X0, THRU_X1, LEG_BOT, SUB_TOP, METAL_T, AIR_H),
    ]


def _fixed_metal_parts() -> list[str]:
    """固定金属：直通线 + 两条腿（腿带外侧圆角过渡，与臂内侧圆角同心）。"""
    return [
        V.brick("thru_line", "feed", "PEC",
                THRU_X0, THRU_X1, THRU_LO, THRU_HI, 0.0, METAL_T),
        V.polygon_extrude("leg_left", "feed", "PEC",
                          _left_leg_polygon(), METAL_T),
        V.polygon_extrude("leg_right", "feed", "PEC",
                          _right_leg_polygon(), METAL_T),
    ]


def _arm_design_part() -> str:
    """设计区初始金属（耦合臂横段 + 两端内侧圆角，pipeline 每轮重建）。

    轮廓与 configs/coupler.yaml 的 initial_metal 一致（由测试锁定）。
    圆弧用折线近似（Extrude "Pointlist" 生成的是直边多边形，见
    build_polygon_test_macro）；弦高误差 ~0.013 mm，远小于网格与
    最小间距。
    """
    return V.polygon_extrude("arm_init", "design_region", "PEC",
                             _arm_design_polygon(), METAL_T)


def _ports() -> list[str]:
    """4 个波导端口：1/2 在直通线两端（xmin/xmax 面），3/4 在两腿底（ymin 面）。

    端口面 = 接地板底面（域 zmin）到空气盒顶（域 zmax）的矩形——
    下缘必须贴合接地参考面（CST 微带端口要求），横向半宽 = w/2 + 1.6。
    face 用 CST 的边界面名（xmin/xmax/ymin/ymax）——`.Orientation`
    只认这组取值。
    """
    pz0, pz1 = -SUB_H - METAL_T, AIR_H
    m = 1.6
    return [
        V.waveguide_port(1, "p1", "xmin", THRU_X0,
                         THRU_LO - m, THRU_HI + m, pz0, pz1),
        V.waveguide_port(2, "p2", "xmax", THRU_X1,
                         THRU_LO - m, THRU_HI + m, pz0, pz1),
        V.waveguide_port(3, "p3", "ymin", LEG_BOT,
                         LEG_L_OUT - m, LEG_L_IN + m, pz0, pz1),
        V.waveguide_port(4, "p4", "ymin", LEG_BOT,
                         LEG_R_IN - m, LEG_R_OUT + m, pz0, pz1),
    ]


# 模板清单：(工程名, 激励端口)。fwd = 输入端口 1，bwd = 观测端口 3。
TEMPLATES = (
    ("coupler_fwd", 1),
    ("coupler_bwd", 3),
)


def build_macro(outdir: Path, project: str, portnum: int) -> Path:
    """生成单个命令宏 build_<project>.mcr：在当前空工程中建模并另存。"""
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    target = (outdir / f"{project}.cst").as_posix()
    fname = f"build_{project}.mcr"

    body = [
        "'#Language \"WWB-COM\"",
        f"' CST 命令宏：建立 {project} 模型并另存为 {target}",
        "' 运行前请先 File -> New 新建一个空工程（模板选 <None>）",
        "' 本宏不含 NewProject（工程级指令在宏上下文非法，见模块头部说明）",
        "",
        "Sub Main()",
        "    Dim errLog As String",
        '    errLog = ""',
        "",
        "    With Units",
        '        .Geometry "mm"',
        '        .Frequency "GHz"',
        '        .Time "ns"',
        "    End With",
    ]
    body += _substrate_parts()
    body += _fixed_metal_parts()
    body.append(_arm_design_part())
    body += _ports()
    body += [
        "",
        "    ' ---- 设置类块：逐块容错（CST 2024 实测 Excitation.Reset 报",
        "    ' (10090) ActiveX Automation error，未加保护会让整个宏中止、",
        "    ' SaveAs 都不执行）。几何与端口不加保护：它们失败必须中止。",
        "    On Error Resume Next",
    ]
    for block, label in (
        (V.excitation("excitation1", f'"{portnum}"'), "Excitation"),
        (V.field_monitor("Efield", FREQ), "Monitor Efield"),
        (V.field_monitor("Hfield", FREQ), "Monitor Hfield"),
        (V.set_boundaries("magnetic", "magnetic", "magnetic", "magnetic",
                          "magnetic", "electric"), "Boundary"),
        (V.time_domain_solver_setup(), "Solver"),
    ):
        body.append(V.guarded(block, label))
    # 另存：也放进保护区。CST 2024 命令宏里 SaveAs 只给路径会报
    # "(10097) wrong number of parameters"，需再给一个布尔；两种布尔的
    # 含义在不同版本文档里说法不一（覆盖开关 / 另存副本），故两种都试。
    # 放进保护区的另一个作用：SaveAs 若失败，宏仍走到结尾的报告框，
    # 用户能一次看到所有失败块（上一版 SaveAs 在保护区外，报告框都没弹）。
    body.append(V.guarded_alternatives(
        [f'SaveAs "{target}", "False"',
         f'SaveAs "{target}", "True"'],
        "SaveAs"))
    body += [
        "    On Error GoTo 0",
        "",
        "    ' 报告：明确告诉用户宏是否跑完、哪些块要手工补",
        "    If Len(errLog) > 0 Then",
        f'        MsgBox "Template saved, but some blocks FAILED and must be '
        f'set by hand (see docs/server_runbook.md):" & vbCrLf & vbCrLf & '
        f'errLog & vbCrLf & "saved: {target}", vbExclamation, '
        f'"{project} template"',
        "    Else",
        f'        MsgBox "Template saved OK: {target}" & vbCrLf & '
        f'"all blocks applied", vbInformation, "{project} template"',
        "    End If",
        "End Sub",
    ]
    path = outdir / fname
    # CRLF：Windows VBA 宏文件的原生换行（内容全 ASCII，无编码风险）
    path.write_text("\n".join(body), encoding="utf-8", newline="\r\n")
    return path


def build_all_templates(outdir: Path) -> list[Path]:
    """生成全部模板命令宏（fwd/bwd 各一个）。"""
    return [build_macro(outdir, project, portnum)
            for project, portnum in TEMPLATES]


# ---- 诊断宏：确认 Extrude "Pointlist" 生成的是直边多边形 ----
# L 形（6 点、非凸、含 90° 内角）+ 方形（4 点），错开摆放不重叠。
POLYGON_TEST_SHAPES = (
    ("L_shape", [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0),
                 (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]),
    ("square", [(5.0, 0.0), (7.0, 0.0), (7.0, 2.0), (5.0, 2.0)]),
)


def build_polygon_test_macro(outdir: Path) -> Path:
    """生成诊断宏 polygon_test.mcr：用同一个 polygon_extrude 建 L 形 + 方形。

    pipeline 每轮迭代都要用 Extrude 重建任意轮廓（design_region），
    所以必须确认该模式生成的是**直边多边形**（尖角保留、首尾以直线
    闭合），而不是把点列拟合成曲线/样条。在任意空工程里运行本宏后
    肉眼核对：两个实体都是直边、L 形六个尖角。若出现弧边，则重建
    方式要改（换曲线对象或改用 Brick 拼）。
    """
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    body = [
        "'#Language \"WWB-COM\"",
        "' 诊断宏：检查 Extrude \"Pointlist\" 是否为直边多边形",
        "' 在任意空工程中运行，然后看模型：L 形与方形都应直边、尖角",
        "",
        "Sub Main()",
        "    With Units",
        '        .Geometry "mm"',
        '        .Frequency "GHz"',
        '        .Time "ns"',
        "    End With",
    ]
    for name, pts in POLYGON_TEST_SHAPES:
        body.append(V.polygon_extrude(name, "component1", "PEC", pts, METAL_T))
    body.append("End Sub")
    path = outdir / "polygon_test.mcr"
    path.write_text("\n".join(body), encoding="utf-8", newline="\r\n")
    return path
