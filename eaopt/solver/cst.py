"""CST 求解器实现（第 6 步完成，需服务器环境与 CST 版本确认）。"""

from __future__ import annotations

from eaopt.config import CaseConfig
from eaopt.solver.base import SolverInterface


class CstSolver(SolverInterface):
    def __init__(self, cfg: CaseConfig):
        raise NotImplementedError("CST 接口在第 6 步实现（需服务器环境）")
