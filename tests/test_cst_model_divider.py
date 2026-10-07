"""功分器算例（论文 III-B）：模板几何 + YAML 一致性 + 离线几何自检。

耦合器那一套在 ``tests/test_cst_model.py``（VBA 片段的行为、模板不变量）。
本文件锁的是"换一个算例"新增的三类东西：

  * **布局常量与论文的关系**：输入段 = λg/2、分路臂 = λg/4、张角 27.9°；
    可动金属在设计区左右界**穿出去**接固定馈线（论文唯一那条几何约束
    "microstrip lines are always connected"）；
  * **YAML 与模板是同一份几何**：设计区盒子、初始金属三个多边形、固定区
    三条馈线逐点一致——两处漂移的话 iter_000 的 CST 结构与水准集表示
    不是同一个形状，而不报错；
  * **离线几何自检**（不需要 CST）：初始 φ 单连通、三个穿出锚点都在、
    轮廓闭合后仍是同一块金属（**V 形缺口不能被填死**）。

数值（格数、锚点跨度、余量）都是本算例几何的确定值，改布局常量时这些
测试会一起变红——那是好事，说明它们真的在看几何。
"""

import dataclasses
import math
from pathlib import Path

import numpy as np
import pytest
from matplotlib.path import Path as MplPath
from scipy import ndimage

from eaopt.config import CaseConfig
from eaopt.geometry.contour import close_open_contours
from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import apply_min_gap, build_velocity_mask
from eaopt.pipeline import make_level_set, movable_contours
from eaopt.solver import cst_model as COUPLER_MODEL
from eaopt.solver import cst_model_divider as D
from eaopt.solver.case import known_cases, load_case
from eaopt.solver.cst_setup import COUPLER, DIVIDER

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "configs" / "divider.yaml"


@pytest.fixture(scope="module")
def cfg() -> CaseConfig:
    return CaseConfig.from_yaml(CONFIG)


@pytest.fixture(scope="module")
def ls(cfg) -> LevelSet2D:
    return make_level_set(cfg)


def _at_edge(poly, x_edge: float) -> list[float]:
    """多边形在竖直线 x = x_edge 上的顶点 y（本算例的切口都是竖直段）。"""
    return sorted(y for x, y in poly if abs(x - x_edge) < 1e-9)


# =========================================================================== #
# 一、布局常量（论文 III-B：λ/2 输入段 + λ/4 分路臂）
# =========================================================================== #
def test_layout_follows_the_paper_lambda_rule():
    """论文文字："a λ/2 long input waveguide and two λ/4 long splitter
    waveguides"——两个长度由 λg 定，与线宽 w=2 一起是布局的全部自由参数。"""
    assert D.W == 2.0                                            # 所有线同宽
    assert D.L_IN == pytest.approx(D.LAMBDA_G / 2, abs=0.05)     # 19.2 vs 19.22
    assert D.L_ARM == pytest.approx(D.LAMBDA_G / 4, abs=0.02)    # 9.618 vs 9.611
    assert D.THETA_DEG == pytest.approx(27.9, abs=0.2)           # 图上量得 27.6°
    # 臂长与张角自洽：dh = L·cosθ、dv = L·sinθ
    assert D.ARM_DX == pytest.approx(D.L_ARM * math.cos(math.radians(D.THETA_DEG)))
    assert D.ARM_DY == pytest.approx(D.L_ARM * math.sin(math.radians(D.THETA_DEG)))


def test_design_box_derives_from_the_layout():
    """设计区（红框）= 输入段 + 臂水平投影 的 x 范围，输出线偏移的 2 倍做 y。"""
    assert D.BOX_X0 + D.L_IN == pytest.approx(D.FORK[0])
    assert D.FORK[0] + D.ARM_DX == pytest.approx(D.BOX_X1)
    assert D.ARM_END == pytest.approx((D.BOX_X1, D.ARM_DY))
    assert (D.BOX_Y0, D.BOX_Y1) == (-9.0, 9.0)
    assert D.BOX_Y1 == pytest.approx(2.0 * D.ARM_DY)     # 上下各留一倍偏移


