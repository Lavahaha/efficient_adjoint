"""CST VBA 命令字符串生成（版本稳定的基础建模命令）。

供 build_cst_template 与 CstSolver 复用；纯字符串生成，本地可单测。
所有坐标单位 mm，z=0 为基板顶面（金属底面）。

约定：
  - 波导端口面落在计算域边界面上（.Orientation 取边界面名，见
    waveguide_port 的 docstring）；
  - 激励用 Excitation 对象（CST 2020+）；若服务器版本不支持，
    用 guarded() 包住即可让宏不中断，并在结尾报告哪个块失败，
    再按提示在 GUI 中手工勾选端口激励。

编码：CST 宏文件按 ANSI 解码，**可执行语句里不要出现非 ASCII**
（字符串字面量中的非 ASCII 字节可能吞掉引号造成语法错误）；
中文只写在注释里（不影响解析）。guarded() 的 label 因此强制 ASCII。
"""

from __future__ import annotations

__all__ = [
    "material_normal", "brick", "polygon_extrude", "guarded",
    "waveguide_port", "excitation", "field_monitor",
    "set_boundaries", "time_domain_solver_setup", "select_field_monitor",
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
        "    .Create\n"
        "End With\n"
    )


def excitation(name: str, port: str) -> str:
    """端口激励（CST 2020+ 的 Excitation 对象，best-effort）。

    port: 已含引号的字面量（如 '"1"'）或 VBA 变量名（如 'portnum'）。

    **实测 CST 2024 上 `.Reset` 会报 "(10090) ActiveX Automation error"**
    （见 guarded()），故模板宏里必须用 guarded() 包住；失败时按
    结尾报告在端口对话框中手工勾选激励。
    """
    return (
        "With Excitation\n"
        "    .Reset\n"
        f"    .Name \"{name}\"\n"
        f"    .Port {port}\n"
        '    .ModeIndex "1"\n'
        "    .Create\n"
        "End With\n"
    )


def field_monitor(name: str, field_type: str, frequency_ghz: float,
                  subvolume: bool = False) -> str:
    """频域场监视器。field_type: "Efield" / "Hfield"。"""
    return (
        "With Monitor\n"
        "    .Reset\n"
        f"    .Name \"{name}\"\n"
        '    .Dimension "Volume"\n'
        '    .Domain "Frequency"\n'
        f'    .FieldType "{field_type}"\n'
        f'    .MonitorValue "{frequency_ghz}"\n'
        f'    .UseSubvolume "{"True" if subvolume else "False"}"\n'
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


def time_domain_solver_setup() -> str:
    """时域求解器设置（论文用法，默认精度）。"""
    return (
        "With Solver\n"
        '    .Method "Hexahedral"\n'
        '    .CalculationType "TD-S"\n'
        '    .StimulationPort "All"\n'
        '    .StimulationMode "All"\n'
        '    .SteadyStateLimit "-30"\n'
        '    .MeshAdaption "False"\n'
        '    .AutoNormImpedance "False"\n'
        '    .NormingImpedance "50"\n'
        "End With\n"
    )


def select_field_monitor(field_type: str, frequency_ghz: float) -> str:
    """在结果树中选中监视器结果（ASCII 导出前必须执行）。"""
    return f'SelectTreeItem("2D/3D Results\\\\{field_type}-Field\\\\'
    f'{field_type.lower()}-field (f={frequency_ghz}) [AC]")\n'


def ascii_export_field(file_path: str, step_mm: float,
                       x0: float, x1: float, y0: float, y1: float,
                       z0: float, z1: float) -> str:
    """把当前选中的 3D 场结果按固定步长导出为 ASCII 文件。"""
    return (
        "With ASCIIExport\n"
        "    .Reset\n"
        f"    .FileName \"{file_path}\"\n"
        '    .Mode "FixedNumber"\n'
        f"    .StepX \"{step_mm}\"\n"
        f"    .StepY \"{step_mm}\"\n"
        f"    .StepZ \"{step_mm}\"\n"
        f"    .XStart \"{x0}\"\n"
        f"    .XEnd \"{x1}\"\n"
        f"    .YStart \"{y0}\"\n"
        f"    .YEnd \"{y1}\"\n"
        f"    .ZStart \"{z0}\"\n"
        f"    .ZEnd \"{z1}\"\n"
        "    .Export\n"
        "End With\n"
    )
