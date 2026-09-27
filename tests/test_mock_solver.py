"""MockSolver 基本性质测试。"""

import numpy as np
import pytest

from eaopt.pipeline import make_level_set
from eaopt.solver.mock import MockSolver

from conftest import make_compact_cfg, with_arm_top


@pytest.fixture
def solver(tmp_path):
    cfg = make_compact_cfg(tmp_path)
    ls = make_level_set(cfg)
    s = MockSolver(cfg, ls)
    s.build_model(None, None)
    return s


def test_initial_s31_magnitude_is_0_3(solver):
    sol = solver.solve_forward()
    assert abs(sol.s_params[(3, 1)]) == pytest.approx(0.3)
    assert abs(sol.s_params[(2, 1)]) == pytest.approx(np.sqrt(1.0 - 0.3**2))


def test_backward_field_is_quadrature_of_forward(solver):
    f = solver.solve_forward()
    b = solver.solve_backward()
    assert np.allclose(b.e_field.data, 1j * f.e_field.data)


def test_field_zero_inside_metal(solver):
    # 臂内部深处（远离边界一个网格以上）场应为零
    pt = np.array([[2.0, -0.3, 0.0175]])
    v = solver.solve_forward().e_field.interp(pt)
    assert np.allclose(v, 0.0)


def test_field_nonzero_in_gap(solver):
    # 间隙中点场应显著非零（E 主要由 y 分量构成）
    pt = np.array([[2.0, 0.2, 0.0175]])
    v = solver.solve_forward().e_field.interp(pt)
    assert np.linalg.norm(v) > 1e-2
    assert abs(v[0, 1]) > abs(v[0, 0])


def test_capacitance_increases_as_gap_shrinks(tmp_path):
    cfg = make_compact_cfg(tmp_path)
    ls1 = make_level_set(cfg)
    s1 = MockSolver(cfg, ls1)
    s1.build_model(None, None)
    cfg2 = with_arm_top(cfg, 0.2)  # 间隙 0.4 -> 0.2
    ls2 = make_level_set(cfg2)
    s2 = MockSolver(cfg2, ls2)
    s2.build_model(None, None)
    assert s2.capacitance > s1.capacitance