def test_movable_metal_crosses_the_box_edges():
    """论文唯一的几何约束："to ensure the in-phase output of the two output
    ports, ... the microstrip lines are always connected"——兑现处就是可动
    金属在左右界**穿出去**接固定馈线：

    * 输入段左端压在框左界上（外面是固定输入馈线）；
    * 两臂端压在框右界上，臂端斜切口与固定输出线的断面**重叠 w/2**。
    """
    strip = D.input_strip_polygon()
    assert min(x for x, _ in strip) == pytest.approx(D.BOX_X0)
    assert max(x for x, _ in strip) == pytest.approx(D.FORK[0])

    for sign in (+1, -1):
        # 都取 sign 侧的 y（下臂镜像成正值）再排序，两个分支的比较才同向
        lo, hi = sorted(sign * y for y in _at_edge(D.arm_polygon(sign), D.BOX_X1))
        assert hi == pytest.approx(D.ARM_DY, abs=1e-9)         # 上到臂端中心线
        assert lo < D.ARM_DY                                   # 下探到中心线以下
        trace_lo, trace_hi = sorted(sign * y for y in
                                    _at_edge(D.output_trace_polygon(sign),
                                             D.OUT_X0))
        overlap = min(hi, trace_hi) - max(lo, trace_lo)
        assert overlap >= D.W / 2 - 1e-9                       # 重叠 = 连通
        assert overlap <= D.W / 2 + 1e-9                       # 但也不多咬


def test_arm_is_clipped_to_the_design_box():
    """设计区是"可动金属的活动范围"：臂端中心线正好落在框右界上，不裁的话
    有半个臂端头伸到框外、扎进固定的输出线实体里（φ 描述不到那块几何）。"""
    for sign in (+1, -1):
        arm = D.arm_polygon(sign)
        assert len(arm) == 5                              # 4 个角 + 1 个裁剪交点
        assert max(x for x, _ in arm) == pytest.approx(D.BOX_X1)
        assert all(D.BOX_X0 <= x <= D.BOX_X1 for x, _ in arm)


def test_output_trace_is_an_equal_width_strip_with_concentric_bend():
    """输出线：等宽条带 + 同心圆角，竖直段中心线正好落在端口面中心上。"""
    for sign in (+1, -1):
        pts = D.output_trace_polygon(sign)
        cx, cy = D.OUT_X1, sign * D.BEND_CY                   # 拐弯中心
        r = [math.hypot(x - cx, y - cy) for x, y in pts]
        # 拐弯里没有点越过内缘，且所有"靠里"的点都正好落在两条同心弧上
        assert min(r) == pytest.approx(D.R_BEND - D.W / 2)
        near = {round(v, 6) for v in r if v <= D.R_BEND + D.W / 2 + 1e-9}
        assert near == {round(D.R_BEND - D.W / 2, 6),
                        round(D.R_BEND + D.W / 2, 6)}
        # 竖直段两条边：宽度 = w，中心线 = 端口面中心（板边缘上）
        xs = {round(x, 9) for x, y in pts if abs(y) == pytest.approx(D.BOARD_Y[1])}
        assert xs == {round(D.V_X - D.W / 2, 9), round(D.V_X + D.W / 2, 9)}
        assert D.V_X == pytest.approx(D.OUT_X1 + D.R_BEND)
        # 水平段两条边 = 中心线 ± w/2（与臂端对齐）
        ys = {round(y, 9) for x, y in pts if abs(x - D.OUT_X0) < 1e-9}
        assert ys == {round(sign * (D.ARM_DY - D.W / 2), 9),
                      round(sign * (D.ARM_DY + D.W / 2), 9)}


# =========================================================================== #
# 二、模板命令块
# =========================================================================== #
def _text(portnum: int) -> str:
    return "\n".join(cmd for _, cmd in D.template_blocks("divider", portnum))


