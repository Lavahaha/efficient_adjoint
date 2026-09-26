"""形状导数（式 25）测试：手工构造复场，独立手算参考值。"""

import numpy as np

from eaopt.adjoint.derivative import EPS0, MU0, shape_derivative


def test_hand_computed_values():
    # 两个采样点：法向分别为 x 轴与 y 轴
    n = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    e_fwd = np.array([[1.0 + 2j, 0.0, 0.0], [0.0, 3.0, 0.0]])
    e_back = np.array([[3.0 - 1j, 0.0, 0.0], [0.0, 2.0 + 1j, 0.0]])
    h_fwd = np.array([[0.0, 2.0 + 0.5j, 0.0], [0.0, 0.0, 1.0 - 1j]])
    h_back = np.array([[0.0, 1.0 + 2j, 0.0], [0.0, 0.0, 2.0]])
    pin, omega, eps_r = 0.5, 2 * np.pi * 5e9, 3.66

    # 点1（法向 x）：E·n = 1+2j，E^back·n = 3−j
    #   → 积 = (1+2j)(3−j) = 3 − j + 6j − 2j² = 5 + 5j
    #   H 切向 = 全部分量 (0, 2+0.5j, 0)；H^back 切向 = (0, 1+2j, 0)
    #   → 积 = (2+0.5j)(1+2j) = 2 + 4j + 0.5j + j² = 1 + 4.5j
    # 点2（法向 y）：E·n = 3，E^back·n = 2+j → 积 = 6 + 3j
    #   H 切向 = (0,0,1−j)；H^back 切向 = (0,0,2) → 积 = 2 − 2j
    integrand = np.array(
        [EPS0 * eps_r * (5 + 5j) + MU0 * (1 + 4.5j),
         EPS0 * eps_r * (6 + 3j) + MU0 * (2 - 2j)]
    )
    expected = np.real(-2j * omega / pin * integrand)

    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, n, pin, omega, eps_r)
    assert np.allclose(got, expected)


def test_tangential_electric_and_normal_magnetic_contribute_nothing():
    # 纯切向 E 与纯法向 H 不出现在式 (25) 中
    n = np.array([[1.0, 0.0, 0.0]])
    e_fwd = np.array([[0.0, 1.0 + 1j, 0.0]])
    e_back = np.array([[0.0, 2.0, 0.0]])
    h_fwd = np.array([[3.0, 0.0, 0.0]])
    h_back = np.array([[4.0, 0.0, 0.0]])
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, n, 0.5, 2 * np.pi * 5e9, 3.66)
    assert np.allclose(got, 0.0)


def test_real_fields_give_zero_derivative():
    # Re[−2jω X] = 2ω·Im[X]：全实场（无相位差）导数为零
    n = np.array([[1.0, 0.0, 0.0]])
    e_fwd = np.array([[1.0, 0.0, 0.0]])
    e_back = np.array([[2.0, 0.0, 0.0]])
    h_fwd = np.array([[0.0, 1.0, 0.0]])
    h_back = np.array([[0.0, 2.0, 0.0]])
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, n, 0.5, 2 * np.pi * 5e9, 3.66)
    assert np.allclose(got, 0.0)
