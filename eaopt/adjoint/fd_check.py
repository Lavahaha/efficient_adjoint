"""有限差分验证（FD check）：形状导数整条链的符号与数值校验。

原理（mock 世界）：后向场取前向场正交相位（E_back = j·E_fwd），
式 (25) 退化为 Hadamard 电容形状导数 δC = ε∫|E_⊥|² v_n ds，
理论比值 ratio = ΔFoM_actual / ΔFoM_pred = 0.3·P_in / (2ω·C0)。

扰动：仅外移可动边界——φ2 = φ − h·w，w = clip(dist_fixed/h, 0, 1)。
φ−h 全局平移会膨胀固定金属裙边（测试已排除），必须加权。

对 CST（第 7 步）：复用同一流程，扰动换成"轮廓沿法向偏移 → 重建
几何"，理论比值不再适用（只做符号裁决与量级检查）。
"""

from __future__ import annotations

import numpy as np

from eaopt.adjoint.derivative import shape_derivative
from eaopt.adjoint.sampling import sample_boundary
from eaopt.config import CaseConfig
from eaopt.geometry.contour import extract_contours, smooth_resample
from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import build_velocity_mask, interp_mask_at
from eaopt.solver.mock import MockSolver

__all__ = ["fd_check"]


def fd_check(cfg: CaseConfig, ls: LevelSet2D, solver, h: float = 0.08) -> dict:
    """对当前几何做一次有限差分验证，返回诊断字典。

    keys: ratio（实测/预测）、expected（mock 理论比值）、
    deviation（|ratio/expected−1|）、pred、actual、h_eff（栅格化
    量化校准后的有效位移）、n_active（参与预测的采样点数）、
    sign_ok（ratio>0 ⟹ velocity_sign=+1 正确）。
    """
    fixed = [np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region]
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
    z = cfg.sampling.field_z_mm
    pts3 = np.column_stack([pts, np.full(len(pts), z)])
    nrm3 = np.column_stack([nrm, np.zeros(len(nrm))])
    omega = 2.0 * np.pi * cfg.frequency * 1e9
    dp = shape_derivative(sol_f.e_field.interp(pts3), sol_f.h_field.interp(pts3),
                          sol_b.e_field.interp(pts3), sol_b.h_field.interp(pts3),
                          nrm3, sol_f.pin, omega, cfg.substrate.eps_r)

    side = 1.0 if cfg.sampling.sample_side == "outside" else -1.0
    bnd = pts - side * cfg.sampling.sample_offset_mm * nrm

    # 守卫：可动边界距固定金属须 > 2h，否则 w 加权扰动失真。
    # 注意：FD 检查用全可动边界口径（扰动动了整条边界，预测也必须
    # 覆盖全部采样点），不可套用优化时的 active 过滤——角点/边缘的
    # 集中场正是实测 ΔC 的大头。
    if cfg.fixed_region:
        tmp_ls = LevelSet2D(ls.box, ls.dx)
        tmp_ls.init_from_polygons(fixed)
        # 注意：界外填充用大数（界外 = 远离固定金属），不能用 0
        from scipy.interpolate import RegularGridInterpolator

        dist_interp = RegularGridInterpolator(
            (ls.xs, ls.ys), np.abs(tmp_ls.phi),
            bounds_error=False, fill_value=1e9,
        )
        if np.min(dist_interp(bnd)) < 2.0 * h:
            raise ValueError(
                f"可动边界距固定金属不足 {2*h:.2f} mm，w 加权扰动失真，"
                "请减小扰动 h 或调整配置"
            )

    # 扰动：φ2 = φ − h·w
    # w = clip(dist/h − 1, 0, 1)：dist≤h 不扰动（w=0），过渡带 (h,2h)
    # 内 φ2 ≡ h（恒正、无零交叉、不产生浮点尘埃），dist≥2h 全量扰动。
    # 注意不可用 w = clip(dist/h, 0, 1)——那会在固定金属邻域形成
    # φ2 ≡ 0 的退化平台区（dist − h·dist/h = 0），整行浮点尘埃。
    if cfg.fixed_region:
        w = np.clip(np.abs(tmp_ls.phi) / h - 1.0, 0.0, 1.0)
    else:
        w = 1.0
    ls2 = LevelSet2D(ls.box, ls.dx)
    ls2.phi = ls.phi - h * w

    # mock 专用：新几何直接来自 φ2（CST 端换成几何重建扰动）
    solver2 = MockSolver(cfg, ls2)
    solver2.build_model(None, fixed)
    c0 = solver._c0
    actual = 0.3 * (solver2.capacitance - solver.capacitance) / c0

    # 栅格化量化校准：有效位移 = 扰动前后可动金属顶行位移差
    h_eff = solver2.movable_extent()[1] - solver.movable_extent()[1]
    if h_eff <= 0.0:
        raise RuntimeError("扰动未使可动金属栅格化边界移动（h 小于量化台阶？）")

    ds = cfg.sampling.point_spacing_mm
    pred = float(np.sum(dp)) * h_eff * ds
    ratio = actual / pred
    expected = 0.3 * sol_f.pin / (2.0 * omega * c0)
    return {
        "ratio": ratio,
        "expected": expected,
        "deviation": abs(ratio / expected - 1.0),
        "pred": pred,
        "actual": actual,
        "h_eff": h_eff,
        "n_samples": len(dp),
        "sign_ok": ratio > 0,
    }