def test_blocks_contain_the_full_model():
    """几何 + 端口 + 边界 + 求解器设置，一个都不能少。"""
    text = _text(1)
    assert "Rogers3003" in text                                # 论文基板
    assert text.count("With Brick") == 4                       # 基板/接地/空气/输入馈线
    assert text.count("With Extrude") == 5                     # 2 输出线 + 3 设计区金属
    assert text.count('.Component "design_region"') == 3
    for name in ("in_strip", "arm_top", "arm_bot", "out_top", "out_bot",
                 "in_feed"):
        assert f'.Name "{name}"' in text
    # 3 个端口：1 在 xmin 面（输入），2/3 在 ymax/ymin 面（输出）
    assert text.count("With Port") == 3
    assert text.count('.Orientation "xmin"') == 1
    assert text.count('.Orientation "ymax"') == 1
    assert text.count('.Orientation "ymin"') == 1
    # 端口面：下缘贴接地板底面（域 zmin）到空气盒顶（域 zmax）
    assert text.count('.Zrange "-0.797", "4"') == 3
    # 边界：zmax 电、其余磁；求解器/频段/监视器与耦合器同一套
    assert '.Zmax "electric"' in text and text.count('"magnetic"') == 5
    assert 'Solver.FrequencyRange "0", "10"' in text
    assert text.count("With Monitor") == 2
    assert text.count('.MonitorValue "5"') == 2
    assert "Mesh.SetCreator" in text
    assert "With Excitation" not in text            # 激励走 StimulationPort
    text.encode("ascii")                            # VBA 按 ANSI 解码


def test_every_project_gets_the_same_model_but_its_own_stimulus():
    """双模板的硬不变量：只差激励端口（fwd→1 输入，bwd→2 观测输出）。"""
    fwd, bwd = _text(1), _text(2)
    assert fwd.replace('.StimulationPort "1"', ".StimulationPort X") == \
        bwd.replace('.StimulationPort "2"', ".StimulationPort X")
    # 端口与模式必须成对（"2"/"All" 会让 Solver.Start 报 "Invalid
    # stimulation port"）：单模端口一律模式 "1"。
    assert fwd.count('.StimulationMode "1"') == 1
    assert bwd.count('.StimulationMode "1"') == 1
    assert '.StimulationMode "All"' not in fwd + bwd


def test_template_blocks_start_with_units_and_keep_model_order():
    blocks = D.template_blocks("divider_fwd", 1)
    mb = D.model_blocks()
    assert blocks[0] == ("Units", D.UNITS_BLOCK)
    assert blocks[1:1 + len(mb)] == [(D.block_header(c), c) for c in mb]
    assert D.block_header(D.UNITS_BLOCK) == "Units"


def test_setting_blocks_reject_a_port_outside_the_template():
    """端口号拼错要当场报错（静默建出"激励了不存在的端口"的工程最危险）。"""
    for bad in (0, 4, 99):
        with pytest.raises(ValueError, match="不在模板建的端口"):
            D.setting_blocks(bad)


# =========================================================================== #
# 三、YAML ↔ 模板
# =========================================================================== #
def test_config_design_box_agrees_with_template(cfg):
    box = cfg.design_region.box
    assert list(box.x) == [D.BOX_X0, D.BOX_X1]
    assert list(box.y) == [D.BOX_Y0, D.BOX_Y1]
    assert cfg.design_region.grid_step_mm == 0.1
    assert cfg.constraints.allowed_region is None       # 两域合一（同耦合器）
    assert cfg.constraints.taper_edges == "x"           # 只在穿出馈线那两条边 taper
    assert cfg.objective.from_port == 1 and cfg.objective.to_port == 2


def test_config_initial_metal_matches_template(cfg):
    """iter_000 的 CST 结构与水准集表示必须是同一块金属。"""
    yaml_polys = [p.vertices for p in cfg.initial_metal]
    code_polys = D.initial_metal_polygons()
    assert len(yaml_polys) == len(code_polys) == 3     # 输入段 + 上臂 + 下臂
    for yp, cp in zip(yaml_polys, code_polys):
        assert len(yp) == len(cp)
        assert np.allclose(np.asarray(yp, float), np.asarray(cp, float),
                           atol=1e-3)                  # YAML 里保留 4 位小数


def test_config_fixed_region_matches_template(cfg):
    """固定区（速度掩膜用）= 模板里的三条馈线，逐点一致。"""
    yaml_polys = [p.vertices for p in cfg.fixed_region]
    code_polys = D.fixed_metal_polygons()
    assert len(yaml_polys) == len(code_polys) == 3
    for yp, cp in zip(yaml_polys, code_polys):
        assert len(yp) == len(cp)
        assert np.allclose(np.asarray(yp, float), np.asarray(cp, float),
                           atol=1e-3)


