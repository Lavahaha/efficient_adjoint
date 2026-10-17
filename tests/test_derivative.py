"""形状导数测试：按三维法向投影 E，并取 H 的三维切向分量。"""

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
    # n=y: E_n=E_y=0; H_t=(H_x, 0, H_z), so H_x and H_z both contribute.
    integrand = np.array([
        EPS0 * eps_r * (2 + 4j) + MU0 * (1 + 4.5j),
        MU0 * (4.0 * 9.0 + 40.0 * 90.0),
    ])
    expected = np.real(-2j * omega / pin * integrand)
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, pin, omega,
                           eps_r, normals, phase=1.0)
    assert np.allclose(got, expected)


def test_3d_normal_uses_ez_and_tangential_xy():
    # n=z: E_n = E_z; H_t = (H_x, H_y, 0) — H_z is normal and excluded.
    e_fwd = np.array([[1.0 + 1j, 2.0, 3.0 - 2.0j]])
    e_back = np.array([[4.0, 5.0j, 6.0]])
    h_fwd = np.array([[1.0, 2.0, 7.0 + 0.5j]])
    h_back = np.array([[3.0j, 4.0, 8.0]])
    normals = np.array([[0.0, 0.0, 1.0]])
    pin, omega, eps_r = 0.5, 2 * np.pi * 5e9, 3.0

    integrand = (EPS0 * eps_r * (3.0 - 2.0j) * 6.0
                 + MU0 * (1.0 * 3.0j + 2.0 * 4.0))
    expected = np.real(-2j * omega / pin * integrand)
    got = shape_derivative(e_fwd, h_fwd, e_back, h_back, pin, omega,
                           eps_r, normals, phase=1.0)
    assert np.allclose(got, expected)


def test_2d_normal_matches_padded_3d_normal():
    rng = np.random.default_rng(0)
    fields = [rng.normal(size=(4, 3)) + 1j * rng.normal(size=(4, 3))
              for _ in range(4)]
    n2 = rng.normal(size=(4, 2))
    n3 = np.column_stack([n2, np.zeros(4)])
    args = (*fields, 0.5, 2 * np.pi * 5e9, 3.0)
    got2 = shape_derivative(*args, n2)
    got3 = shape_derivative(*args, n3)
    assert np.allclose(got2, got3)


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
