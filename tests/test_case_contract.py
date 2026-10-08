"""算例模块契约：``solver/case.py`` 认得的每个算例都要过这道关。

``cst.py``（``_initialize`` 建模板、``_update_design_region`` 每轮换形状）与
三个 CST 脚本只按 :data:`eaopt.solver.case.CASE_INTERFACE` 这套名字调用算例
模块。少一个的代价**不是"用不了"，而是跑到一半才炸**：

    AttributeError: module 'eaopt.solver.cst_model_divider' has no attribute
    'design_region_update'

功分器就是这么炸的——模板建工程（``template_blocks``）一路正常，直到 pipeline
第一次 ``build_model`` 才在 cst.py 里爆，中间隔着一次完整的 CST 建模 + 存盘。
接口检查已经放进 ``load_case``（脚本启动即报错）；本文件的用例守住它别退化。
"""

import types
from pathlib import Path

import pytest

from eaopt.config import CaseConfig
from eaopt.solver import case as case_mod
from eaopt.solver.case import CASE_INTERFACE, known_cases, load_case
from eaopt.solver.cst_setup import CstSetup

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", known_cases())
def test_every_known_case_has_a_yaml_and_a_complete_template_module(name):
    """每个算例：YAML（名字自洽）+ CstSetup + 模板模块接口齐全。"""
    path = REPO / "configs" / f"{name}.yaml"
    assert path.is_file(), f"{name}：没有 configs/{name}.yaml"
    cfg = CaseConfig.from_yaml(path)
    assert cfg.name == name, f"{path} 里的 name 是 {cfg.name!r}"

    setup, model = load_case(cfg)          # 内部就做接口检查，缺一个当场报错
    assert isinstance(setup, CstSetup)
    missing = [a for a in CASE_INTERFACE if not hasattr(model, a)]
    assert not missing, f"{name} 的 {model.__name__} 缺 {missing}"


@pytest.mark.parametrize("name", known_cases())
def test_design_region_update_actually_builds_a_command(name):
    """``design_region_update`` 必须真的能建命令：删组件 + 逐多边形挤出。

    只有名字、不会干活的话，报错会推迟到 CST 那一步（或更糟：静默建出
    空的设计区）。
    """
    cfg = CaseConfig.from_yaml(REPO / "configs" / f"{name}.yaml")
    setup, model = load_case(cfg)
    cmd = model.design_region_update([[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]],
                                     setup.metal_thickness_mm,
                                     component=model.DESIGN_COMPONENT)
    assert isinstance(cmd, str)
    assert "With Extrude" in cmd
    assert model.DESIGN_COMPONENT in cmd
    cmd.encode("ascii")                    # VBA 按 ANSI 解码


def test_interface_check_rejects_a_module_missing_one_name():
    """把用户报的那个 AttributeError 演一遍：模块缺名字必须**在 load_case 里**
    当场拦下（而不是跑到 build_model 才炸）。

    用桩模块而不是真的改坏某个算例：守的是这个守卫本身，与具体几何无关。
    """
    full = types.ModuleType("eaopt.solver.cst_model_full")
    for attr in CASE_INTERFACE:
        setattr(full, attr, object())
    assert case_mod._check_interface(full, "full") is None      # 齐全 → 不抛

    # 缺 design_region_update：功分器真实踩过的那个坑
    stub = types.ModuleType("eaopt.solver.cst_model_divider")
    for attr in CASE_INTERFACE:
        if attr != "design_region_update":
            setattr(stub, attr, object())
    with pytest.raises(AttributeError, match="design_region_update"):
        case_mod._check_interface(stub, "divider")
