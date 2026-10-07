"""CST 侧模型文本（功分器算例，论文 III-B）：模板命令块 + 布局常量。

与 ``cst_model``（耦合器）**同构**：版本敏感的通用 VBA 片段（材质/长方体/
挤出/端口/监视器/边界/求解器）一律从 ``cst_model`` import，本模块只写三样
——布局常量、几何构造函数、命令块装配。求解器与三个 CST 程序只按
``template_blocks`` / ``model_blocks`` / ``setting_blocks`` / ``DESIGN_COMPONENT``
/ ``design_region_update`` 这套接口调用，算例由 ``solver/case.py`` 按
YAML 的 ``name`` 选模块。

论文 III-B 的几何（Fig.9 + 文字；单位 mm，z=0 为基板顶面）：

    设计区（红框）= 可动金属的活动范围，x∈[6.0,33.7] y∈[−9.0,9.0]；
      框内初始金属 = λg/2 长输入段 + 两根 λg/4 长分路臂（Y 形，线宽 w=2），
      臂以 θ=27.9° 张开，臂端中心线正好落在框右界上
    固定金属：输入馈线（端口 1 → 框左界）、两根输出线（框右界 → 水平段 →
      R=2.5 圆角拐弯 → 竖直段 → 板上下边缘 = 端口 2/3）
    基板 Rogers3003 30mil；接地 PEC；空气盒把计算域撑到端口面所需高度
    端口：1 在 xmin 面（输入馈线左端），2/3 在 ymax/ymin 面（竖直走线顶端）
    监视器：5 GHz E/H（Volume）；时域求解；边界 x/y/zmin 磁、zmax 电

论文唯一的几何约束——"to ensure the in-phase output of the two output ports,
constraints on the structure description are added so that the microstrip lines
are always connected"（两根输出线必须始终连着输入）——由两件事兑现：可动金属
在设计区左右界（x=6.0 / 33.7）处**必然穿出**（taper 让那里的速度趋零），
以及优化配置把三条馈线列进 ``fixed_region``（邻域冻结速度）。

与论文的两处**已知偏差**（常量都起了名字，随时可切回图上方案）：
  * 臂长取文字的 λg/4 = 9.61（图上量得 14.2，与 λ/4 标注自相矛盾）；
  * 板子 50×22 比论文的 65×30 紧凑（论文图纸与 Table I 的"有效面积 25×25"
    本身也对不上），S 参数看趋势与量级，不追求逐点吻合。
"""

from __future__ import annotations

import math

from eaopt.solver.cst_model import (
    DESIGN_COMPONENT, MESH_CREATOR, UNITS_BLOCK, arc_points, block_header,
    brick, field_monitor, frequency_range, material_normal, polygon_extrude,
    set_boundaries, time_domain_solver_setup, waveguide_port,
)
from eaopt.solver.cst_setup import DIVIDER

__all__ = [
    # 模板接口（与 cst_model 同名同义）
    "template_blocks", "model_blocks", "setting_blocks", "block_header",
    "UNITS_BLOCK", "DESIGN_COMPONENT", "layout_view",
    # 几何（模板与 YAML 的共同来源：测试按它们锁 YAML）
    "input_strip_polygon", "arm_polygon", "output_trace_polygon",
    "input_feed_polygon", "initial_metal_polygons", "fixed_metal_polygons",
    # 布局常量
    "W", "EPS_EFF", "LAMBDA_G", "L_IN", "L_ARM", "ARM_DX", "ARM_DY",
    "THETA_DEG", "FORK", "ARM_END", "BOX_X0", "BOX_X1", "BOX_Y0", "BOX_Y1",
    "BOARD_X", "BOARD_Y", "OUT_X0", "OUT_X1", "R_BEND", "BEND_CY", "V_X",
    "PORT_MARGIN", "SUBSTRATE", "ARC_SEGS",
    "SUB_H", "EPS_R", "TAND", "METAL_T", "AIR_H", "FREQ", "FMIN", "FMAX",
]


