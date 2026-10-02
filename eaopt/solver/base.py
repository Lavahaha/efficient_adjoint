"""求解器抽象层。

定义优化闭环与电磁求解器之间的契约：
  - Solution: 一次仿真的完整结果（S 参数、场、入射功率）；
  - SolverInterface: build_model / solve_forward / solve_backward。

求解器无关性是论文方法的核心卖点：pipeline 只依赖本模块的接口；
本仓库的求解器实现是 ``cst.CstSolver``（CST 官方 Python API）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from eaopt.adjoint.fields import FieldGrid

__all__ = ["Solution", "SolverInterface"]


@dataclass
class Solution:
    """一次仿真的结果。

    s_params: {(响应端口, 激励端口): complex}，约定 S_ij = j 端口响应、
    i 端口激励（与 CST 一致）；
    e_field / h_field: FieldGrid 复矢量场（V/m, A/m）；
    pin: 入射功率 (W)。
    """

    s_params: dict
    e_field: FieldGrid
    h_field: FieldGrid
    pin: float
    extra: dict = field(default_factory=dict)


class SolverInterface(ABC):
    """求解器接口：正向/后向两次仿真 + 几何重建。"""

    @abstractmethod
    def build_model(self, movable: list, fixed: list) -> None:
        """重建几何。

        movable: 可动金属轮廓（世界坐标 mm，闭合或开放路径）；
        fixed: 固定金属多边形（世界坐标 mm，含设计区外馈线等）。
        """

    @abstractmethod
    def solve_forward(self) -> Solution:
        """正向仿真：input 端口激励。"""

    @abstractmethod
    def solve_backward(self) -> Solution:
        """后向仿真：observation 端口激励（论文的"伴随仿真"）。"""

    # ------------------------------------------------------------------ #
    # 下面两个是**非抽象**的默认实现：只有需要外部资源/产物目录的求解器
    # 才覆写（CST 覆写两者）。
    # ------------------------------------------------------------------ #
    def begin_iteration(self, iteration: int) -> None:
        """告知当前轮次（用于把产物写进 iter_NNN/，以及日志）。默认不做任何事。"""

    def close(self) -> None:
        """释放资源（CST 会话、打开的工程）。默认不做任何事。

        pipeline 在 finally 里调用——异常退出也必须收尾，否则下一次运行
        会附接到一个状态不明的 CST 实例上。
        """