def test_config_min_gap_must_stay_zero(cfg, ls):
    """``min_gap_mm`` **必须为 0**（不是论文的 0.1）：

    ``apply_min_gap`` 在固定金属**外侧**铺一条 gap 厚的禁区带，而三条馈线
    正贴在框边界上——非 0 的带子会伸进框内、把可动金属在边界处整列削成
    空气。这里把"断"这件事演一遍：gap=0.2 时设计区轮廓从 3 条开口变成 1 条，
    闭合多边形再也够不到框右界（臂端与输出线断开，而 CST 照样出数）。
    """
    assert cfg.constraints.min_gap_mm == 0.0
    broken = dataclasses.replace(cfg, constraints=dataclasses.replace(
        cfg.constraints, min_gap_mm=0.2))
    ls2 = LevelSet2D(ls.box, ls.dx)
    ls2.phi = ls.phi.copy()
    apply_min_gap(ls2, broken)

    # 只看条带内部的节点：y=±w/2 那两个正好落在多边形边上（φ 恰为 0），
    # 翻符号后仍是 0，判不出"削没削"。
    strip_y = np.abs(ls.ys) <= D.W / 2 - 0.5 * ls.dx
    # 真实配置：框内第二列全是金属；gap=0.2：那一列被削空 → 金属缩回框外
    assert np.all(ls.phi[1, strip_y] <= 1e-9)
    assert np.all(ls2.phi[1, strip_y] > 0)
    assert len(movable_contours(ls, cfg)) == 3
    assert len(movable_contours(ls2, broken)) != 3
    polys = close_open_contours(movable_contours(ls2, broken), ls2.box)
    assert max(np.asarray(p)[:, 0].max() for p in polys) \
        < D.BOX_X1 + 0.05 - 1e-6                   # 够不到框右界了


def test_anchors_are_frozen_by_the_velocity_mask(ls, cfg):
    """连接靠两件事兑现（模板 docstring 的承诺）：可动金属穿出边界 +
    **掩膜把边界锚点的速度冻成 0**。冻不住的话金属会从馈线上"缩回去"，
    而每轮照样出 S 参数。"""
    mask = build_velocity_mask(ls, cfg)
    strip_y = np.abs(ls.ys) <= D.W / 2 + 1e-9
    anchor_y = (np.abs(ls.ys) >= 3.4) & (np.abs(ls.ys) <= 4.5)
    assert np.all(mask[0, strip_y] == 0.0)        # 左界：输入段锚点
    assert np.all(mask[-1, anchor_y] == 0.0)      # 右界：两个臂端锚点
    assert mask[20, np.abs(ls.ys) < 1e-9][0] > 0.0    # 框内深处仍可动


# =========================================================================== #
# 四、离线几何自检（无 CST）
# =========================================================================== #
def test_initial_metal_is_one_connected_body_with_three_anchors(ls):
    """初始金属必须是**一块**：论文两输出同相的前提就是线始终连着。"""
    _, n = ndimage.label(ls.phi < 0, structure=np.ones((3, 3)))
    assert n == 1
    # 三个穿出锚点。边界上的节点 φ = −0.0（正好压在网格线上），用 <= 判；
    # 严格 < 0 会把三个锚点全判成"没有金属"。
    left = ls.ys[ls.phi[0, :] <= 1e-9]
    assert (left.min(), left.max()) == pytest.approx((-D.W / 2, D.W / 2))
    for sign in (+1, -1):
        right = sign * ls.ys[ls.phi[-1, :] <= 1e-9]
        right = right[right > 0]                     # 只看该侧臂端
        lo, hi = sorted(sign * y for y in _at_edge(D.arm_polygon(sign), D.BOX_X1))
        assert right.min() >= lo - ls.dx             # 覆盖到斜切口下端（±1 格）
        assert right.max() >= hi - ls.dx             # 且顶到臂端中心线
        assert right.max() <= D.ARM_DY + D.W / 2 + 1e-9      # 没越过输出线


