"""CST 侧模型文本：模板命令块 + VBA 命令串（**纯字符串，本地可单测**）。

本模块不 import cst、不碰任何 API——只产出文本，由调用方（三个 CST 程序 /
``cst.py``）交给 ``model3d.add_to_history(标题, 命令文本)``。两件事：

1. **VBA 命令片段**（材质/长方体/挤出/端口/监视器/边界/求解器/ASCIIExport
   参数）——版本敏感的写法与实测结论都写在各自 docstring 里；
2. **模板命令块** ``template_blocks(project, portnum)``——建一个工程所需的
   全部 ``[(标题, 命令文本)]``，是几何的**单一事实来源**：改几何只改这里，
   三个脚本不会有第二份定义。

约定（服务器实测，勿违反）：

* 所有坐标单位 mm，z=0 为基板顶面（金属底面）；
* "只激励哪个端口"用 Solver 的 ``.StimulationPort``。**不要用 Excitation
  对象**：它在 CST 2024 命令宏上下文里实测报 "(10090) ActiveX Automation
  error"，且失败是静默的——S 参数照样对，但场监视器里存的是多个激励叠加
  的场，会悄悄毁掉伴随梯度；
* 端口面落在计算域边界面上，``.Orientation`` 只认 ``xmin/xmax/ymin/ymax``
  边界面名（不认 positive/negative），``.Coordinates`` 必须 ``"Free"``；
* 场导出用 ASCIIExport 的**固定属性集**（``ascii_export_params``），没有
  XStart/XEnd 这类范围属性（实测不存在）；
* 编码：CST 宏文件按 ANSI 解码，**可执行语句里不要出现非 ASCII**；中文只
  写在注释里。

几何布局（论文 Fig. 5，单位 mm，z=0 为基板顶面）：

    直通线（固定，端口 1-2）：y∈[1.0,2.6]（w=1.6），x 贯通整块板
    耦合臂（"⊓"形）：横段 y∈[−1.6,0]（w=1.6），两端各一条腿
        x∈[−1.6,0] / [12,13.6] 垂直下到板底；设计区 x∈[0,12]
        （= 两腿内边缘之间 = d）内的横段可动，腿与直通线固定
    拐弯过渡（论文 Fig.5）：耦合臂是等宽条带以圆角拐弯，外缘 R_OUT=2.0、
        内缘 R_IN=0.4（同心，中心线半径 R_BEND=1.2）
    基板 Rogers4350B 30mil：x∈[−5.6,17.6] y∈[−7,5.6] z∈[−0.762,0]
    接地 PEC：z∈[−0.797,−0.762]；空气盒（Vacuum）z∈[0.035,2.0]
    端口：1/2 在直通线两端（xmin/xmax 面），3/4 在两腿底（ymin 面）
    边界：x/y/zmin 磁边界，zmax 电边界（论文设定）
    监视器：5 GHz E/H 场（Volume）；求解器：时域 TD-S

物理常量（频点/材料/端口）来自 CST 侧单一事实来源 ``cst_setup.COUPLER``，
不在本模块另写一份。
"""

from __future__ import annotations

import math

from eaopt.solver.cst_setup import COUPLER

__all__ = [
    # VBA 片段
    "material_normal", "brick", "polygon_extrude",
    "DESIGN_COMPONENT", "delete_component", "design_region_update",
    "waveguide_port", "field_monitor", "field_monitor_name",
    "field_result_path", "FIELD_TYPES",
    "set_boundaries", "time_domain_solver_setup", "frequency_range",
    "MESH_CREATOR",
    "ASCII_EXPORT_MODE", "ASCII_EXPORT_EXECUTE", "ascii_export_params",
    # 模板
    "template_blocks", "model_blocks", "setting_blocks", "block_header",
    "UNITS_BLOCK", "arc_points", "layout_view",
    # 布局常量
    "SUB_H", "EPS_R", "TAND", "METAL_T", "W", "G", "D",
    "LEG_L_IN", "LEG_R_IN", "LEG_L_OUT", "LEG_R_OUT",
    "THRU_X0", "THRU_X1", "THRU_LO", "THRU_HI",
    "ARM_HI", "ARM_LO", "LEG_BOT", "R_BEND", "R_OUT", "R_IN",
    "ARC_SEGS", "SUB_TOP", "AIR_H", "FREQ", "FMIN", "FMAX",
]


