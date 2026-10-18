"""步长策略测试：两种归一化模式与 active 掩膜、子步规划。"""

import numpy as np
import pytest

from eaopt.optimize.step import SUBSTEP_CAP, fixed_step_velocity, plan_substeps


# ---------------------------------------------------------------------- #
# normalize="max"（旧行为）
# ---------------------------------------------------------------------- #
def test_normalizes_by_global_max():
    dp = np.array([10.0, 4.0, -2.0])
    v = fixed_step_velocity(dp, 1.0)
    assert np.allclose(v, [1.0, 0.4, -0.2])


def test_active_mask_excludes_from_normalization():
    dp = np.array([10.0, 4.0, 2.0])
    # 最大点被禁：归一化改用次大 4.0
    v = fixed_step_velocity(dp, 1.0, active=np.array([False, True, True]))
    assert np.allclose(v, [0.0, 1.0, 0.5])


def test_all_inactive_gives_zero():
    dp = np.array([1.0, -1.0])
    v = fixed_step_velocity(dp, 1.0, active=np.array([False, False]))
    assert np.allclose(v, 0.0)


# ---------------------------------------------------------------------- #
# normalize="none"（2026-10-18：去掉归一化）
# ---------------------------------------------------------------------- #
def test_no_normalization_returns_raw_scaled_delta_p():
    dp = np.array([10.0, 4.0, -2.0])
    v = fixed_step_velocity(dp, 1.0, normalize="none")
    assert np.allclose(v, dp)          # 原值直出，不再有 |V| ≤ 1


def test_no_normalization_applies_sign_and_active_mask():
    dp = np.array([10.0, 4.0, 2.0])
    v = fixed_step_velocity(dp, -1.0, active=np.array([False, True, True]),
                            normalize="none")
    assert np.allclose(v, [0.0, -4.0, -2.0])


def test_unknown_normalize_rejected():
    with pytest.raises(ValueError):
        fixed_step_velocity(np.array([1.0]), 1.0, normalize="maxmin")


# ---------------------------------------------------------------------- #
# plan_substeps：无归一化模式的尺度规划
# ---------------------------------------------------------------------- #
def test_plan_substeps_reference_quantile_moves_step_cells_times_dx():
    """参考分位点每轮位移 = step_cells·dx；其余点与 |δp| 严格成比例。"""
    dp = np.array([1.0] * 90 + [20.0] * 10)      # 重尾：p90 ≈ 2.9，max = 20
    active = np.ones(len(dp), dtype=bool)
    step_cells, cfl, dx = 8.0, 0.5, 0.1
    steps, info = plan_substeps(dp, active, step_cells=step_cells, cfl=cfl,
                                dx=dx, step_ref_quantile=0.9)
    vmax = np.abs(dp).max()
    ref = np.quantile(np.abs(dp), 0.9)
    assert info["ref"] == pytest.approx(ref)
    assert info["vmax"] == vmax
    # steps = ceil(step_cells·vmax/(cfl·ref))，位移 = |δp|·steps·cfl·dx/vmax
    c = step_cells * vmax / (cfl * ref)
    assert steps == int(np.ceil(c))
    disp = np.abs(dp) * steps * cfl * dx / vmax
    d_ref = ref * steps * cfl * dx / vmax
    assert step_cells * dx <= d_ref <= step_cells * dx * (1.0 + 1.0 / c) + 1e-12
    # 比例性：位移只与 |δp| 成正比，max 点不再被压到 0.8 mm
    assert disp.max() > step_cells * dx
    assert np.allclose(disp / disp.max(), np.abs(dp) / vmax)
    assert info["disp_max_mm"] == pytest.approx(disp.max())
    assert info["disp_p50_mm"] == pytest.approx(np.percentile(disp, 50))


def test_plan_substeps_zero_or_inactive_delta_p():
    dp = np.array([1.0, -2.0])
    steps, info = plan_substeps(dp, np.array([False, False]),
                                step_cells=8.0, cfl=0.5, dx=0.1)
    assert steps == 0
    steps, info = plan_substeps(np.zeros(5), np.ones(5, bool),
                                step_cells=8.0, cfl=0.5, dx=0.1)
    assert steps == 0


def test_plan_substeps_caps_runaway_substeps():
    """极端重尾（max/ref 巨大）时子步数触顶 SUBSTEP_CAP 而不是天文数字。"""
    dp = np.array([1.0] * 100 + [1e9])
    active = np.ones(len(dp), dtype=bool)
    steps, info = plan_substeps(dp, active, step_cells=8.0, cfl=0.5, dx=0.1,
                                step_ref_quantile=0.9)
    assert steps == SUBSTEP_CAP
    assert info["capped"]
