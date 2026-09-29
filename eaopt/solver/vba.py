"""CST VBA 命令字符串生成（版本稳定的基础建模命令）。

供 build_cst_template 与 CstSolver 复用；纯字符串生成，本地可单测。
所有坐标单位 mm，z=0 为基板顶面（金属底面）。

约定：
  - 波导端口面垂直于 y 轴（端口定义在 y=const 平面，Xrange/Zrange
    给端口面范围）；Orientation: "negative"= 沿 −y 传播（端口面在
    结构上方），"positive"= 沿 +y（端口面在结构下方）；
  - 激励用 Excitation 对象（CST 2020+）；若服务器版本不支持，
    在 GUI 中手工勾选端口激励（见 smoke 脚本说明）。
"""

from __future__ import annotations

__all__ = [
    "material_normal", "brick", "polygon_extrude",
    "waveguide_port_yface", "excitation", "field_monitor",
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
    """多边形挤出实体（points: (N,2) 或 (N,3)，z 分量被忽略）。

    多边形定义在 z=z0 平面，向 +z 挤出 height_mm。
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    lines = [
        "With Polygon",
        "    .Reset",
        f"    .Name \"{name}_curve\"",
        f"    .Curve \"{name}_curve\"",
        f"    .Point \"{pts[0][0]:.6g}\", \"{pts[0][1]:.6g}\"",
    ]
    for x, y in pts[1:]:
        lines.append(f"    .LineTo \"{x:.6g}\", \"{y:.6g}\"")
    lines += ["    .Create", "End With", ""]
    lines += [
        "With Extrude",
        "    .Reset",
        f"    .Name \"{name}\"",
        f"    .Component \"{component}\"",
        f"    .Material \"{material}\"",
        f"    .Origin \"0.0\", \"0.0\", \"{z0}\"",
        '    .PlaneNormal "0", "0", "1"',
        f"    .Height \"{height_mm}\"",
        '    .Twist "0"',
        '    .Taper "0"',
        "    .Create",
        "End With",
        "",
    ]
    return "\n".join(lines) + "\n"


def waveguide_port_yface(port_number: int, name: str,
                         y: float, orientation: str,
                         x0: float, x1: float, z0: float, z1: float) -> str:
    """y=const 平面上的波导端口（单模）。

    orientation: "negative"（沿 −y 传播）/ "positive"（沿 +y）。
    """
    return (
        "With Port\n"
        "    .Reset\n"
        f"    .PortNumber \"{port_number}\"\n"
        f"    .Label \"{name}\"\n"
        '    .Folder ""\n'
        '    .NumberOfModes "1"\n'
        '    .AdjustPolarization "False"\n'
        '    .PolarizationAngle "0.0"\n'
        '    .ReferencePlaneDistance "0"\n'
        '    .TextSize "50"\n'
        '    .TextMaxLimit "1"\n'
        '    .Coordinates "Ranges"\n'
        f'    .Orientation "{orientation}"\n'
        '    .PortOnBound "False"\n'
        '    .ClipPickedPortToBound "False"\n'
        f'    .Xrange "{x0:.6g}", "{x1:.6g}"\n'
        f'    .Yrange "{y:.6g}", "{y:.6g}"\n'
        f'    .Zrange "{z0:.6g}", "{z1:.6g}"\n'
        "    .Create\n"
        "End With\n"
    )


def excitation(name: str, port: str) -> str:
    """端口激励（CST 2020+ 的 Excitation 对象，best-effort）。

    port: 已含引号的字面量（如 '"1"'）或 VBA 变量名（如 'portnum'）。
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
