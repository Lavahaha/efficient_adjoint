"""形状导数（式 54–55）测试：手工构造复场，独立手算参考值。

配对按式 (54) 原文：E_⊥ = E_z、H_∥ = (H_x, H_y)——顶面/底面 PEC 上的
"切向电场与法向磁场为零"两项，而不是沿面内轮廓法向读到的侧壁配对。
"""

import numpy as np

from eaopt.adjoint.derivative import EPS0, MU0, shape_derivative


def test_hand_computed_values():
    # 点1：E_z 与面内 H 都有值；点2：只有面内 E 与 H_z（式 54 里为零的两项）
    e_fwd = np.array([[1.0 + 2j, 0.0, 3.0 - 1j],
                      [5.0, 0.0, 0.0]])
    e_back = np.array([[2.0, 0.0, 1.0 + 1j],
                       [7.0, 0.0, 0.0]])
    h_fwd = np.array([[0.0, 2.0 + 0.5j, 0.0],
                      [0.0, 0.0, 4.0]])
    h_back = np.array([[0.0, 1.0 + 2j, 0.0],
                       [0.0, 0.0, 9.0]])
    pin, omega, eps_r = 0.5, 2 * np.pi * 5e9, 3.66

    # 点1：E_z·E_z^back = (3−j)(1+j) = 4 + 2j
    #      H_x·H_x^back = (2+0.5j)(1+2j) = 1 + 4.5j
    # 点2：E_⊥ = E_z = 0，H_∥ = (H_x, H_y) = 0 → 被积式 0
    integrand = np.array([EPS0 * eps_r * (4 + 2j) + MU0 * (1 + 4.5j), 0.0])
    expected = np.real(-2j * omega / pin * integrand)

    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, pin, omega, eps_r,
                           phase=1.0)
    assert np.allclose(got, expected)


def test_inplane_electric_and_normal_magnetic_contribute_nothing():
    # 面内 E 与 H_z 不出现在式 (54) 中（它们在 PEC 面上为零）
    e_fwd = np.array([[0.0, 1.0 + 1j, 0.0]])
    e_back = np.array([[0.0, 2.0, 0.0]])
    h_fwd = np.array([[0.0, 0.0, 3.0]])
    h_back = np.array([[0.0, 0.0, 4.0]])
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, 0.5, 2 * np.pi * 5e9,
                           3.66, phase=1.0)
    assert np.allclose(got, 0.0)


def test_real_fields_give_zero_derivative():
    # Re[−2jω X] = 2ω·Im[X]：全实场（无相位差）导数为零
    e_fwd = np.array([[1.0, 0.0, 1.0]])
    e_back = np.array([[2.0, 0.0, 3.0]])
    h_fwd = np.array([[0.0, 1.0, 0.0]])
    h_back = np.array([[0.0, 2.0, 0.0]])
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, 0.5, 2 * np.pi * 5e9,
                           3.66, phase=1.0)
    assert np.allclose(got, 0.0)


def test_phase_factor_reproduces_magnitude_derivative():
    """phase = e^{−j·arg S} 时，δp 应与 |S| 的有限差分同号同比例。

    把两个采样点当成"δ 变化时 S(δ) = c0 + δ·X"的一阶模型：式 (54) 给出的
    复导数是 X，乘相位后取实部必须等于 d|S|/dδ 的中心差分。
    """
    c0 = -0.27 - 0.58j                      # 量级同 divider 的 S21
    x = np.array([0.4 - 0.9j])
    delta = 1e-6
    fd = (abs(c0 + delta * x[0]) - abs(c0 - delta * x[0])) / (2 * delta)

    # 反解出能产生该 X 的场：E_z 单独承担（H_∥ 置零），X = −2jω/P_in·ε0εr·E_z E_z^b
    pin, omega, eps_r = 0.5, 2 * np.pi * 5e9, 3.0
    e_fwd = np.array([[0.0, 0.0, 1.0]])
    e_back = np.array([[0.0, 0.0, x[0] / (-2j * omega / pin * EPS0 * eps_r)]])
    zeros = np.zeros((1, 3))
    got = shape_derivative(e_fwd, zeros, e_back, zeros, pin, omega, eps_r,
                           phase=np.exp(-1j * np.angle(c0)))
    assert np.isclose(got[0], fd, rtol=1e-6)
