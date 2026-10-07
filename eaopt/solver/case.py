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

__all__ = ["load_case", "known_cases"]


def known_cases() -> tuple[str, ...]:
    """有 ``CstSetup`` + 模板模块的算例名（= 合法的 YAML ``name``）。"""
    return ("coupler", "divider")


def load_case(cfg: CaseConfig) -> tuple[CstSetup, object]:
    """按 ``cfg.name`` 取 ``(CstSetup, 模板模块)``。

    模板模块的接口与 ``cst_model`` 一致（``template_blocks`` /
    ``DESIGN_COMPONENT`` / ``design_region_update`` 等），求解器与三个脚本
    只按这套接口调用。
    """
    if cfg.name == "coupler":
        from eaopt.solver import cst_model as model
        return COUPLER, model
    if cfg.name == "divider":
        from eaopt.solver import cst_model_divider as model
        from eaopt.solver.cst_setup import DIVIDER
        return DIVIDER, model
    raise ValueError(
        f"未知算例 name={cfg.name!r}：已知 {known_cases()}。"
        "算例 = cst_setup.py 里的 CstSetup + cst_model*.py 模板模块 + 这里"
        "的一行分发；新增算例见 eaopt/solver/case.py 模块说明。")