# =========================================================================== #
# 一、布局常量（论文 III-B；单位 mm，z=0 为基板顶面）
# =========================================================================== #
# 材料/频点/端口等物理常量来自 CST 侧单一事实来源 cst_setup.DIVIDER，
# 不在本模块另写一份。
SUB_H, EPS_R, TAND = DIVIDER.substrate_h_mm, DIVIDER.eps_r, DIVIDER.loss_tangent
METAL_T = DIVIDER.metal_thickness_mm
FREQ = DIVIDER.frequency_ghz
FMIN, FMAX = 0.0, DIVIDER.fmax_ghz      # 时域求解频段 0 ~ 2×f0
SUBSTRATE = "Rogers3003"

C0 = 299.792458             # mm·GHz
W = 2.0                     # 线宽（论文：所有线 w = 2 mm）
# 微带合成（w=2 / h=0.762 / εr=3.0）：Z0 ≈ 48.6 Ω（≈50 Ω 线宽），εeff ≈ 2.4326
EPS_EFF = 2.4326
LAMBDA_G = C0 / (FREQ * math.sqrt(EPS_EFF))         # 波导波长 38.44 mm @5 GHz

# ---- 设计区（= 红框 = 可动金属的活动范围；必须与 configs/divider.yaml 一致，
#      由 tests/test_cst_model_divider.py 锁定）----
L_IN = 19.2                 # 输入段长 = λg/2 = 19.22（取到 0.1 网格）
ARM_DX, ARM_DY = 8.5, 4.5   # 分路臂的水平投影 / 竖向抬升（图上量得的张角）
L_ARM = math.hypot(ARM_DX, ARM_DY)                  # 臂长 9.618 ≈ λg/4 = 9.61
THETA_DEG = math.degrees(math.atan2(ARM_DY, ARM_DX))    # 27.90°（图上 27.6°）

BOX_X0, BOX_Y0 = 6.0, -9.0
FORK = (BOX_X0 + L_IN, 0.0)             # 分叉点 (25.2, 0)
BOX_X1 = FORK[0] + ARM_DX               # 33.7：臂端中心线 = 设计区右界
BOX_Y1 = 9.0                            # 盒高 2× 输出线偏移（图上比例 ~2.1）
ARM_END = (BOX_X1, ARM_DY)              # 臂端中心 (33.7, 4.5)

# ---- 板 / 计算域（几何包围盒就是计算域，端口面落在它的边界面上）----
BOARD_X = (0.0, 50.0)       # 左端 = 端口 1 面；右侧留 5 mm 余量
BOARD_Y = (-11.0, 11.0)     # 上下边缘 = 端口 2/3 面（竖直走线走到这里）
AIR_H = 4.0                 # 空气盒顶 = 计算域 zmax（端口高 ≈ h + 5h，同耦合器）
PORT_MARGIN = 1.6           # 端口面横向半宽 = w/2 + 1.6（同耦合器）

# ---- 输出线（固定）：框右界 → 水平段 → 圆角 → 竖直段 → 板边缘 ----
OUT_X0 = BOX_X1             # 33.7（与可动臂端重叠，见 initial_metal 说明）
OUT_X1 = 41.5               # 水平段末端 = 拐弯起点
R_BEND = 2.5                # 拐弯中心线半径（外缘 3.5 / 内缘 1.5，同心）
BEND_CY = ARM_DY + R_BEND   # 7.0：拐弯中心 y（上侧；下侧取负）
V_X = OUT_X1 + R_BEND       # 44.0：竖直段中心线 x → 端口面中心
ARC_SEGS = 6                # 每段四分之一圆的折线段数（弦高误差 ~0.02 mm）


