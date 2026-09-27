"""步长策略测试：归一化与 active 掩膜。"""

import numpy as np

from eaopt.optimize.step import fixed_step_velocity


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