# =========================================================================== #
# 一、VBA 命令片段
# =========================================================================== #
def material_normal(name: str, eps: float, mu: float = 1.0, tand: float = 0.0) -> str:
    """普通介电材料（Rogers 基板等）。"""
    return (
        "With Material\n"
        "    .Reset\n"
        f"    .Name \"{name}\"\n"
        '    .Folder ""\n'
        f"    .FrqType \"all\"\n"
        '    .Type "Normal"\n'
        '    .SetMaterialUnit "GHz", "mm"\n'
        f"    .Epsilon \"{eps}\"\n"
        f"    .Mu \"{mu}\"\n"
        f"    .TanD \"{tand}\"\n"
        "    .Create\n"
        "End With\n"
    )


def brick(name: str, component: str, material: str,
          x0: float, x1: float, y0: float, y1: float,
          z0: float, z1: float) -> str:
    """长方体实体。"""
    return (
        f"With Brick\n"
        f"    .Reset\n"
        f"    .Name \"{name}\"\n"
        f"    .Component \"{component}\"\n"
        f"    .Material \"{material}\"\n"
        f"    .Xrange \"{x0:.6g}\", \"{x1:.6g}\"\n"
        f"    .Yrange \"{y0:.6g}\", \"{y1:.6g}\"\n"
        f"    .Zrange \"{z0:.6g}\", \"{z1:.6g}\"\n"
        f"    .Create\n"
        f"End With\n"
    )