# =========================================================================== #
# 二、几何（模板与 YAML 的共同来源）
# =========================================================================== #
def _clip_x_max(poly, xmax: float) -> list[tuple[float, float]]:
    """把多边形按 ``x <= xmax`` 半平面裁断（Sutherland–Hodgman 单边裁剪）。

    分路臂的臂端中心线正好落在设计区右界上（``ARM_END[0] == BOX_X1``），
    不裁的话有半个臂端头伸到框外，扎进固定的输出线实体里（重叠）。
    设计区是"可动金属的活动范围"，框外那部分既不被 φ 描述、又是多余几何。
    裁剪结果里同时去掉重复点（点在裁剪线上时会重复发一次）。
    """
    out: list[tuple[float, float]] = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        a_in, b_in = a[0] <= xmax, b[0] <= xmax
        if a_in:
            out.append((float(a[0]), float(a[1])))
        if a_in != b_in:
            t = (xmax - a[0]) / (b[0] - a[0])
            out.append((xmax, float(a[1] + t * (b[1] - a[1]))))
    return [p for i, p in enumerate(out)
            if i == 0 or abs(p[0] - out[i - 1][0]) > 1e-9
            or abs(p[1] - out[i - 1][1]) > 1e-9]


def input_strip_polygon() -> list[tuple[float, float]]:
    """设计区内的输入段（**可动**）：从框左界直行到分叉点，宽 w。"""
    h = W / 2.0
    return [(BOX_X0, -h), (FORK[0], -h), (FORK[0], h), (BOX_X0, h)]


def arm_polygon(sign: float) -> list[tuple[float, float]]:
    """分路臂（**可动**）：``sign=+1`` 上臂 / ``-1`` 下臂，从分叉点到臂端。

    臂 = 分叉点 F 与臂端 E 之间、宽 w 的等宽条带（E 在框右界上），按框右界
    裁断：臂端因此是一条斜切口（(33.7, 4.5) 到 (33.7, 3.37)），它**整段落在
    固定输出线的 y 区间 [3.5, 5.5] 里**——这正是"可动金属始终与输出线连着"
    的那处重叠（CST 侧靠 close_open_contours 的 0.05 mm 外扩压实）。
    """
    s = 1.0 if sign > 0 else -1.0
    fx, fy = FORK
    ex, ey = ARM_END[0], s * ARM_END[1]
    dx, dy = ex - fx, ey - fy
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux                        # 单位法向（指向 s 一侧）
    h = W / 2.0
    quad = [(fx + h * nx, fy + h * ny), (ex + h * nx, ey + h * ny),
            (ex - h * nx, ey - h * ny), (fx - h * nx, fy - h * ny)]
    return _clip_x_max(quad, BOX_X1)


def input_feed_polygon() -> list[tuple[float, float]]:
    """输入馈线（**固定**）：端口 1（板左边缘）到设计区左界，宽 w。"""
    h = W / 2.0
    return [(BOARD_X[0], -h), (BOX_X0, -h), (BOX_X0, h), (BOARD_X[0], h)]


def output_trace_polygon(sign: float) -> list[tuple[float, float]]:
    """输出线（**固定**）：``sign=+1`` 上输出 / ``-1`` 下输出。

    从框右界起（y 中心 ±ARM_DY 与臂端对齐）水平走到 OUT_X1，经 R=R_BEND 的
    同心圆弧拐弯（外缘 R+w/2、内缘 R−w/2），再沿竖直段走到板边缘（端口面）。
    折线点序：外缘 → 拐弯 → 竖直段 → 拐弯 → 内缘返回，闭合边落在框右界上。
    """
    s = 1.0 if sign > 0 else -1.0
    h = W / 2.0
    cy = s * BEND_CY                        # 拐弯中心 (41.5, ±7.0)
    y_out = cy - s * (R_BEND + h)           # 水平段外缘 y（远离拐弯中心）
    y_in = cy - s * (R_BEND - h)            # 水平段内缘 y
    x_out = OUT_X1 + R_BEND + h             # 竖直段外缘 x（45）
    x_in = OUT_X1 + R_BEND - h              # 竖直段内缘 x（43）
    y_edge = s * BOARD_Y[1]                 # 竖直段末端 = 板边缘（端口面）
    a_out = (-90.0 * s, 0.0)                # 外缘弧角：s=+1 (−90°→0°)，镜像取正
    a_in = (0.0, -90.0 * s)
    return [
        (OUT_X0, y_out),                    # 框右界（外缘）
        (OUT_X1, y_out),                    # 水平段 → 拐弯起点
        *arc_points(OUT_X1, cy, R_BEND + h, *a_out),    # 外圆角 → (x_out, cy)
        (x_out, y_edge),                    # 竖直段外缘 → 板边缘
        (x_in, y_edge),                     # 板边缘 → 竖直段内缘
        (x_in, cy),                         # 竖直段内缘 → 内圆角起点
        *arc_points(OUT_X1, cy, R_BEND - h, *a_in),     # 内圆角 → (OUT_X1, y_in)
        (OUT_X0, y_in),                     # 水平段内缘 → 回框右界
    ]


