"""有限差分验证（mock）：式 (25) 整条链 vs Hadamard 电容形状导数。

δC = ε∫|E_⊥|² v_n ds（外移边界增强耦合）是解析已知的；
mock 的后向场取前向场正交相位，使预测值可精确对照：
  ratio = ΔFoM_actual / ΔFoM_pred = 0.3·P_in / (2ω·C0)。

扰动方式：把臂矩形顶边抬高 h（仅可动边界移动；勿用 φ−h 全局平移，
那会连固定直通线的裙边一起膨胀，破坏几何语义）。
"""

import numpy as np
import pytest

from eaopt.adjoint.derivative import shape_derivative
from eaopt.adjoint.sampling import sample_boundary
from eaopt.geometry.contour import extract_contours, smooth_resample
from eaopt.pipeline import make_level_set
from eaopt.solver.mock import MockSolver

from conftest import make_compact_cfg, with_arm_top


def test_fd_ratio_matches_hadamard(tmp_path):
    cfg = make_compact_cfg(tmp_path)
    ls = make_level_set(cfg)
    fixed = [np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region]
    solver = MockSolver(cfg, ls)
    solver.build_model(None, fixed)
    sol_f = solver.solve_forward()
    sol_b = solver.solve_backward()

    contours = extract_contours(ls.xs, ls.ys, ls.phi)
    movable = [
        smooth_resample(c, cfg.sampling.point_spacing_mm, smoothing=0.0,
                        closed=np.allclose(c[0], c[-1]))
        for c in contours
    ]
    pts, nrm = sample_boundary(movable, ls, cfg)

    # 只取臂顶边上的采样点（唯一被扰动的边界；偏移后位于 y≈+0.06）。
    # 注意取全顶边（不限制 x）：实际扰动动了整条顶边（含两端），
    # 预测必须同口径，否则两端翻转贡献被漏掉、比值失真
    off = cfg.sampling.sample_offset_mm
    sel = (pts[:, 1] > off - 0.03) & (pts[:, 1] < off + 0.03)
    assert sel.sum() > 10
    z = cfg.sampling.field_z_mm
    pts3 = np.column_stack([pts, np.full(len(pts), z)])
    nrm3 = np.column_stack([nrm, np.zeros(len(nrm))])
    e_f = sol_f.e_field.interp(pts3)
    h_f = sol_f.h_field.interp(pts3)
    e_b = sol_b.e_field.interp(pts3)
    h_b = sol_b.h_field.interp(pts3)
    dp = shape_derivative(e_f, h_f, e_b, h_b, nrm3, sol_f.pin,
                          2 * np.pi * 5e9, cfg.substrate.eps_r)

    h = 0.08  # 顶边抬高量（mm）；0.08 严格落在网格节点之间，无浮点歧义
    # 栅格化量化：mock 网格 0.1 mm，臂顶的栅格化边沿从 y=−0.05 移到 y=+0.05，
    # 有效位移为整整一个格子 0.1 mm，预测按有效位移计算
    h_eff = 0.1
    ds = cfg.sampling.point_spacing_mm
    pred = float(np.sum(dp[sel])) * h_eff * ds  # ΔFoM 预测

    # 实际：臂顶边抬高 h 后重建几何（其余边界不动）
    cfg2 = with_arm_top(cfg, 0.0 + h)
    ls2 = make_level_set(cfg2)
    solver2 = MockSolver(cfg2, ls2)
    solver2.build_model(None, fixed)
    c0 = solver._c0
    actual = 0.3 * (solver2.capacitance - solver.capacitance) / c0

    # 符号一致（外扩增强耦合）：预测与实际均为正
    assert pred > 0
    assert actual > 0
    ratio = actual / pred
    expected = 0.3 * sol_f.pin / (2 * 2 * np.pi * 5e9 * c0)
    assert ratio == pytest.approx(expected, rel=0.5)
