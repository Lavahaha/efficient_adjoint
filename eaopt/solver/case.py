"""算例分发：YAML 里的 ``name`` → （CST 侧常量 ``CstSetup``, 模板模块）。

一个算例 = 一份 YAML（优化配置） + 一个 ``CstSetup``（cst_setup.py） +
一个模板模块（cst_model*.py）。三个 CST 脚本与 ``run_*.py`` 都要拿后两样，
**分发只放这一处**：散在各脚本里写 if/else 迟早漏改一个，而症状是某个
脚本按另一个算例的几何建工程——S 参数照样出数，只是全错。

新增算例 = ① 写 ``CstSetup`` ② 写模板模块 ③ 在 :func:`load_case` 加一行
+ ④ 写 YAML 并把 ``name`` 设成同一个名字。

名字是**严格**的：对不上就报错，不做"猜一个最像的"——拼错算例名去跑另一套
几何，是本项目里最贵的一类错误（看起来在跑、结果全错）。
"""

from __future__ import annotations

from eaopt.config import CaseConfig
from eaopt.solver.cst_setup import COUPLER, CstSetup

__all__ = ["load_case", "known_cases", "CASE_INTERFACE"]

#: 求解器与三个 CST 脚本按这套名字调用模板模块：``cst.py`` 的 ``_initialize``
#: （``template_blocks``）与 ``_update_design_region``（``design_region_update``
#: + ``DESIGN_COMPONENT``）、``cst_init_*.py``、``cst_update.py``；
#: ``model_blocks`` / ``setting_blocks`` / ``layout_view`` 供测试与 layout 脚本用。
#: **少一个的代价不是"用不了"，而是跑到一半才炸**：功分器漏了
#: ``design_region_update``，建工程一切正常，直到 pipeline 第一次
#: ``build_model`` 才在 cst.py 里 AttributeError——中间隔着一次完整的 CST
#: 建模 + 存盘（`tests/test_case_contract.py` 在本地把它挡掉）。
CASE_INTERFACE = ("template_blocks", "model_blocks", "setting_blocks",
                  "DESIGN_COMPONENT", "design_region_update", "layout_view")


def known_cases() -> tuple[str, ...]:
    """有 ``CstSetup`` + 模板模块的算例名（= 合法的 YAML ``name``）。"""
    return ("coupler", "divider")


def _check_interface(model, name: str) -> None:
    """模板模块必须实现 :data:`CASE_INTERFACE`——**这里就报错，别等跑到那一步**。"""
    missing = [a for a in CASE_INTERFACE if not hasattr(model, a)]
    if missing:
        raise AttributeError(
            f"算例 {name!r} 的模板模块 {model.__name__} 缺 {missing}。"
            f"求解器与 CST 脚本按 {CASE_INTERFACE} 这套接口调用，缺一个就会在"
            "用到它那一步才炸（见 eaopt/solver/case.py 的 CASE_INTERFACE）。")


def load_case(cfg: CaseConfig) -> tuple[CstSetup, object]:
    """按 ``cfg.name`` 取 ``(CstSetup, 模板模块)``，并当场校验模块接口。

    模板模块的接口与 ``cst_model`` 一致（``template_blocks`` /
    ``DESIGN_COMPONENT`` / ``design_region_update`` 等），求解器与三个脚本
    只按这套接口调用。
    """
    if cfg.name == "coupler":
        from eaopt.solver import cst_model as model
        _check_interface(model, cfg.name)
        return COUPLER, model
    if cfg.name == "divider":
        from eaopt.solver import cst_model_divider as model
        from eaopt.solver.cst_setup import DIVIDER
        _check_interface(model, cfg.name)
        return DIVIDER, model
    raise ValueError(
        f"未知算例 name={cfg.name!r}：已知 {known_cases()}。"
        "算例 = cst_setup.py 里的 CstSetup + cst_model*.py 模板模块 + 这里"
        "的一行分发；新增算例见 eaopt/solver/case.py 模块说明。")
