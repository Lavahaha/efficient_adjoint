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


def test_validate_rejects_unknown_taper_edges():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.constraints.taper_edges = "left"
    with pytest.raises(ValueError):
        cfg.validate()


def test_coupler_samples_intersections_with_the_phi_grid_step():
    """交点方案：φ 网格步长 = CST 场导出步长（分辨率对等），**不要求对齐**。

    导出网格原点由 CST 包围盒定（实测 y −3.55，与设计区 −2.6 差半格），
    采样点按坐标取场——"设计区必须与导出网格对齐"这条约定已经取消。
    """
    cfg = CaseConfig.from_yaml(CFG_PATH)
    assert cfg.sampling.scheme == "intersection"
    assert cfg.field_export_step_mm == cfg.design_region.grid_step_mm == 0.1
    # 优化器步长以格为单位：1 格
    assert cfg.optimizer.step_cells == 1.0
    # 场就取在交点上（论文式 25 的边界值），不作沿法向的额外偏移
    assert cfg.sampling.intersection_offset_mm == 0.0


def test_contour_scheme_export_step_follows_point_spacing():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.sampling.scheme = "contour"
    assert cfg.field_export_step_mm == cfg.sampling.point_spacing_mm


def test_validate_rejects_grid_step_that_does_not_divide_the_box():
    """除不尽时 linspace 会悄悄拉伸格距（5.5/0.2 → 实际 0.1964），必须拦住。"""
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.design_region.grid_step_mm = 0.2       # 5.5/0.2 = 27.5
    with pytest.raises(ValueError, match="除不尽"):
        cfg.validate()


@pytest.mark.parametrize("key, bad", [
    ("intersection_offset_mm", -0.1),   # 只能沿外法向偏（负=往金属里钻）
    ("wls_radius_cells", 0.0),          # 邻域半径必须为正
    ("wls_order", 3),                   # 只支持 1 / 2
    ("scatter_max_cells", 0.0),         # 回写作用半径必须为正
])
def test_validate_rejects_bad_sampling_keys(key, bad):
    cfg = CaseConfig.from_yaml(CFG_PATH)
    setattr(cfg.sampling, key, bad)
    with pytest.raises(ValueError, match=key):
        cfg.validate()


def test_validate_rejects_unknown_scheme():
    """nodes 方案已删：旧 YAML 必须报错，不能悄悄按默认值跑。"""
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.sampling.scheme = "nodes"
    with pytest.raises(ValueError, match="采样方案"):
        cfg.validate()


def test_validate_rejects_unknown_extension_method():
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.level_set.extension_method = "ffm"
    with pytest.raises(ValueError, match="extension_method"):
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