def polygon_extrude(name: str, component: str, material: str,
                    points, height_mm: float, z0: float = 0.0) -> str:
    """多边形挤出实体（Extrude 对象，录制式：.Mode "Pointlist"）。

    points: (N,2)（挤出平面内坐标，z 分量忽略）。轮廓点直接写进
    Extrude 块，无需先建 Polygon 曲线；挤出平面由 Origin 与
    Uvector/Vvector 给出（此处取 x/y 基矢，故点坐标即 (x, y)），
    沿 U×V=+z 挤出 height_mm。

    注意：**没有 .PlaneNormal 属性**（CST 2024 实测报
    "no such property or method (.PlaneNormal)"），方向只能由
    Origin + Uvector + Vvector 表达。
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    lines = [
        "With Extrude",
        "    .Reset",
        f'    .Name "{name}"',
        f'    .Component "{component}"',
        f'    .Material "{material}"',
        '    .Mode "Pointlist"',
        f'    .Height "{height_mm:.6g}"',
        '    .Twist "0.0"',
        '    .Taper "0.0"',
        f'    .Origin "0.0", "0.0", "{z0:.6g}"',
        '    .Uvector "1.0", "0.0", "0.0"',
        '    .Vvector "0.0", "1.0", "0.0"',
        f'    .Point "{pts[0][0]:.6g}", "{pts[0][1]:.6g}"',
    ]
    for x, y in pts[1:]:
        lines.append(f'    .LineTo "{x:.6g}", "{y:.6g}"')
    lines += ["    .Create", "End With", ""]
    return "\n".join(lines) + "\n"


#: 设计区组件名。**模板与每轮重建必须用同一个名字**——名字漂移的话每轮会
#: 新建一个组件，而上一轮的金属留在模型里，梯度就作用在一个"叠了两层金属"
#: 的模型上，且 S 参数照样出数。
DESIGN_COMPONENT = "design_region"


def delete_component(component: str) -> str:
    """删除整个组件（连同其中所有实体）。

    比"按名字逐个删实体"稳：每轮不需要知道上一轮有几个实体、叫什么。

    ``On Error Resume Next`` 只包住这一条并在其后立刻 ``On Error GoTo 0``
    复位（成对出现，不会把错误处理状态泄漏给同一条历史记录里的挤出命令）：
    组件不存在时（首次更新、或工程里本来没有设计区金属）不该让整块失败。
    """
    return (
        "On Error Resume Next\n"
        f'Component.Delete "{component}"\n'
        "On Error GoTo 0\n"
    )


def design_region_update(polys, height_mm: float,
                         component: str = DESIGN_COMPONENT,
                         material: str = "PEC", z0: float = 0.0) -> str:
    """一轮形状更新：删掉设计区组件 → 按多边形逐个挤出。

    整段是**一条历史记录**（调用方把它整体交给 ``add_to_history``）：
    既改当前模型、又写进 History List，所以工程重放历史得到的就是当前
    形状——直接调对象模型命令做不到这一点（当次看着对，重开工程旧形状
    复活）。每轮历史表只增长一条，20 轮量级无压力。

    polys: 可迭代的 (N,2) 世界坐标多边形（mm）。**空列表直接报错**——
    那会把设计区金属全部删掉、静默产出一个没有可动金属的模型。
    """
    polys = list(polys)
    if not polys:
        raise ValueError("polys 为空：这会把设计区金属全删掉，拒绝生成该命令")
    for i, p in enumerate(polys):
        if len(p) < 3:
            raise ValueError(f"polys[{i}] 只有 {len(p)} 个点，CST 建不出实体")
        if any(len(q) < 2 for q in p):
            raise ValueError(f"polys[{i}] 的元素必须形如 (x, y)")
    parts = [delete_component(component)]
    for i, p in enumerate(polys):
        parts.append(polygon_extrude(f"design_{i}", component, material,
                                     p, height_mm, z0=z0))
    return "".join(parts)


# 端口所在的计算域边界面（CST 的 .Orientation 只认这组名字）
PORT_FACES = ("xmin", "xmax", "ymin", "ymax")


def waveguide_port(port_number: int, name: str, face: str, at: float,
                   u0: float, u1: float, z0: float, z1: float) -> str:
    """波导端口（单模）：端口面落在计算域边界面 face 上。

    face: "xmin"/"xmax"/"ymin"/"ymax" —— CST 2024 实测：.Orientation
    不接受 "positive"/"negative"，只认边界面名（录制宏同）。
    at: 该面的坐标（如 xmin 面的 x 值）；u0..u1 为面内横向范围
    （x 面 → y 范围，y 面 → x 范围）；z0..z1 为高度范围。
    .Coordinates 必须 "Free"（给显式范围；合法值仅 Free/Full/Picks）。
    """
    if face not in PORT_FACES:
        raise ValueError(f"face 只能是 {PORT_FACES}，收到 {face!r}")
    axis, side = face[0], face[1:]
    u_axis = "y" if axis == "x" else "x"
    rng = {
        axis: f'"{at:.6g}", "{at:.6g}"',      # 端口面：该轴为常量
        u_axis: f'"{u0:.6g}", "{u1:.6g}"',
        "z": f'"{z0:.6g}", "{z1:.6g}"',
    }
    return (
        "With Port\n"
        "    .Reset\n"
        f'    .PortNumber "{port_number}"\n'
        f'    .Label "{name}"\n'
        '    .Folder ""\n'
        '    .NumberOfModes "1"\n'
        '    .AdjustPolarization "False"\n'
        '    .PolarizationAngle "0.0"\n'
        '    .ReferencePlaneDistance "0"\n'
        '    .TextSize "50"\n'
        '    .TextMaxLimit "1"\n'
        '    .Coordinates "Free"\n'
        f'    .Orientation "{face}"\n'
        '    .PortOnBound "True"\n'      # 端口面就在计算域边界面上
        '    .ClipPickedPortToBound "False"\n'
        f'    .Xrange {rng["x"]}\n'
        f'    .Yrange {rng["y"]}\n'
        f'    .Zrange {rng["z"]}\n'
        # 下面三条是 GUI 录制宏里跟着出现的（范围附加量，默认全 0）；
        # 默认值本来就是这个，写上是为了与录制结果逐行一致——出问题时
        # 可以直接和 CST 自己录的宏对拍。
        '    .XrangeAdd "0.0", "0.0"\n'
        '    .YrangeAdd "0.0", "0.0"\n'
        '    .ZrangeAdd "0.0", "0.0"\n'
        '    .SingleEnded "False"\n'
        '    .WaveguideMonitor "False"\n'
        "    .Create\n"
        "End With\n"
    )


# 场监视器类型 → (结果树条目前缀, 结果树文件夹)。CST 里 E 场监视器的结果
# 条目名为 "e-field (f=5) [AC]"，归属 "E-Field" 文件夹（H 场同构）。
FIELD_TYPES = {"Efield": ("e-field", "E-Field"),
               "Hfield": ("h-field", "H-Field")}


def field_monitor_name(field_type: str, frequency_ghz: float) -> str:
    """监视器名，取 CST 惯例 "e-field (f=5)"。

    **这个名字同时是结果树条目名的叶子**（CST 再往后缀，如 "<name> [AC]"）：
    导出场时到结果树上按这个叶子名认领条目（见
    ``cst_results.resolve_field_item``），所以创建与导出必须共用本函数，
    不能各写一份——否则命名漂移会让场导出找不到条目。
    """
    if field_type not in FIELD_TYPES:
        raise ValueError(f"field_type 只能是 {tuple(FIELD_TYPES)}，"
                         f"收到 {field_type!r}")
    return f"{FIELD_TYPES[field_type][0]} (f={frequency_ghz:g})"


def field_result_path(field_type: str, frequency_ghz: float) -> str:
    """该监视器在结果树中的**惯例**条目路径（``... [AC]`` 是猜测的后缀）。

    导出场选中条目**不靠它**：真实条目名由 CST 起（后缀随版本/运行次数变），
    由 ``cst_results.resolve_field_item`` 到活结果树上按叶子名认领。这条路径
    只做两件事：枚举不可用时的退路、报错里与 CST 的条目名对拍的参照。
    """
    folder = FIELD_TYPES[field_type][1] if field_type in FIELD_TYPES else None
    if folder is None:
        raise ValueError(f"field_type 只能是 {tuple(FIELD_TYPES)}，"
                         f"收到 {field_type!r}")
    return (f"2D/3D Results\\{folder}\\"
            f"{field_monitor_name(field_type, frequency_ghz)} [AC]")


def field_monitor(field_type: str, frequency_ghz: float) -> str:
    """频域场监视器（Volume：覆盖整个计算域，没有"位置"参数）。

    Volume 监视器与计算域同大小，因此它的边界自然贴在四个端口面上
    （端口面就是计算域边界面）——这是正常现象，不是"监视器跑到端口
    上去了"。导出场时只按设计区附近的薄层取数（见 cst_results.crop_grid）。

    `.UseSubvolume "False"` 按 GUI 录制宏补上：录制结果里它后面还跟着
    `.Coordinates`/`.SetSubvolume`/`.SetSubvolumeOffset`/
    `.SetSubvolumeInflateWithOffset` 四条子域设置，但 UseSubvolume=False
    时它们全是惰性的，故不写（也避免照抄录制里疑似截断的取值）。
    """
    return (
        "With Monitor\n"
        "    .Reset\n"
        f'    .Name "{field_monitor_name(field_type, frequency_ghz)}"\n'
        '    .Dimension "Volume"\n'
        '    .Domain "Frequency"\n'
        f'    .FieldType "{field_type}"\n'
        f'    .MonitorValue "{frequency_ghz:g}"\n'
        '    .UseSubvolume "False"\n'
        "    .Create\n"
        "End With\n"
    )


def set_boundaries(xmin: str, xmax: str, ymin: str, ymax: str,
                   zmin: str, zmax: str) -> str:
    """边界条件（论文：x/y/zmin 磁边界，zmax 电边界）。"""
    return (
        "With Boundary\n"
        f'    .Xmin "{xmin}"\n'
        f'    .Xmax "{xmax}"\n'
        f'    .Ymin "{ymin}"\n'
        f'    .Ymax "{ymax}"\n'
        f'    .Zmin "{zmin}"\n'
        f'    .Zmax "{zmax}"\n'
        "End With\n"
    )


def time_domain_solver_setup(stimulation_port: str = "All",
                             stimulation_mode: str | None = None) -> str:
    """时域求解器设置（论文用法，默认精度）。

    stimulation_port: 激励端口，"All" 或端口号字符串（如 "1"）。**这是
    "只激励哪个端口"的正规、版本稳定的写法**（Excitation 对象在 CST 2024
    命令宏里实测报 "(10090) ActiveX Automation error"）。伴随法要求两个
    模板各自只激励一个端口（fwd→1，bwd→3）：否则场监视器存的是多个激励
    叠加的场，梯度就无从谈起（S 参数不受影响，所以光看 S 参数发现不了）。

    stimulation_mode: 激励模式，"All" 或模式号字符串（单模端口即 "1"）。
    None → 与端口取同一值（"All"/"All" 或 "1"/"1"）。**端口与模式必须
    成对**：CST 2024 实测 `.StimulationPort "1"` + `.StimulationMode "All"`
    会让 Solver.Start 报 "Invalid stimulation port, please specify."。

    属性集与顺序照 **GUI 录制宏**对齐：除了我们原来自写的几项，录制结果里
    还有 CalculateModesOnly / SParaSymmetry / StoreTDResultsInCache /
    RunDiscretizerOnly / FullDeembedding / SuperimposePLWExcitation /
    UseSensitivityAnalysis——都是默认值，写上只为与 CST 自己的记录逐行对齐
    （便于对拍、也避免某台机器上默认值被 GUI 手改过）。
    `.SteadyStateLimit "-40"` 取该版本 GUI 默认值。
    """
    if stimulation_mode is None:
        stimulation_mode = "All" if stimulation_port == "All" else "1"
    return (
        "With Solver\n"
        '    .Method "Hexahedral"\n'
        '    .CalculationType "TD-S"\n'
        f'    .StimulationPort "{stimulation_port}"\n'
        f'    .StimulationMode "{stimulation_mode}"\n'
        '    .SteadyStateLimit "-40"\n'
        '    .MeshAdaption "False"\n'
        '    .CalculateModesOnly "False"\n'
        '    .SParaSymmetry "False"\n'
        '    .StoreTDResultsInCache "False"\n'
        '    .RunDiscretizerOnly "False"\n'
        '    .FullDeembedding "False"\n'
        '    .SuperimposePLWExcitation "False"\n'
        '    .UseSensitivityAnalysis "False"\n'
        # 下面两项是录制里没有、我们额外保留的：S 参数按 50 Ω 归一，
        # 不依赖该机器 GUI 里的默认值（|S31| 是我们的目标函数）。
        '    .AutoNormImpedance "False"\n'
        '    .NormingImpedance "50"\n'
        "End With\n"
    )


# GUI 录制：打开时域求解器对话框时，CST 会把网格生成器写进历史表。
# 单独一条历史记录（录制里就是独立一行），故单独成块。
MESH_CREATOR = 'Mesh.SetCreator "High Frequency"\n'


def frequency_range(fmin_ghz: float, fmax_ghz: float) -> str:
    """时域求解的频段（决定自适应网格与激励脉冲带宽）。

    模板里显式写死，避免"某次在 GUI 里手改过频段"造成两个模板不一致。
    """
    return f'Solver.FrequencyRange "{fmin_ghz:g}", "{fmax_ghz:g}"\n'


#: ASCIIExport 的 Mode 取值。**FixedWidth** = 按 StepX/Y/Z 给定的步长
#: （建模单位，即 mm）均匀取点；导出的 ASCII 文件是"表头 + 每个点一行 9 列
#: ``x y z Re1 Im1 Re2 Im2 Re3 Im3``"（服务器 2026-10-04 实测），正是
#: cst_results.parse_ascii_field 解析的格式。
#:
#: 官方例程用的就是这个组合（Dassault《Scripting the CST Studio Suite
#: with the Python》的场导出一节）。早先写的是 "FixedNumber"，那是
#: **采样点数**语义，却把 mm 步长填进去——语义矛盾，导出范围会随包围盒
#: 大小变，解析端拿到的网格也不是我们以为的那个。
ASCII_EXPORT_MODE = "FixedWidth"

#: 执行 ASCIIExport 的方法名（单独列出来，导出路径共用一份）。
ASCII_EXPORT_EXECUTE = "Execute"


def ascii_export_params(step_mm: float, step_z_mm: float | None = None,
                        mode: str = ASCII_EXPORT_MODE) -> list[tuple[str, str]]:
    """ASCIIExport 的设置序列 [(属性名, 值)]——**单一事实来源**。

    CST 2024 可用的属性集：Reset / FileName / Mode / StepX / StepY /
    StepZ / Execute（另有可选的 SetFileType）。**没有区域范围属性**——
    导出范围就是当前选中结果的整个包围盒（Volume 监视器 => 整个计算域），
    要限制范围只能在解析端裁剪（see cst_results.crop_grid）。

    StepX/Y/Z 是**每个轴上的采样步长**，单位 = 建模单位（mm）；面内取值来自
    ``CstSetup.resolve_export_steps``（缺省 = CaseConfig.field_export_step_mm）。
    ``step_z_mm`` 单列出来是因为 z 只影响采样面吸附、而点数按步长线性增长，
    功分器算例面内取 0.2、z 取 0.1（见 CstSetup.resolve_export_steps）。
    导出点数按步长的立方增长、范围是整个计算域（没有区域属性），改步长前
    先估一下体积：0.2 mm → 29 MB/份，0.1 mm → 约 225 MB/份。
    """
    s = f"{step_mm:g}"
    sz = s if step_z_mm is None else f"{float(step_z_mm):g}"
    return [("Mode", mode), ("StepX", s), ("StepY", s), ("StepZ", sz)]


# =========================================================================== #
# 二、布局常量与模板命令块
# =========================================================================== #
# ---- 布局常量（论文 Fig. 5；单位 mm，z=0 为基板顶面）----
# 材料/频点等物理常量来自 CST 侧单一事实来源 cst_setup（不要在别处再写一份）。
SUB_H, EPS_R, TAND = COUPLER.substrate_h_mm, COUPLER.eps_r, COUPLER.loss_tangent
METAL_T = COUPLER.metal_thickness_mm
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
FREQ = COUPLER.frequency_ghz
# 时域求解频段：0 ~ 2×f0。显式写进模板，避免"某次在 GUI 里手改过频段"
# 导致两个模板不一致（频段决定自适应网格与脉冲带宽，会影响 S 参数）。
FMIN, FMAX = 0.0, COUPLER.fmax_ghz


def arc_points(cx: float, cy: float, r: float, a0_deg: float, a1_deg: float,
               n: int = ARC_SEGS) -> list[tuple[float, float]]:
    """圆弧折线采样点（不含起点，便于与上一条边相接）。

    两个算例的布局模块共用（耦合器的腿/臂圆角、功分器的输出拐弯）。
    """
    angs = [math.radians(a0_deg + (a1_deg - a0_deg) * i / n)
            for i in range(1, n + 1)]
    return [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angs]


def _left_leg_polygon() -> list[tuple[float, float]]:
    """左腿轮廓（含外侧四分之一圆过渡，在设计区左界 x=0 处裁断）。"""
    cx, cy = LEG_L_IN + R_IN, ARM_LO - R_IN              # 拐弯中心 (0.4, -2.0)
    a_cross = math.degrees(math.acos((LEG_L_IN - cx) / R_OUT))   # 外弧与 x=0 的交角
    y_cross = cy + R_OUT * math.sin(math.radians(a_cross))       # 交点 y ≈ -0.04
    pts = [(LEG_L_OUT, LEG_BOT), (LEG_L_OUT, cy)]
    pts += arc_points(cx, cy, R_OUT, 180.0, a_cross)
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
            + arc_points(cx, cy, R_IN, 180.0, 90.0)
            + [(cx2, ARM_LO)]
            + arc_points(cx2, cy, R_IN, 90.0, 0.0)   # 末点即 (LEG_R_IN, cy)
            + [(LEG_R_IN, ARM_HI), (LEG_L_IN, ARM_HI)])


def _substrate_parts() -> list[str]:
    """基板 + 接地 + 空气盒（空气盒只用于撑大计算域）。"""
    return [
        material_normal("Rogers4350B", EPS_R, 1.0, TAND),
        brick("substrate", "component1", "Rogers4350B",
              THRU_X0, THRU_X1, LEG_BOT, SUB_TOP, -SUB_H, 0.0),
        brick("ground", "component1", "PEC",
              THRU_X0, THRU_X1, LEG_BOT, SUB_TOP, -SUB_H - METAL_T, -SUB_H),
        # z 从金属顶面起，避免与金属实体重叠
        brick("air", "component1", "Vacuum",
              THRU_X0, THRU_X1, LEG_BOT, SUB_TOP, METAL_T, AIR_H),
    ]


def _fixed_metal_parts() -> list[str]:
    """固定金属：直通线 + 两条腿（腿带外侧圆角过渡，与臂内侧圆角同心）。"""
    return [
        brick("thru_line", "feed", "PEC",
              THRU_X0, THRU_X1, THRU_LO, THRU_HI, 0.0, METAL_T),
        polygon_extrude("leg_left", "feed", "PEC",
                        _left_leg_polygon(), METAL_T),
        polygon_extrude("leg_right", "feed", "PEC",
                        _right_leg_polygon(), METAL_T),
    ]


def _arm_design_part() -> str:
    """设计区初始金属（耦合臂横段 + 两端内侧圆角，pipeline 每轮重建）。

    轮廓与 configs/coupler.yaml 的 initial_metal 一致（由测试锁定）。
    圆弧用折线近似（Extrude "Pointlist" 生成的是直边多边形）；弦高误差
    ~0.013 mm，远小于网格与最小间距。
    """
    return polygon_extrude("arm_init", DESIGN_COMPONENT, "PEC",
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
        waveguide_port(1, "p1", "xmin", THRU_X0,
                       THRU_LO - m, THRU_HI + m, pz0, pz1),
        waveguide_port(2, "p2", "xmax", THRU_X1,
                       THRU_LO - m, THRU_HI + m, pz0, pz1),
        waveguide_port(3, "p3", "ymin", LEG_BOT,
                       LEG_L_OUT - m, LEG_L_IN + m, pz0, pz1),
        waveguide_port(4, "p4", "ymin", LEG_BOT,
                       LEG_R_IN - m, LEG_R_OUT + m, pz0, pz1),
    ]


def model_blocks() -> list[str]:
    """全部**建模**命令块（顺序即执行顺序）：基板/接地/空气盒 → 固定金属
    → 设计区初始金属 → 4 个端口。"""
    return (_substrate_parts() + _fixed_metal_parts()
            + [_arm_design_part()] + _ports())


def setting_blocks(portnum: int) -> list[tuple[str, str]]:
    """全部**设置**类块 ``[(标签, 命令文本)]``：求解器/频段/监视器/边界。

    顺序与 template_blocks 一致（标签在前）。标签是 ASCII（进 History
    List 的标题列）。
    """
    return [
        # 网格生成器：GUI 打开时域求解器对话框时 CST 自己写的那一条
        # （录制宏里是独立一行），放最前面与录制顺序一致。
        ("Mesh", MESH_CREATOR),
        # 只激励本模板指定的端口（fwd→1 / bwd→3）。用 Solver 的
        # StimulationPort，不用 Excitation 对象（后者在 CST 2024 里
        # 报 10090，且失败静默：S 参数照样对，场却是多激励叠加的）。
        # 端口与模式成对给出（"1"/"1"）：实测 "1" + "All" 会让
        # Solver.Start 报 "Invalid stimulation port, please specify."。
        ("Solver", time_domain_solver_setup(str(portnum))),
        ("FrequencyRange", frequency_range(FMIN, FMAX)),
        ("Monitor Efield", field_monitor("Efield", FREQ)),
        ("Monitor Hfield", field_monitor("Hfield", FREQ)),
        ("Boundary", set_boundaries("magnetic", "magnetic", "magnetic",
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

    调用方逐块交给 ``model3d.add_to_history(标题, 命令文本)``。改几何只改
    这里（以及它调用的 VBA 生成器），三个 CST 程序不会有第二份定义。

    ``project`` 只是日志里用的名字，不进命令文本。
    """
    blocks = [("Units", UNITS_BLOCK)]
    blocks += [(block_header(c), c) for c in model_blocks()]
    blocks += setting_blocks(portnum)
    return blocks