def initial_metal_polygons() -> list[list[tuple[float, float]]]:
    """设计区初始金属 = 输入段 ∪ 上臂 ∪ 下臂（Y 形，三个多边形取并集）。

    ``LevelSet2D.init_from_polygons`` 对多个多边形取距离最小、内部取或，
    所以交集处自动连成一体，不用手搓单条外轮廓；CST 侧也是三个挤出实体
    （重叠的同类材料实体，CST 自己合并）。
    **必须与 configs/divider.yaml 的 initial_metal 逐点一致**（测试锁定）。
    """
    return [input_strip_polygon(), arm_polygon(+1.0), arm_polygon(-1.0)]


def fixed_metal_polygons() -> list[list[tuple[float, float]]]:
    """固定金属（= optimizer 的 ``fixed_region``）：输入馈线 + 两根输出线。"""
    return [input_feed_polygon(), output_trace_polygon(+1.0),
            output_trace_polygon(-1.0)]


# =========================================================================== #
# 三、模板命令块
# =========================================================================== #
def _substrate_parts() -> list[str]:
    """基板 + 接地 + 空气盒（空气盒只用于撑大计算域）。"""
    x0, x1 = BOARD_X
    y0, y1 = BOARD_Y
    return [
        material_normal(SUBSTRATE, EPS_R, 1.0, TAND),
        brick("substrate", "component1", SUBSTRATE, x0, x1, y0, y1, -SUB_H, 0.0),
        brick("ground", "component1", "PEC", x0, x1, y0, y1,
              -SUB_H - METAL_T, -SUB_H),
        # z 从金属顶面起，避免与金属实体重叠
        brick("air", "component1", "Vacuum", x0, x1, y0, y1, METAL_T, AIR_H),
    ]


def _fixed_metal_parts() -> list[str]:
    """固定金属：输入馈线（长方体）+ 两根输出线（带圆角的挤出多边形）。

    输出线用挤出而不是长方体，是因为拐弯处是圆弧（与耦合器同款做法）。
    """
    x0, x1 = BOARD_X
    h = W / 2.0
    return [
        brick("in_feed", "feed", "PEC", x0, BOX_X0, -h, h, 0.0, METAL_T),
        polygon_extrude("out_top", "feed", "PEC",
                        output_trace_polygon(+1.0), METAL_T),
        polygon_extrude("out_bot", "feed", "PEC",
                        output_trace_polygon(-1.0), METAL_T),
    ]


def _design_parts() -> list[str]:
    """设计区初始金属（Y 形，pipeline 每轮按 φ 重建整块设计区）。"""
    names = ("in_strip", "arm_top", "arm_bot")
    polys = initial_metal_polygons()
    return [polygon_extrude(n, DESIGN_COMPONENT, "PEC", p, METAL_T)
            for n, p in zip(names, polys)]


