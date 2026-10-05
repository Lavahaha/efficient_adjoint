"""约束测试：速度掩膜与最小间距投影。"""

from copy import deepcopy

import numpy as np
import pytest

from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import apply_min_gap, build_velocity_mask
from eaopt.pipeline import make_level_set

from conftest import make_compact_cfg, with_arm_top


def _at(ls, mask, x, y):
    ix = int(round((x - ls.box.x[0]) / ls.dx))
    iy = int(round((y - ls.box.y[0]) / ls.dx))
    return mask[ix, iy]


def _with_taper(cfg, edges):
    cfg2 = deepcopy(cfg)
    cfg2.constraints.taper_edges = edges
    return cfg2


def test_velocity_mask(tmp_path):
    cfg = make_compact_cfg(tmp_path)
    ls = make_level_set(cfg)
    margin = max(cfg.constraints.min_gap_mm, 2.0 * ls.dx)  # = 0.2（compact 配置）
    # 小 taper（0.5）：盒子内部（距各边 >0.5）不受 taper 影响
    mask = build_velocity_mask(ls, cfg, taper_mm=0.5)
    assert _at(ls, mask, 2.0, 0.7) == 0.0  # 直通线（固定区）内禁止
    # 距直通线 > margin 才允许运动：y=0.15 → 距离 0.25 > 0.2
    assert _at(ls, mask, 2.0, 0.15) == 1.0
    assert _at(ls, mask, 2.0, 0.25) == 0.0  # 距离 0.15 < margin → 禁止
    assert _at(ls, mask, 2.0, -0.3) == 1.0  # 臂内部允许（掩膜不管金属语义）
    # 默认 taper 1 mm：x=0.2 距左边缘 0.2 mm → 因子 0.2
    mask2 = build_velocity_mask(ls, cfg)
    assert _at(ls, mask2, 0.2, -0.3) == pytest.approx(0.2, abs=0.02)
    # 允许活动区外禁止
    from eaopt.config import BoxSpec

    cfg2 = make_compact_cfg(tmp_path)
    cfg2.constraints.allowed_region = BoxSpec(x=(0.0, 4.0), y=(-1.0, 0.0))
    mask2 = build_velocity_mask(ls, cfg2)
    assert _at(ls, mask2, 2.0, 0.2) == 0.0


def test_taper_edges_selects_which_box_edges_are_damped(tmp_path):
    """taper 的作用边可选：设计区自身的边界（可动金属自由生长的那条）不能 taper。

    本算例的设计区上界离直通线只有 0.1 mm（论文约束），臂顶要长进耦合间隙；
    若上下边也被 1 mm 的 taper 削，间隙里的速度会被整体压掉、优化推不动。
    """
    cfg = make_compact_cfg(tmp_path)
    cfg.fixed_region = []          # 隔离 taper：固定金属邻域另行掩膜
    ls = make_level_set(cfg)
    xy = build_velocity_mask(ls, cfg, taper_mm=1.0)              # 默认 "xy"
    x = build_velocity_mask(ls, _with_taper(cfg, "x"), taper_mm=1.0)
    off = build_velocity_mask(ls, _with_taper(cfg, "none"), taper_mm=1.0)
    # 距上边 0.2 mm（x 远离两端）：只有 y 边参与 taper 时才被削
    assert _at(ls, xy, 2.0, 0.8) == pytest.approx(0.2, abs=0.02)
    assert _at(ls, x, 2.0, 0.8) == 1.0
    assert _at(ls, off, 2.0, 0.8) == 1.0
    # 距左端 0.3 mm：x 边在所有模式下都该削（那里接着区外的固定腿）
    assert _at(ls, xy, 0.3, 0.0) == pytest.approx(0.3, abs=0.02)
    assert _at(ls, x, 0.3, 0.0) == pytest.approx(0.3, abs=0.02)
    assert _at(ls, off, 0.3, 0.0) == 1.0


def test_min_gap_carves_encroaching_metal(tmp_path):
    cfg = make_compact_cfg(tmp_path, min_gap=0.1)
    cfg2 = with_arm_top(cfg, 0.35)  # 臂顶 0.35，距直通线仅 0.05 < min_gap
    ls = make_level_set(cfg2)
    apply_min_gap(ls, cfg2)
    tmp = LevelSet2D(ls.box, ls.dx)
    tmp.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg2.fixed_region])
    dist = np.abs(tmp.phi)
    prohibited = (tmp.phi > 0) & (dist < cfg2.constraints.min_gap_mm)
    assert not np.any((ls.phi < 0) & prohibited)  # 无侵入禁区
    # 臂主体仍保留
    ix = int(round((2.0 - ls.box.x[0]) / ls.dx))
    iy = int(round((-0.3 - ls.box.y[0]) / ls.dx))
    assert ls.phi[ix, iy] < 0
