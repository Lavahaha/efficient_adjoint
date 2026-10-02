"""配置系统测试：加载、校验、错误拦截。"""

from pathlib import Path

import pytest
import yaml

from eaopt.config import CaseConfig
from eaopt.solver.cst_setup import COUPLER

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "coupler.yaml"


def test_load_coupler():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    assert cfg.name == "coupler"
    assert cfg.objective.from_port == 1 and cfg.objective.to_port == 3
    assert cfg.design_region.box.x == (0.0, 12.0)
    assert cfg.constraints.min_gap_mm == 0.1
    assert cfg.optimizer.max_iterations == 30


def test_yaml_carries_no_cst_side_keys():
    """CST 侧信息只在 cst_setup（单一来源），不得再出现在 YAML 里。"""
    d = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    for key in ("solver", "frequency", "substrate", "metal", "ports"):
        assert key not in d, f"{key} 已搬去 cst_setup，YAML 里不该再有"


def test_validate_rejects_bad_grid_step():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.design_region.grid_step_mm = -0.1
    with pytest.raises(ValueError):
        cfg.validate()


def test_validate_rejects_bad_sample_side():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.sampling.sample_side = "sideways"
    with pytest.raises(ValueError):
        cfg.validate()


def test_unknown_key_is_rejected_not_ignored():
    """旧 YAML 的 solver/substrate 等键必须报错，不能被静默丢弃。"""
    d = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    d["solver"] = {"type": "cst"}
    with pytest.raises(ValueError, match="solver"):
        CaseConfig.from_dict(d)


def test_unknown_nested_key_is_rejected():
    d = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    d["sampling"]["point_spacing"] = 0.2      # 少写了 _mm
    with pytest.raises(ValueError, match="point_spacing"):
        CaseConfig.from_dict(d)


def test_setup_validates_objective_ports():
    """端口表在 CST 模板里：objective 的端口合法性由 CstSetup 校验。"""
    cfg = CaseConfig.from_yaml(CFG_PATH)
    COUPLER.validate_objective(cfg.objective)          # 1/3 都在表里，通过
    cfg.objective.to_port = 99
    with pytest.raises(ValueError, match="to_port"):
        COUPLER.validate_objective(cfg.objective)