def _ports() -> list[str]:
    """3 个波导端口：1 在 xmin 面，2/3 在 ymax/ymin 面（按优化目标激励 1/2）。

    端口面 = 接地板底面（域 zmin）到空气盒顶（域 zmax）的矩形——下缘必须
    贴合接地参考面（CST 微带端口要求），横向半宽 = w/2 + 1.6（同耦合器）。
    face 用 CST 的边界面名（xmin/ymax/ymin）——``.Orientation`` 只认这组。
    """
    pz0, pz1 = -SUB_H - METAL_T, AIR_H
    m = PORT_MARGIN
    h = W / 2.0
    return [
        waveguide_port(1, "p1", "xmin", BOARD_X[0], -h - m, h + m, pz0, pz1),
        waveguide_port(2, "p2", "ymax", BOARD_Y[1],
                       V_X - h - m, V_X + h + m, pz0, pz1),
        waveguide_port(3, "p3", "ymin", BOARD_Y[0],
                       V_X - h - m, V_X + h + m, pz0, pz1),
    ]


def model_blocks() -> list[str]:
    """全部**建模**命令块（顺序即执行顺序）：基板/接地/空气盒 → 固定金属
    → 设计区初始金属 → 3 个端口。"""
    return (_substrate_parts() + _fixed_metal_parts()
            + _design_parts() + _ports())


def setting_blocks(portnum: int) -> list[tuple[str, str]]:
    """全部**设置**类块 ``[(标签, 命令文本)]``：求解器/频段/监视器/边界。

    与耦合器逐条同款（只有激励端口号随算例变），顺序与 template_blocks 一致。
    """
    if int(portnum) not in DIVIDER.ports:
        raise ValueError(
            f"激励端口 {portnum} 不在模板建的端口 {DIVIDER.ports} 里"
            "（fwd 激励 objective.from_port、bwd 激励 to_port）")
    return [
        # 网格生成器：GUI 打开时域求解器对话框时 CST 自己写的那一条
        ("Mesh", MESH_CREATOR),
        # 只激励本模板指定的端口（fwd→1 / bwd→2）。用 Solver 的
        # StimulationPort，不用 Excitation 对象（后者在 CST 2024 里报
        # 10090，且失败静默：S 参数照样对，场却是多激励叠加的）。
        ("Solver", time_domain_solver_setup(str(portnum))),
        ("FrequencyRange", frequency_range(FMIN, FMAX)),
        ("Monitor Efield", field_monitor("Efield", FREQ)),
        ("Monitor Hfield", field_monitor("Hfield", FREQ)),
        ("Boundary", set_boundaries("magnetic", "magnetic", "magnetic",
                                    "magnetic", "magnetic", "electric")),
    ]


def template_blocks(project: str, portnum: int) -> list[tuple[str, str]]:
    """模板的全部命令块 ``[(标题, VBA 命令文本)]`` —— **单一事实来源**。

    调用方逐块交给 ``model3d.add_to_history(标题, 命令文本)``。``project``
    只是日志里用的名字，不进命令文本。
    """
    blocks = [("Units", UNITS_BLOCK)]
    blocks += [(block_header(c), c) for c in model_blocks()]
    blocks += setting_blocks(portnum)
    return blocks


def layout_view() -> dict:
    """模板侧几何的绘图数据（``scripts/plot_layout.py`` 用，不出 CST）。

    结构同 ``cst_model.layout_view``（两个算例同一套接口）。这里画的是
    **模板自己的**几何：板 → 两条固定馈线 → Y 形设计金属 → 3 个端口，
    与 ``cst_model_divider`` 生成的 VBA 是同一批构造函数。
    """
    x0, x1 = BOARD_X
    y0, y1 = BOARD_Y
    return {
        "bounds": (x0 - 1.0, x1 + 1.0, y0 - 1.0, y1 + 1.0),
        "shapes": [
            ("substrate", [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
             "substrate"),
            ("metal", input_feed_polygon(), "input feed (p1)"),
            ("metal", output_trace_polygon(+1.0), "output traces (p2/p3)"),
            ("metal", output_trace_polygon(-1.0), None),
            ("metal", input_strip_polygon(), "design metal (Y)"),
            ("metal", arm_polygon(+1.0), None),
            ("metal", arm_polygon(-1.0), None),
        ],
        "ports": [
            (1, x0, 0.0, 1, 0),                 # 输入：板左边缘
            (2, V_X, y1, 0, -1),                # 上输出：板上下边缘
            (3, V_X, y0, 0, 1),
        ],
    }