def test_closed_contours_preserve_the_y_shape_and_the_v_notch(cfg, ls):
    """Y 形有 **3 个开口**（输入段 1 + 两臂 2）→ 必须闭合成 1 个多边形。

    闭合规则由"沿边界周长配对相邻端点"给出，这里锁住结果：一块金属、
    无自交（close_open_contours 自己会检）、**两臂之间的 V 形空气缺口不能
    被填死**——填死等于静默把设计区改成实心楔形，S 参数照样出数。
    """
    mov = movable_contours(ls, cfg)
    assert len(mov) == 3
    assert all(not np.allclose(c[0], c[-1]) for c in mov)

    polys = close_open_contours(mov, cfg.design_region.box)
    assert len(polys) == 1
    poly = np.asarray(polys[0], float)
    inside = MplPath(poly).contains_points

    metal = [(10.0, 0.0), (18.0, 0.0), (30.0, 2.7), (30.0, -2.7),
             (33.5, 4.0), (33.5, -4.0)]
    air = [(28.0, 0.0), (31.0, 0.0), (33.0, 0.0), (8.0, 5.0), (20.0, -4.0)]
    for x, y in metal:      # 距真实边界 ≥ 0.2 mm
        assert inside([[x, y]])[0], f"({x}, {y}) 丢了金属"
    for x, y in air:        # V 形缺口 / 框内空白区
        assert not inside([[x, y]])[0], f"({x}, {y}) 是空气却被填成金属"
    # 闭合时沿框边界外扩 0.05 mm（与固定馈线重叠，CST 侧压实连通）
    assert poly[:, 0].min() == pytest.approx(D.BOX_X0 - 0.05)
    assert poly[:, 0].max() == pytest.approx(D.BOX_X1 + 0.05)


def test_the_outline_matches_the_level_set_inside_the_box(cfg, ls):
    """闭合多边形与 φ 场必须是同一块金属。

    这是"轮廓提取 → CST 几何"这条路上最容易静默出错的一环：轮廓多一块、
    少一块，S 参数都照样出数。只允许**界面节点**（φ 恰好 0、正好落在多边形
    上）判得不一样。
    """
    polys = close_open_contours(movable_contours(ls, cfg), cfg.design_region.box)
    inside = MplPath(np.asarray(polys[0], float)).contains_points
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    box = cfg.design_region.box
    inner = ((X > box.x[0] + 0.25) & (X < box.x[1] - 0.25)
             & (Y > box.y[0] + 0.25) & (Y < box.y[1] - 0.25)).ravel()
    bad = inner & ((ls.phi.ravel() < 0)
                   != inside(np.stack([X.ravel(), Y.ravel()], axis=1)))
    assert np.all(np.abs(ls.phi.ravel()[bad]) <= 1e-9)


# =========================================================================== #
# 五、算例分发（case.py）与 CST 侧常量
# =========================================================================== #
def test_load_case_dispatch(cfg):
    setup, model = load_case(cfg)
    assert setup is DIVIDER and model is D
    coupler_cfg = CaseConfig.from_yaml(REPO / "configs" / "coupler.yaml")
    setup, model = load_case(coupler_cfg)
    assert setup is COUPLER and model is COUPLER_MODEL
    assert set(known_cases()) == {"coupler", "divider"}


def test_load_case_rejects_an_unknown_name(cfg):
    """算例名拼错必须当场报错：静默按另一套几何建工程 = S 参数照样出数、
    结果全错（本项目最贵的一类错误）。"""
    with pytest.raises(ValueError, match="未知算例"):
        load_case(dataclasses.replace(cfg, name="dvider"))


def test_divider_setup_physics_and_export_steps(cfg):
    """论文 III-B 的物理常量：Rogers3003（εr=3.0 / tanδ=0.001）、3 个端口；
    场导出 0.2 mm（面内）× 0.1 mm（z）——导出体积按步长立方增长，
    面内放到 0.2 才把每份文件压到百 MB 以内。"""
    assert (DIVIDER.eps_r, DIVIDER.loss_tangent) == (3.0, 0.001)
    assert DIVIDER.ports == (1, 2, 3)
    assert DIVIDER.substrate_h_mm == 0.762
    assert DIVIDER.metal_thickness_mm == 0.035
    assert DIVIDER.frequency_ghz == 5.0 and DIVIDER.fmax_ghz == 10.0
    assert DIVIDER.resolve_export_steps(cfg.field_export_step_mm) == (0.2, 0.1)
    assert DIVIDER.stimulus("fwd", cfg.objective) == 1
    assert DIVIDER.stimulus("bwd", cfg.objective) == 2
    DIVIDER.validate_objective(cfg.objective)          # 1/2 都在表里
    for bad in (0, 4):
        with pytest.raises(ValueError, match="不是模板中的端口"):
            DIVIDER.validate_objective(
                dataclasses.replace(cfg.objective, from_port=bad))
