"""CST 双模板宏（.bas）生成（本地可单测，无需 CST）。

几何布局（单位 mm，z=0 为基板顶面，与 configs/coupler.yaml 一致）：
    直通线 y∈[1.0,2.6]（固定）：水平段 x∈[−2,14] + 两端竖桩 y∈[1,4.5]
    耦合臂 y∈[−1.6,0]（设计区 x∈[0,12] 可动；两端馈线固定）：
        水平馈线段 x∈[−2,0] 与 x∈[12,14] + 竖桩 y∈[−4,0]
    基板 Rogers4350B 30mil：x∈[−4,16] y∈[−6,6] z∈[−0.762,0]
    接地 PEC：z∈[−0.797,−0.762]
    端口 1/2 在直通线竖桩顶（y=4.5，沿 −y），端口 3/4 在耦合臂
    竖桩底（y=−4，沿 +y）
    边界：x/y/zmin 磁边界，zmax 电边界（论文设定）
    监视器：5 GHz E/H 场；求解器：时域 TD-S

服务器步骤见 scripts/build_cst_template.py 头部注释。
"""

from __future__ import annotations

from pathlib import Path

from eaopt.solver import vba as V

# ---- 布局常量（与 configs/coupler.yaml 对应）----
SUB_H, EPS_R, TAND = 0.762, 3.66, 0.0037
METAL_T = 0.035
X0, X1 = 0.0, 12.0          # 设计区 x 范围
THRU_LO, THRU_HI = 1.0, 2.6  # 直通线 y
ARM_LO, ARM_HI = -1.6, 0.0   # 耦合臂 y
FEED_EXT = 2.0               # 设计区外馈线水平延伸长度
STUB_LO, STUB_HI = -4.0, 4.5  # 竖桩端 y（端口面位置）
STUB_W = 1.6                 # 竖桩宽（x）
AIR_X = (-4.0, 16.0)
AIR_Y = (-6.0, 6.0)
AIR_Z = (-1.2, 3.0)
FREQ = 5.0


def _through_line_parts() -> list[str]:
    parts = [
        V.brick("thru_main", "feed", "PEC",
                X0 - FEED_EXT, X1 + FEED_EXT, THRU_LO, THRU_HI, 0.0, METAL_T),
    ]
    for xa, xb in ((X0 - FEED_EXT, X0 - FEED_EXT + STUB_W),
                   (X1 + FEED_EXT - STUB_W, X1 + FEED_EXT)):
        parts.append(V.brick(f"thru_stub_{xa}", "feed", "PEC",
                             xa, xb, THRU_LO, STUB_HI, 0.0, METAL_T))
    return parts


def _arm_feed_parts() -> list[str]:
    parts = []
    for xa, xb in ((X0 - FEED_EXT, X0), (X1, X1 + FEED_EXT)):
        parts.append(V.brick(f"arm_feed_{xa}", "feed", "PEC",
                             xa, xb, ARM_LO, ARM_HI, 0.0, METAL_T))
    for xa, xb in ((X0 - FEED_EXT, X0 - FEED_EXT + STUB_W),
                   (X1 + FEED_EXT - STUB_W, X1 + FEED_EXT)):
        parts.append(V.brick(f"arm_stub_{xa}", "feed", "PEC",
                             xa, xb, STUB_LO, ARM_HI, 0.0, METAL_T))
    return parts


def _arm_design_part() -> str:
    """设计区初始金属（耦合臂段，pipeline 每轮重建）。"""
    return V.polygon_extrude("arm_init", "design_region", "PEC",
                             [(X0, ARM_LO), (X1, ARM_LO),
                              (X1, ARM_HI), (X0, ARM_HI)],
                             METAL_T)


def _ports() -> list[str]:
    """4 个波导端口（y=const 面，端口面半宽 2.4 ≈ 3×线宽/2 + 余量）。"""
    pz0, pz1 = -SUB_H, 2.0
    pxw = 2.4
    thru_cx = X0 - FEED_EXT + STUB_W / 2.0
    arm_cx = X0 - FEED_EXT + STUB_W / 2.0
    thru_cx2 = X1 + FEED_EXT - STUB_W / 2.0
    return [
        V.waveguide_port_yface(1, "p1", STUB_HI, "negative",
                               thru_cx - pxw, thru_cx + pxw, pz0, pz1),
        V.waveguide_port_yface(2, "p2", STUB_HI, "negative",
                               thru_cx2 - pxw, thru_cx2 + pxw, pz0, pz1),
        V.waveguide_port_yface(3, "p3", STUB_LO, "positive",
                               arm_cx - pxw, arm_cx + pxw, pz0, pz1),
        V.waveguide_port_yface(4, "p4", STUB_LO, "positive",
                               thru_cx2 - pxw, thru_cx2 + pxw, pz0, pz1),
    ]


def build_macro(outdir: Path) -> Path:
    """生成 build_templates.bas（含 Main + BuildProject(fname, portnum)）。"""
    outdir = outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    fwd = (outdir / "coupler_fwd.cst").as_posix()
    bwd = (outdir / "coupler_bwd.cst").as_posix()

    body = [
        "Sub Main()",
        f'    BuildProject "{fwd}", 1',
        f'    BuildProject "{bwd}", 3',
        "End Sub",
        "",
        "Sub BuildProject(fname As String, portnum As Integer)",
        "    NewProject",
        "    With Units",
        '        .Geometry "mm"',
        '        .Frequency "GHz"',
        '        .Time "ns"',
        "    End With",
        V.material_normal("Rogers4350B", EPS_R, 1.0, TAND),
        V.brick("substrate", "component1", "Rogers4350B",
                AIR_X[0], AIR_X[1], AIR_Y[0], AIR_Y[1], -SUB_H, 0.0),
        V.brick("ground", "component1", "PEC",
                AIR_X[0], AIR_X[1], AIR_Y[0], AIR_Y[1], -SUB_H - METAL_T, -SUB_H),
    ]
    body += _through_line_parts()
    body += _arm_feed_parts()
    body.append(_arm_design_part())
    body += _ports()
    body += [
        V.excitation("excitation1", "portnum"),
        V.field_monitor("e5", "Efield", FREQ),
        V.field_monitor("h5", "Hfield", FREQ),
        V.set_boundaries("magnetic", "magnetic", "magnetic", "magnetic",
                         "magnetic", "electric"),
        V.time_domain_solver_setup(),
        "    SaveAs fname",
        "End Sub",
    ]
    path = outdir / "build_templates.bas"
    path.write_text("\n".join(body), encoding="utf-8")
    return path
