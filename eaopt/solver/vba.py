"""CST VBA 命令字符串生成（版本稳定的基础建模命令）。

供 build_cst_template 与 CstSolver 复用；纯字符串生成，本地可单测。
所有坐标单位 mm，z=0 为基板顶面（金属底面）。

约定：
  - 波导端口面落在计算域边界面上（.Orientation 取边界面名，见
    waveguide_port 的 docstring）；
  - "只激励哪个端口"用 Solver 的 `.StimulationPort`（见
    time_domain_solver_setup）。**不要用 Excitation 对象**：它在
    CST 2024 命令宏上下文里实测报 "(10090) ActiveX Automation
    error"，且失败是静默的——S 参数照样对，但场监视器里存的是多个
    激励叠加的场，会悄悄毁掉伴随梯度；
  - 场导出用 ASCIIExport 的**固定属性集**（见 ascii_export_params），
    没有 XStart/XEnd 这类范围属性（实测不存在）；
  - 结果读取的候选 API 收敛在 eaopt/solver/cst_api.py（ResultTree
    的 GetResultItem/GetAllItems 在 CST 2024 实测不存在）。

编码：CST 宏文件按 ANSI 解码，**可执行语句里不要出现非 ASCII**
（字符串字面量中的非 ASCII 字节可能吞掉引号造成语法错误）；
中文只写在注释里（不影响解析）。guarded() 的 label 因此强制 ASCII。
"""

from __future__ import annotations

__all__ = [
    "material_normal", "brick", "polygon_extrude", "guarded",
    "guarded_alternatives",
    "DESIGN_COMPONENT", "delete_component", "design_region_update",
    "waveguide_port", "field_monitor", "field_monitor_name",
    "field_result_path", "FIELD_TYPES",
    "set_boundaries", "time_domain_solver_setup", "frequency_range",
    "MESH_CREATOR",
    "select_field_monitor",
    "ASCII_EXPORT_MODE", "ASCII_EXPORT_EXECUTE", "ascii_export_params",
    "ascii_export_field",
]


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


#: 设计区组件名。**模板（template_builder）与每轮重建（cst_driver）必须
#: 用同一个名字**——名字漂移的话每轮会新建一个组件，而上一轮的金属留在
#: 模型里，梯度就作用在一个"叠了两层金属"的模型上，且 S 参数照样出数。
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
    形状——这正是旧 COM 路径（直接调 ``Component.Delete``/``Extrude``）
    做不到、会让梯度作用在错模型上的地方。
    每轮历史表只增长一条，20 轮量级无压力。

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


def guarded(block: str, label: str, indent: str = "    ") -> str:
    """把"设置类"块包成失败不中断、错误汇总到 errLog 的形式。

    背景：模板宏里几何与端口必须成功（失败就该中止，模板不可用），
    但激励/监视器/边界/求解器这些设置块存在版本差异——实测
    `Excitation.Reset` 在 CST 2024 命令宏上下文报
    "(10090) ActiveX Automation error"，一旦抛出整个宏就中止，
    SaveAs 都不会执行。用本函数包住后：该块失败只记录到 errLog，
    宏继续跑完，结尾由 MsgBox 列出失败清单，用户在 GUI 里手工补。

    宏需在开头 `Dim errLog As String`，并把这一组块用
    `On Error Resume Next` / `On Error GoTo 0` 括起来。

    label 必须是 ASCII（见模块头部的编码说明）。
    """
    if not label.isascii():
        raise ValueError(f"label 必须为 ASCII（CST 宏按 ANSI 解码）：{label!r}")
    body = "".join(indent + ln + "\n"
                   for ln in block.rstrip("\n").split("\n"))
    return (
        f"{indent}Err.Clear\n"
        f"{body}"
        f"{indent}If Err.Number <> 0 Then\n"
        f'{indent}    errLog = errLog & "{label}: (" & Err.Number & ") " '
        f"& Err.Description & vbCrLf\n"
        f"{indent}    Err.Clear\n"
        f"{indent}End If\n"
    )


