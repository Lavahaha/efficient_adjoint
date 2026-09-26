"""配置系统测试：加载、校验、错误拦截。"""

from pathlib import Path

import pytest

from eaopt.config import CaseConfig

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "coupler.yaml"


def test_load_coupler():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    assert cfg.name == "coupler"
    assert cfg.frequency == 5.0
    assert cfg.substrate.eps_r == 3.66
    assert cfg.substrate.height_mm == 0.762
    assert cfg.metal.material == "pec"
    assert cfg.metal.thickness_mm == 0.035
    obs = [p for p in cfg.ports if p.role == "observation"]
    assert len(obs) == 1 and obs[0].id == cfg.objective.to_port
    assert cfg.constraints.min_gap_mm == 0.1


def test_validate_rejects_bad_grid_step():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.design_region.grid_step_mm = -0.1
    with pytest.raises(ValueError):
        cfg.validate()


def test_validate_rejects_unknown_observation_port():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.objective.to_port = 99
    with pytest.raises(ValueError):
        cfg.validate()


def test_validate_rejects_missing_input_port():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.ports = [p for p in cfg.ports if p.role != "input"]
    with pytest.raises(ValueError):
        cfg.validate()


def test_validate_rejects_bad_sample_side():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.sampling.sample_side = "sideways"
    with pytest.raises(ValueError):
        cfg.validate()
