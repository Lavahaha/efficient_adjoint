"""CST 双模板宏生成（本地可单测，无需 CST）。

宏类型（CST 2024 实测 + 官方文档结论）——**这是最容易踩的坑**：
  - **结构宏 `.mcs`**：建模类指令，动作**写进 History List**，CST 拿它
    重放生成模型；
  - **控制宏 `.mcr`**：控制/工程类指令（新建/打开/另存/后处理），
    动作**不进 History List**。

  服务器实测：用 .mcr 建出来的工程，几何/端口/监视器在会话里都正常，
  但 **History List 是空的** ⇒ 存盘重开就是空工程（"打开一片空白"的
  根因）。所以建模必须用 .mcs。

  **另一个同等重要的条件**：即使文件是 .mcs，也**必须在 CST 主界面的
  Macros 下拉菜单里运行**——在 VBA 编辑器里点运行图标执行，不会写
  History List。两条都满足才进历史表。

其他约束：
  1. Import Macro 对话框只列 "CST Macro Files (*.mcs; *.mcr)"，.bas 不可见；
  2. **工程级指令只在控制宏上下文合法**：在 .mcs 里执行 NewProject 实测
     报 "Invalid instruction"。故建模宏（.mcs）不含 NewProject/SaveAs，
     新建工程由用户在 GUI 中 File → New 完成，另存交给配套的
     save_<project>.mcr（或 GUI 手工另存）；所有参数写字面量，不依赖
     VBA 变量传参。
  3. **SaveAs 必须带两个参数**：`SaveAs "<路径>"` 实测报
     "(10097) ActiveX Automation: wrong number of parameters"，需再给一个
     布尔（见 vba.guarded_alternatives 调用处）。
  4. 设置类块（激励/监视器/边界/求解器）逐个容错，失败只记入结尾的报告
     框——CST 2024 实测 `Excitation.Reset` 报 "(10090)"，未加保护会让整个
     宏中止。

用法（**首选**）：不经过宏菜单，直接跑
  ``python scripts/cst_build_template.py configs/coupler.yaml``
（Python + COM，逐块 ``mws.AddToHistory(标题, 命令文本)``：既执行又写历史
表，且每块能拿到返回值与原始异常）。本模块的 template_blocks() 是那条路
与下面宏路共用的**单一事实来源**。

备用用法（每个模板一次，共两次）：
  CST → File → New（模板 <None>）→ Macros 菜单运行 build_coupler_fwd.mcs
  → 确认 History List 非空、几何与 4 个端口都在 → 运行 save_coupler_fwd.mcr
  （或 File → Save As）→ 得到 <输出目录>/coupler_fwd.cst；
  再 File → New → build_coupler_bwd.mcs → save_coupler_bwd.mcr → coupler_bwd.cst。
另有诊断宏 polygon_test.mcs（可选，见 build_polygon_test_macro）。

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
# 时域求解频段：0 ~ 2×f0。显式写进模板，避免"某次在 GUI 里手改过频段"
# 导致两个模板不一致（频段决定自适应网格与脉冲带宽，会影响 S 参数）。
FMIN, FMAX = 0.0, 2 * FREQ


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


def model_blocks() -> list[str]:
    """全部**建模**命令块（顺序即执行顺序）：基板/接地/空气盒 → 固定金属
    → 设计区初始金属 → 4 个端口。"""
    return (_substrate_parts() + _fixed_metal_parts()
            + [_arm_design_part()] + _ports())


def setting_blocks(portnum: int) -> list[tuple[str, str]]:
    """全部**设置**类块 ``[(标签, 命令文本)]``：求解器/频段/监视器/边界。

    顺序与 template_blocks 一致（标签在前）——两条建模板路径共用同一份
    列表，元组顺序写反会让 COM 路径把命令文本当标题传给 AddToHistory。
    标签是 ASCII（宏里要写进 errLog，见 vba.guarded 的编码说明）。
    """
    return [
        # 只激励本模板指定的端口（fwd→1 / bwd→3）。用 Solver 的
        # StimulationPort，不用 Excitation 对象（后者在 CST 2024 里
        # 报 10090，且失败静默：S 参数照样对，场却是多激励叠加的）。
        # 端口与模式成对给出（"1"/"1"）：实测 "1" + "All" 会让
        # Solver.Start 报 "Invalid stimulation port, please specify."。
        ("Solver", V.time_domain_solver_setup(str(portnum))),
        ("FrequencyRange", V.frequency_range(FMIN, FMAX)),
        ("Monitor Efield", V.field_monitor("Efield", FREQ)),
        ("Monitor Hfield", V.field_monitor("Hfield", FREQ)),
        ("Boundary", V.set_boundaries("magnetic", "magnetic", "magnetic",
                                      "magnetic", "magnetic", "electric")),
    ]


UNITS_BLOCK = ('With Units\n'
               '    .Geometry "mm"\n'
               '    .Frequency "GHz"\n'
               '    .Time "ns"\n'
               'End With')


def block_header(cmd: str) -> str:
    """从命令文本推一个可读的标题（进 History List 时显示这一列）。

    形如 ``With Brick`` + ``.Name "substrate"`` → ``"Brick substrate"``。
    """
    text = cmd.strip()
    kind = text.split("\n", 1)[0].replace("With ", "").strip() or "Block"
    for line in text.splitlines():
        s = line.strip()
        for key in (".Name ", ".Label "):
            if s.startswith(key):
                return f'{kind} {s[len(key):].strip().strip(chr(34))}'
    return kind


def template_blocks(project: str, portnum: int) -> list[tuple[str, str]]:
    """模板的全部命令块 ``[(标题, VBA 命令文本)]`` —— **单一事实来源**。

    两条建模板的路径共用本函数，保证建出来的模型一模一样：
      - GUI 宏路径：build_macro() 拿它拼 .mcs（结构宏）；
      - COM 路径：scripts/cst_build_template.py 把每个块交给
        ``mws.AddToHistory(标题, 命令文本)``（既执行、又写进历史表）。
    改几何只改这里（以及它调用的 vba 生成器），两条路径不会漂移。
    """
    blocks = [("Units", UNITS_BLOCK)]
    blocks += [(block_header(c), c) for c in model_blocks()]
    blocks += setting_blocks(portnum)
    return blocks


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
    """生成**结构宏** build_<project>.mcs：在当前空工程中建模。

    **为什么必须是 .mcs（结构宏）而不是 .mcr（控制宏）**——服务器实测：
    .mcr 跑完几何/端口/监视器都"看起来"建好了，但 **History List 是空
    的**。CST 的模型是"历史表重放"出来的：历史为空 ⇒ 存盘出来的工程
    重开就是空的（这正是"打开一片空白"的根因）。结构宏的动作才会写进
    History List（见 docs/server_runbook.md 第 2 节）。

    另存不在这里做：SaveAs 是**工程级指令**，只在控制宏上下文合法
    （在 .mcs 里报 "Invalid instruction"），由配套的 build_save_macro
    生成的 save_<project>.mcr 负责，或用户在 GUI 里手工 File → Save As。
    """
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    fname = f"build_{project}.mcs"

    body = [
        "'#Language \"WWB-COM\"",
        f"' CST **结构宏**（.mcs）：建立 {project} 模型。动作会进 History List。",
        "' 运行前请先 File -> New 新建一个空工程（模板选 <None>）",
        "' **必须在 CST 主界面的 Macros 下拉菜单里运行**：在 VBA 编辑器里",
        "' 点运行图标，即使是结构宏也不写 History List（实测结论）。",
        "' 本宏不含 NewProject/SaveAs（工程级指令只在控制宏里合法）。",
        "",
        "Sub Main()",
        "    Dim errLog As String",
        '    errLog = ""',
        "",
    ]
    body += ["    " + ln for ln in UNITS_BLOCK.splitlines()]  # 见 template_blocks
    body += model_blocks()
    body += [
        "",
        "    ' ---- 设置类块：逐块容错。CST 2024 宏里设置类命令实测报过",
        "    ' (10090) ActiveX Automation error / (10097) wrong number of",
        "    ' parameters；不加保护的话整个宏会中止，而且用户看不到到底",
        "    ' 哪块失败。几何与端口不加保护：它们失败必须中止（模板建不",
        "    ' 出来就没有意义）。",
        "    On Error Resume Next",
    ]
    for label, block in setting_blocks(portnum):
        body.append(V.guarded(block, label))
    body += [
        "    On Error GoTo 0",
        "",
        "    ' 报告：明确告诉用户哪些块要手工补，以及接下来该干什么",
        "    If Len(errLog) > 0 Then",
        f'        MsgBox "Some blocks FAILED and must be set by hand '
        f'(see docs/server_runbook.md):" & vbCrLf & vbCrLf & errLog, '
        f'vbExclamation, "{project} structure macro"',
        "    Else",
        f'        MsgBox "All blocks applied." & vbCrLf & vbCrLf & '
        f'"Now: (1) check History List is NOT empty and shows the solids/'
        f'ports, (2) run save_{project}.mcr (or File -> Save As) to '
        f'write {project}.cst.", vbInformation, '
        f'"{project} structure macro"',
        "    End If",
        "End Sub",
    ]
    path = outdir / fname
    # CRLF：Windows VBA 宏文件的原生换行（内容全 ASCII，无编码风险）
    path.write_text("\n".join(body), encoding="utf-8", newline="\r\n")
    return path


def build_save_macro(outdir: Path, project: str) -> Path:
    """生成配套的**控制宏** save_<project>.mcr：把当前工程另存为模板。

    与 build_<project>.mcs 分开的原因：SaveAs 是工程级指令，只在控制宏
    （.mcr）上下文合法。跑完结构宏、在 GUI 里确认几何/端口都在之后，
    运行本宏即可；也可以直接 File → Save As，效果一样。
    """
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    target = (outdir / f"{project}.cst").as_posix()
    body = [
        "'#Language \"WWB-COM\"",
        f"' CST 控制宏（.mcr）：把当前工程另存为 {target}",
        "' 先跑 build_*.mcs（结构宏）并确认 History List 非空，再跑本宏。",
        "",
        "Sub Main()",
        "    Dim errLog As String",
        '    errLog = ""',
        "    On Error Resume Next",
    ]
    # SaveAs 只给路径会报 "(10097) wrong number of parameters"，需再给
    # 一个布尔；两种布尔的含义在不同版本文档里说法不一（覆盖开关 /
    # 另存副本），故两种都试。
    body.append(V.guarded_alternatives(
        [f'SaveAs "{target}", "False"',
         f'SaveAs "{target}", "True"'],
        "SaveAs"))
    body += [
        "    On Error GoTo 0",
        "    If Len(errLog) > 0 Then",
        f'        MsgBox "SaveAs FAILED:" & vbCrLf & errLog & vbCrLf & '
        f'"use File -> Save As by hand.", vbExclamation, '
        f'"{project} save"',
        "    Else",
        f'        MsgBox "Saved: {target}", vbInformation, "{project} save"',
        "    End If",
        "End Sub",
    ]
    path = outdir / f"save_{project}.mcr"
    path.write_text("\n".join(body), encoding="utf-8", newline="\r\n")
    return path


def build_all_templates(outdir: Path) -> list[Path]:
    """生成全部模板宏：每个模板一对（结构宏 .mcs + 另存控制宏 .mcr）。

    顺序：fwd 的 .mcs/.mcr，然后 bwd 的 .mcs/.mcr。
    """
    out: list[Path] = []
    for project, portnum in TEMPLATES:
        out.append(build_macro(outdir, project, portnum))
        out.append(build_save_macro(outdir, project))
    return out


# ---- 诊断宏：确认 Extrude "Pointlist" 生成的是直边多边形 ----
# L 形（6 点、非凸、含 90° 内角）+ 方形（4 点），错开摆放不重叠。
POLYGON_TEST_SHAPES = (
    ("L_shape", [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0),
                 (1.0, 1.0), (1.0, 3.0), (0.0, 3.0)]),
    ("square", [(5.0, 0.0), (7.0, 0.0), (7.0, 2.0), (5.0, 2.0)]),
)


def build_polygon_test_macro(outdir: Path) -> Path:
    """生成诊断**结构宏** polygon_test.mcs：用同一个 polygon_extrude 建
    L 形 + 方形。

    pipeline 每轮迭代都要用 Extrude 重建任意轮廓（design_region），
    所以必须确认该模式生成的是**直边多边形**（尖角保留、首尾以直线
    闭合），而不是把点列拟合成曲线/样条。在任意空工程里（从 Macros
    菜单）运行本宏后肉眼核对：两个实体都是直边、L 形六个尖角。若出现
    弧边，则重建方式要改（换曲线对象或改用 Brick 拼）。

    用 .mcs 而非 .mcr：它要建模，必须进 History List（见模块头部）。
    """
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    body = [
        "'#Language \"WWB-COM\"",
        "' 诊断结构宏：检查 Extrude \"Pointlist\" 是否为直边多边形",
        "' 从 CST 主界面的 Macros 菜单运行（在编辑器里点运行不进历史表）",
        "' 然后看模型：L 形与方形都应直边、尖角，且 History List 非空",
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
    path = outdir / "polygon_test.mcs"
    path.write_text("\n".join(body), encoding="utf-8", newline="\r\n")
    return path