def layout_view() -> dict:
    """模板侧几何的绘图数据（``scripts/plot_layout.py`` 用，不出 CST）。

    ``{"bounds": (x0, x1, y0, y1), "shapes": [(种类, 点列, 图例), ...],
    "ports": [(编号, x, y, 朝内 dx, 朝内 dy), ...]}``——种类 ∈
    ``{"substrate", "metal"}``，配色由 ``eaopt.plotting`` 定。两个算例的
    模板模块都实现它，画图脚本因此与算例无关（几何只有模板这一份来源）。
    """
    return {
        "bounds": (THRU_X0 - 1.5, THRU_X1 + 1.5, LEG_BOT - 1.0, SUB_TOP + 1.0),
        "shapes": [
            ("substrate", [(THRU_X0, LEG_BOT), (THRU_X1, LEG_BOT),
                           (THRU_X1, SUB_TOP), (THRU_X0, SUB_TOP)],
             "substrate"),
            ("metal", [(THRU_X0, THRU_LO), (THRU_X1, THRU_LO),
                       (THRU_X1, THRU_HI), (THRU_X0, THRU_HI)],
             "through line (p1-p2)"),
            ("metal", _left_leg_polygon(), "legs (p3-p4)"),
            ("metal", _right_leg_polygon(), None),
            ("metal", _arm_design_polygon(), "design metal (arm)"),
        ],
        "ports": [
            (1, THRU_X0, (THRU_LO + THRU_HI) / 2, 1, 0),
            (2, THRU_X1, (THRU_LO + THRU_HI) / 2, -1, 0),
            (3, (LEG_L_OUT + LEG_L_IN) / 2, LEG_BOT, 0, 1),
            (4, (LEG_R_OUT + LEG_R_IN) / 2, LEG_BOT, 0, 1),
        ],
    }
