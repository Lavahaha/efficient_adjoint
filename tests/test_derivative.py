"""形状导数测试：按 xy 轮廓法向投影 E，并取面内 H_xy 切向分量。"""

import numpy as np

from eaopt.adjoint.derivative import EPS0, MU0, shape_derivative


def test_hand_computed_values():
    e_fwd = np.array([[1.0 + 2j, 0.0, 30.0],
                      [5.0, 0.0, 0.0]])
    e_back = np.array([[2.0, 0.0, 10.0],
                       [7.0, 0.0, 0.0]])
    h_fwd = np.array([[0.0, 2.0 + 0.5j, 0.0],
                      [4.0, 0.0, 40.0]])
    h_back = np.array([[0.0, 1.0 + 2j, 0.0],
                       [9.0, 0.0, 90.0]])
    normals = np.array([[1.0, 0.0], [0.0, 1.0]])
    pin, omega, eps_r = 0.5, 2 * np.pi * 5e9, 3.66

    # n=x: E_n product=(1+2j)*2; H_tau=H_y and H_z=0.
    # n=y: E_n=E_y=0; H_x is xy-tangential, while H_z is excluded.
    integrand = np.array([
        EPS0 * eps_r * (2 + 4j) + MU0 * (1 + 4.5j),
        MU0 * 36.0,
    ])
    expected = np.real(-2j * omega / pin * integrand)
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, pin, omega,
                           eps_r, normals, phase=1.0)
    assert np.allclose(got, expected)


def test_tangential_electric_and_normal_magnetic_do_not_contribute():
    # For n=x, E_y is contour-tangential and excluded; H_x is contour-normal.
    e_fwd = np.array([[0.0, 1.0 + 1j, 0.0]])
    e_back = np.array([[0.0, 2.0, 0.0]])
    h_fwd = np.array([[3.0, 0.0, 0.0]])
    h_back = np.array([[4.0, 0.0, 0.0]])
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, 0.5,
                           2 * np.pi * 5e9, 3.66,
                           np.array([[1.0, 0.0]]))
    assert np.allclose(got, 0.0)


def test_real_fields_give_zero_derivative():
    e_fwd = np.array([[1.0, 0.0, 1.0]])
    e_back = np.array([[2.0, 0.0, 3.0]])
    h_fwd = np.array([[0.0, 1.0, 0.0]])
    h_back = np.array([[0.0, 2.0, 0.0]])
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, 0.5,
                           2 * np.pi * 5e9, 3.66,
                           np.array([[1.0, 0.0]]), phase=1.0)
    assert np.allclose(got, 0.0)


def test_phase_factor_reproduces_magnitude_derivative():
    c0 = -0.27 - 0.58j
    x = np.array([0.4 - 0.9j])
    delta = 1e-6
    fd = (abs(c0 + delta * x[0]) - abs(c0 - delta * x[0])) / (2 * delta)

    pin, omega, eps_r = 0.5, 2 * np.pi * 5e9, 3.0
    e_fwd = np.array([[1.0, 0.0, 0.0]])
    e_back = np.array([[x[0] / (-2j * omega / pin * EPS0 * eps_r), 0.0, 0.0]])
    zeros = np.zeros((1, 3))
    got = shape_derivative(e_fwd, zeros, e_back, zeros, pin, omega,
                           eps_r, np.array([[1.0, 0.0]]),
                           phase=np.exp(-1j * np.angle(c0)))
    assert np.isclose(got[0], fd, rtol=1e-6)