def guarded_alternatives(blocks, label: str, indent: str = "    ") -> str:
    """依次尝试同一命令的多种写法，第一个不报错的即采用。

    用于"参数个数/含义随版本变"的命令（如 SaveAs：CST 2024 命令宏里
    只给一个路径会报 "(10097) ActiveX Automation: wrong number of
    parameters"，需再给一个布尔）。全部写法都失败时，把最后一个错误
    记入 errLog（与 guarded() 同款报告），不抛出。
    """
    if not label.isascii():
        raise ValueError(f"label 必须为 ASCII（CST 宏按 ANSI 解码）：{label!r}")
    if not blocks:
        raise ValueError("blocks 不能为空")
    lines = [f"{indent}Err.Clear"]
    lvl = indent
    for block in blocks:
        for ln in block.rstrip("\n").split("\n"):
            lines.append(lvl + ln)
        lines.append(f"{lvl}If Err.Number <> 0 Then")
        lvl += indent
        lines.append(f"{lvl}Err.Clear")
    lines.append(f'{lvl}errLog = errLog & "{label}: (" & Err.Number & ") " '
                 f"& Err.Description & vbCrLf")
    lines.append(f"{lvl}Err.Clear")
    for _ in blocks:
        lvl = lvl[:-len(indent)]
        lines.append(f"{lvl}End If")
    return "\n".join(lines) + "\n"


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

    **这个名字同时决定结果树里的条目名**（"<name> [AC]"），导出场时
    要按条目名选中它，所以创建（本模块）与导出（cst.py / 脚本）必须
    共用本函数，不能各写一份——否则命名漂移会让场导出找不到条目。
    """
    if field_type not in FIELD_TYPES:
        raise ValueError(f"field_type 只能是 {tuple(FIELD_TYPES)}，"
                         f"收到 {field_type!r}")
    return f"{FIELD_TYPES[field_type][0]} (f={frequency_ghz:g})"


def field_result_path(field_type: str, frequency_ghz: float) -> str:
    """该监视器在结果树中的条目路径（SelectTreeItem / COM 用，单个反斜杠）。

    注意：这里是**路径本身**（Python/COM 用法）。要生成 VBA 源码里的
    SelectTreeItem 调用请用 select_field_monitor()——VBA 字符串里反斜杠
    不做转义，多写一层就会选中一个不存在的条目。
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
    上去了"。导出场时只按设计区附近的薄层取数（见 cst.py::_export_field）。

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
    命令宏里实测报 "(10090) ActiveX Automation error"，见 guarded()）。
    伴随法要求两个模板各自只激励一个端口（fwd→1，bwd→3）：否则场监视器
    存的是多个激励叠加的场，梯度就无从谈起（S 参数不受影响，所以光看
    S 参数发现不了这个问题）。

    stimulation_mode: 激励模式，"All" 或模式号字符串（单模端口即 "1"）。
    None → 与端口取同一值（"All"/"All" 或 "1"/"1"）。**端口与模式必须
    成对**：CST 2024 实测 `.StimulationPort "1"` + `.StimulationMode "All"`
    会让 Solver.Start 报 "Invalid stimulation port, please specify."；
    成对的写法见 Dassault 官方教程 "Scripting the CST Studio Suite with
    the Python"（TD-S 例：`.StimulationPort "1"` + `.StimulationMode "1"`）。

    属性集与顺序照 **GUI 录制宏**对齐（2026-10-01 用户实测录制）：除了我们
    原来自写的几项，录制结果里还有 CalculateModesOnly / SParaSymmetry /
    StoreTDResultsInCache / RunDiscretizerOnly / FullDeembedding /
    SuperimposePLWExcitation / UseSensitivityAnalysis——都是默认值，写上
    只为与 CST 自己的记录逐行对齐（便于对拍、也避免某台机器上默认值被
    GUI 手改过）。`.SteadyStateLimit "-40"` 取该版本 GUI 默认值（原写
    "-30" 是旧版默认）。
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


def select_field_monitor(field_type: str, frequency_ghz: float) -> str:
    """在结果树中选中监视器结果（ASCII 导出前必须执行）。

    VBA 字符串里反斜杠不做转义，故路径按原样写入（早期版本多写了一层
    反斜杠，会去选一个不存在的条目）。
    """
    return f'SelectTreeItem("{field_result_path(field_type, frequency_ghz)}")\n'


#: ASCIIExport 的 Mode 取值。**FixedWidth** = 按 StepX/Y/Z 给定的步长
#: （建模单位，即 mm）均匀取点；导出的 ASCII 文件头三行是
#: ``x0 x1 nx`` / ``y0 y1 ny`` / ``z0 z1 nz``，正是
#: ``ascii_fields.parse_ascii_field`` 解析的格式。
#:
#: 官方例程用的就是这个组合（Dassault《Scripting the CST Studio Suite
#: with the Python》的场导出一节：``.Mode "FixedWidth"`` + ``.StepX(0.5)``
#: + ``.SetFileType("hdf5")``）。早先我们写的是 "FixedNumber"，那是
#: **采样点数**语义，却把 mm 步长填进去——语义矛盾，导出范围会随包围盒
#: 大小变（同样"0.2"在大域上点数暴涨），解析端拿到的网格也不是我们以为
#: 的那个。改用 FixedWidth 后"步长 = mm"自洽。
ASCII_EXPORT_MODE = "FixedWidth"

#: 执行 ASCIIExport 的方法名（单独列出来，导出路径三处共用一份）。
ASCII_EXPORT_EXECUTE = "Execute"


def ascii_export_params(step_mm: float,
                        mode: str = ASCII_EXPORT_MODE) -> list[tuple[str, str]]:
    """ASCIIExport 的设置序列 [(属性名, 值)]——**单一事实来源**。

    vba.ascii_export_field（生成 VBA）、cst_results（逐条 COM 调用）与
    smoke 共用本函数，避免属性名再次漂移：早期版本写过 XStart/XEnd/
    YStart/YEnd/ZStart/ZEnd，CST 2024 实测报 `<unknown>.XStart`（这些
    属性不存在）。

    CST 2024 可用的属性集：Reset / FileName / Mode / StepX / StepY /
    StepZ / Execute（另有可选的 SetFileType）。**没有区域范围属性**——
    导出范围就是当前选中结果的整个包围盒（Volume 监视器 => 整个计算域），
    要限制范围只能在解析端裁剪（see cst_results.crop_grid）。

    StepX/Y/Z 是**每个轴上的采样步长**，单位 = 建模单位（mm）；取
    sampling.point_spacing_mm（0.2）量级即可——用网格步长 0.05 会把导出
    点数放大 64 倍（全计算域 GB 级），而边界采样点间距本来就是 0.2 mm。
    """
    s = f"{step_mm:g}"
    return [("Mode", mode), ("StepX", s), ("StepY", s), ("StepZ", s)]


def ascii_export_field(file_path: str, step_mm: float,
                       mode: str = ASCII_EXPORT_MODE) -> str:
    """把当前选中的 3D 场结果按固定步长导出为 ASCII（VBA 片段）。"""
    lines = ["With ASCIIExport", "    .Reset", f'    .FileName "{file_path}"']
    lines += [f'    .{prop} "{val}"' for prop, val in ascii_export_params(step_mm, mode)]
    lines += [f"    .{ASCII_EXPORT_EXECUTE}", "End With", ""]
    return "\n".join(lines)
