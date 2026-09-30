"""CST 双模板宏（.mcr 命令宏）生成（本地可单测，无需 CST）。

命名与类型的两点约束（均为 CST 2024 实测/文档结论）：
  1. Import Macro 对话框只列 "CST Macro Files (*.mcs; *.mcr)"，.bas 不可见；
  2. CST 宏分两类——命令宏（.mcr，控制类指令）/ 结构宏（.mcs，建模类指令，
     动作进 History List）。**工程级指令（新建/打开/另存工程）只在命令宏
     上下文合法**：在 .mcs 里执行 NewProject 实测报 "Invalid instruction"。
     故本宏不含 NewProject，只做"建模 + SaveAs"，新建工程由用户在 GUI 中
     File → New 完成；且所有参数写字面量，不依赖 VBA 变量传参。

用法（每个模板一次，共两次）：
  CST → File → New（模板 <None>）→ 导入并运行 build_coupler_fwd.mcr
  → 生成 <输出目录>/coupler_fwd.cst；再 File → New → 运行
  build_coupler_bwd.mcr → coupler_bwd.cst。

几何布局（单位 mm，z=0 为基板顶面；按论文 Fig. 5 与 w/d/g 参数）：
    直通线（固定，端口 1-2）：y∈[1.0,2.6]（w=1.6），x 贯通整块板
    耦合臂（"⊓"形）：横段 y∈[−1.6,0]（w=1.6）位于两腿之间，
        两端各一条腿 x∈[−1.6,0] / [12,13.6] 垂直下到板底；
        设计区 x∈[0,12]（= 两腿内边缘之间 = d）内的横段可动，
        腿与直通线固定
    耦合间距 g = 1.0（直通线下边缘 y=1.0 与臂上边缘 y=0 之间）
    基板 Rogers4350B 30mil：x∈[−5.6,17.6] y∈[−7,5.6] z∈[−0.762,0]
    接地 PEC：z∈[−0.797,−0.762]；空气盒（Vacuum）z∈[0.035,2.0]
        —— 用于把计算域撑到端口面所需高度（磁/电边界下计算域 =
        几何包围盒）
    端口：1/2 在直通线两端（x=const 面，沿 ±x），
        3/4 在两腿底（y=−7，沿 +y）
    边界：x/y/zmin 磁边界，zmax 电边界（论文设定）
    监视器：5 GHz E/H 场；求解器：时域 TD-S

TODO(架构)：本模块的布局常量目前与算例耦合（耦合器）。后续应把 CST 侧
布局（馈线/端口/空气盒）搬进 YAML，使其与 configs/ 的算例一一对应。

服务器步骤见 scripts/build_cst_template.py 头部注释。
"""

from __future__ import annotations

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
SUB_TOP = THRU_HI + 3.0     # 板上边缘
AIR_H = 2.0                 # 空气盒顶 = 计算域 zmax（电边界）
FREQ = 5.0


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
    """固定金属：直通线 + 两条腿（腿顶与臂横段齐平，构成 "⊓" 的外角）。"""
    return [
        V.brick("thru_line", "feed", "PEC",
                THRU_X0, THRU_X1, THRU_LO, THRU_HI, 0.0, METAL_T),
        V.brick("leg_left", "feed", "PEC",
                LEG_L_OUT, LEG_L_IN, LEG_BOT, ARM_HI, 0.0, METAL_T),
        V.brick("leg_right", "feed", "PEC",
                LEG_R_IN, LEG_R_OUT, LEG_BOT, ARM_HI, 0.0, METAL_T),
    ]


def _arm_design_part() -> str:
    """设计区初始金属（耦合臂横段，pipeline 每轮重建）。"""
    return V.polygon_extrude("arm_init", "design_region", "PEC",
                             [(LEG_L_IN, ARM_LO), (LEG_R_IN, ARM_LO),
                              (LEG_R_IN, ARM_HI), (LEG_L_IN, ARM_HI)],
                             METAL_T)


def _ports() -> list[str]:
    """4 个波导端口：1/2 在直通线两端（x=const），3/4 在两腿底（y=const）。

    端口面 = 基板底面到空气盒顶的矩形，横向半宽 = w/2 + 1.6 余量。
    """
    pz0, pz1 = -SUB_H, AIR_H
    m = 1.6
    return [
        V.waveguide_port(1, "p1", "x", THRU_X0, "positive",
                         THRU_LO - m, THRU_HI + m, pz0, pz1),
        V.waveguide_port(2, "p2", "x", THRU_X1, "negative",
                         THRU_LO - m, THRU_HI + m, pz0, pz1),
        V.waveguide_port(3, "p3", "y", LEG_BOT, "positive",
                         LEG_L_OUT - m, LEG_L_IN + m, pz0, pz1),
        V.waveguide_port(4, "p4", "y", LEG_BOT, "positive",
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
        V.excitation("excitation1", f'"{portnum}"'),
        V.field_monitor("e5", "Efield", FREQ),
        V.field_monitor("h5", "Hfield", FREQ),
        V.set_boundaries("magnetic", "magnetic", "magnetic", "magnetic",
                         "magnetic", "electric"),
        V.time_domain_solver_setup(),
        f'SaveAs "{target}"',
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
