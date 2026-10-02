"""CST 侧常量与规则（cst_setup）：单一事实来源 + 端口/路径/导出步长规则。"""

import dataclasses
import pytest

from eaopt.solver import cst_model as M
from eaopt.solver.cst_setup import COUPLER, CstSetup


class _Objective:
    """objective 的鸭子类型替身（CstSetup 不 import CaseConfig）。"""

    def __init__(self, from_port=1, to_port=3):
        self.from_port = from_port
        self.to_port = to_port


# --------------------------------------------------------------------------- #
# 单一事实来源：模板与 pipeline 都从这里取值
# --------------------------------------------------------------------------- #
def test_template_physical_constants_come_from_setup():
    assert (M.EPS_R, M.TAND, M.SUB_H) == (COUPLER.eps_r, COUPLER.loss_tangent,
                                             COUPLER.substrate_h_mm)
    assert M.METAL_T == COUPLER.metal_thickness_mm
    assert M.FREQ == COUPLER.frequency_ghz
    assert M.FMAX == COUPLER.fmax_ghz == 2.0 * COUPLER.frequency_ghz
    assert M.FMIN == 0.0


def test_setup_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        COUPLER.frequency_ghz = 10.0


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
def test_project_path_defaults_into_output_dir(tmp_path):
    p = COUPLER.project_path("fwd", tmp_path / "results", "coupler")
    assert p == tmp_path / "results" / "cst" / "coupler_fwd.cst"
    assert COUPLER.project_path("bwd", tmp_path / "results", "coupler").name \
        == "coupler_bwd.cst"


def test_project_path_can_be_redirected():
    s = CstSetup(project_dir="D:/cst_projects")
    assert s.project_path("fwd", "results/x", "coupler").as_posix() == \
        "D:/cst_projects/coupler_fwd.cst"


def test_project_path_rejects_unknown_tag():
    with pytest.raises(ValueError, match="fwd"):
        COUPLER.project_path("sideways", "results", "coupler")


def test_stimulus_rule_fwd_from_bwd_to():
    assert COUPLER.stimulus("fwd", _Objective(1, 3)) == 1
    assert COUPLER.stimulus("bwd", _Objective(1, 3)) == 3
    assert COUPLER.stimulus("bwd", _Objective(2, 4)) == 4   # 规则在代码，值在配置
    with pytest.raises(ValueError):
        COUPLER.stimulus("sideways", _Objective())


def test_export_step_follows_sampling_unless_given():
    assert COUPLER.resolve_export_step(0.2) == 0.2          # None → 跟随采样点距
    assert CstSetup(export_step_mm=0.35).resolve_export_step(0.2) == 0.35


def test_validate_objective_checks_against_template_ports():
    COUPLER.validate_objective(_Objective(1, 3))
    with pytest.raises(ValueError, match="to_port"):
        COUPLER.validate_objective(_Objective(1, 99))
    with pytest.raises(ValueError, match="from_port"):
        COUPLER.validate_objective(_Objective(0, 3))
