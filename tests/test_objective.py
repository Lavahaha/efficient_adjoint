"""目标函数（FoM）：下标映射、取值、|S| 目标的链式法则相位因子。"""

import dataclasses

import numpy as np
import pytest
from conftest import make_compact_cfg

from eaopt.config import ObjectiveSpec
from eaopt.optimize.objective import fom_key, fom_phase, make_fom
from eaopt.solver.base import Solution


def _cfg(tmp_path, **obj):
    cfg = make_compact_cfg(tmp_path)
    return dataclasses.replace(cfg, objective=ObjectiveSpec(**obj))


def _sol(sp):
    # 只测 S 参数路径：场对象不参与（占位 None 由 dataclass 允许）
    return Solution(s_params=sp, e_field=None, h_field=None, pin=0.5)


def test_fom_key_follows_objective_ports(tmp_path):
    cfg = _cfg(tmp_path)                       # 缺省 from=1, to=3
    assert fom_key(cfg) == (3, 1)
    cfg = _cfg(tmp_path, from_port=1, to_port=2)
    assert fom_key(cfg) == (2, 1)


def test_fom_is_magnitude_of_that_sparam(tmp_path):
    cfg = _cfg(tmp_path, from_port=1, to_port=2)
    sp = {(2, 1): -0.27 - 0.58j, (3, 1): 0.5 + 0.1j}
    assert make_fom(cfg)(_sol(sp)) == pytest.approx(abs(sp[(2, 1)]))


def test_unknown_objective_type_raises(tmp_path):
    cfg = _cfg(tmp_path, type="reflection")
    with pytest.raises(ValueError, match="reflection"):
        fom_key(cfg)
    with pytest.raises(ValueError):
        make_fom(cfg)


def test_phase_factor_is_exp_minus_j_arg_s(tmp_path):
    """e^{−j·arg S}：divider 实测 arg S21 = −115°，因子应是 +115°。"""
    cfg = _cfg(tmp_path, from_port=1, to_port=2)
    s21 = -0.27257 - 0.58212j
    got = fom_phase(cfg, _sol({(2, 1): s21}))
    assert got == pytest.approx(np.exp(-1j * np.angle(s21)))
    assert np.angle(got, deg=True) == pytest.approx(-np.angle(s21, deg=True))


def test_phase_factor_obeys_chain_rule(tmp_path):
    """d|S| = Re[e^{−j arg S} dS]：与中心差分一致。"""
    cfg = _cfg(tmp_path, from_port=1, to_port=2)
    s21, ds, h = -0.27257 - 0.58212j, 0.4 - 0.9j, 1e-6
    fd = (abs(s21 + h * ds) - abs(s21 - h * ds)) / (2 * h)
    got = np.real(fom_phase(cfg, _sol({(2, 1): s21})) * ds)
    assert got == pytest.approx(fd, rel=1e-6)
