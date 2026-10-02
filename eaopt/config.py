"""算例配置系统（**只描述优化问题**）。

分工：本模块管优化侧——设计区、初始/固定金属、采样、约束、优化器、水准集、
目标函数与输出目录；**CST 侧的一切**（模板布局、频点、基板/金属材料、端口、
求解设置、场导出步长）在 ``eaopt/solver/cst_setup.py``，不在这里出现。
两者共享的物理量（频点、εr）以 cst_setup 为单一来源，pipeline 从那取值。

一个算例 = 一份 YAML（优化配置）+ 一个 ``CstSetup``（CST 配置）。

坐标约定（全局）：
  - 设计平面为 x-y 平面，单位 mm；
  - 金属层沿 z 向挤出（厚度在 cst_setup）；
  - 水准集定义域为设计区域包围盒 DesignRegionSpec.box。
"""

# 注意：不引入 from __future__ import annotations —— _build 依赖运行期
# 真实类型对象来递归构造嵌套 dataclass，字符串注解会使其失效。
import dataclasses
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, get_args, get_origin

import yaml

__all__ = [
    "BoxSpec", "PolygonSpec", "ObjectiveSpec", "DesignRegionSpec",
    "SamplingSpec", "OptimizerSpec", "LevelSetSpec", "ConstraintSpec",
    "OutputSpec", "CaseConfig",
]


@dataclass
class BoxSpec:
    """二维矩形区域 [x0, x1] x [y0, y1]，单位 mm。"""

    x: tuple[float, float]
    y: tuple[float, float]

    @property
    def width(self) -> float:
        return self.x[1] - self.x[0]

    @property
    def height(self) -> float:
        return self.y[1] - self.y[0]

    def validate(self, name: str) -> None:
        if not (self.x[0] < self.x[1] and self.y[0] < self.y[1]):
            raise ValueError(f"{name}: 区域无效 {self}")


@dataclass
class PolygonSpec:
    """多边形（顶点序列，mm）。允许开放顶点（使用处自动闭合）。"""

    vertices: list[list[float]]

    def validate(self, name: str) -> None:
        if len(self.vertices) < 3:
            raise ValueError(f"{name}: 多边形顶点不足 3 个")


@dataclass
class ObjectiveSpec:
    """优化目标：最大化 |S_{to,from}|（频点与端口表在 cst_setup）。"""

    type: str = "transmission"  # 目前实现: maximize |S_ij|
    from_port: int = 1
    to_port: int = 3


@dataclass
class DesignRegionSpec:
    box: BoxSpec
    grid_step_mm: float = 0.05
    # 场数据提取范围在设计区基础上向外扩展的余量（mm）
    field_margin_mm: float = 1.0


@dataclass
class SamplingSpec:
    point_spacing_mm: float = 0.2  # 边界导数采样点间距（论文经验 0.1~0.5）
    sample_offset_mm: float = 0.05  # 采样点沿法向的偏移量（对应论文 delta_z）
    sample_side: str = "outside"  # outside=金属外侧(PEC) | inside=金属内侧(损耗金属)
    field_z_mm: float = 0.0  # 场采样平面的 z 坐标（mm，基板顶面为 0）


@dataclass
class OptimizerSpec:
    step_size: float = 0.02  # 固定步长（mm 量级，先跑通后自适应）
    velocity_sign: float = 1.0  # 形状导数→速度的符号开关（±1），FD 验证定号
    max_iterations: int = 30
    fom_tolerance: float = 1e-4
    convergence_window: int = 5  # 连续 N 次迭代 FoM 变化小于容差则收敛


@dataclass
class LevelSetSpec:
    reinit_every: int = 5  # 每 N 次迭代重初始化一次
    extension_band_mm: float = 0.5  # 速度延拓带宽（mm）


@dataclass
class ConstraintSpec:
    min_gap_mm: float = 0.0  # 金属间最小间距（论文 0.1 mm）
    # 可动金属的活动范围（速度掩膜区域），None 表示整个设计区都可动
    allowed_region: Optional[BoxSpec] = None


@dataclass
class OutputSpec:
    dir: str = "results/default"  # 输出目录（迭代日志、形状快照、图）


@dataclass
class CaseConfig:
    """一个算例的完整配置。"""

    name: str = ""
    description: str = ""
    objective: ObjectiveSpec = field(default_factory=ObjectiveSpec)
    design_region: Optional[DesignRegionSpec] = None
    # 设计区内的初始金属多边形（金属在 φ<0 侧）
    initial_metal: list[PolygonSpec] = field(default_factory=list)
    # 固定材料区（速度强制为 0，不参与优化；如直通线、设计区外的馈线段）
    fixed_region: list[PolygonSpec] = field(default_factory=list)
    constraints: ConstraintSpec = field(default_factory=ConstraintSpec)
    sampling: SamplingSpec = field(default_factory=SamplingSpec)
    optimizer: OptimizerSpec = field(default_factory=OptimizerSpec)
    level_set: LevelSetSpec = field(default_factory=LevelSetSpec)
    output: OutputSpec = field(default_factory=OutputSpec)

    # ------------------------------------------------------------------ #
    # 构造与校验
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, d: dict) -> "CaseConfig":
        cfg = _build(cls, d)
        cfg.validate()
        return cfg

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CaseConfig":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f))

    def validate(self) -> None:
        if not self.name:
            raise ValueError("缺少算例名称 name")
        if self.design_region is None:
            raise ValueError("缺少 design_region 配置")
        self.design_region.box.validate("design_region.box")
        if self.design_region.grid_step_mm <= 0:
            raise ValueError("design_region.grid_step_mm 必须为正")
        # 端口表在 CST 模板里（cst_setup.ports）；objective 的端口合法性由
        # CstSetup.validate_objective 在装配点校验（这里不知道模板）。
        for i, poly in enumerate(self.initial_metal):
            poly.validate(f"initial_metal[{i}]")
        for i, poly in enumerate(self.fixed_region):
            poly.validate(f"fixed_region[{i}]")
        if self.constraints.allowed_region is not None:
            self.constraints.allowed_region.validate("constraints.allowed_region")
        if self.sampling.point_spacing_mm <= 0:
            raise ValueError("sampling.point_spacing_mm 必须为正")
        if self.sampling.sample_offset_mm < 0:
            raise ValueError("sampling.sample_offset_mm 不能为负")
        if self.sampling.sample_side not in ("outside", "inside"):
            raise ValueError(f"未知采样侧 {self.sampling.sample_side}")
        if self.optimizer.step_size <= 0:
            raise ValueError("optimizer.step_size 必须为正")
        if self.optimizer.velocity_sign not in (1.0, -1.0):
            raise ValueError("optimizer.velocity_sign 必须为 +1.0 或 -1.0")
        if self.optimizer.max_iterations <= 0:
            raise ValueError("optimizer.max_iterations 必须为正")
        if self.level_set.reinit_every < 1:
            raise ValueError("level_set.reinit_every 至少为 1")

    def summary(self) -> str:
        """优化侧配置摘要（CST 侧常量见 ``cst_setup.CstSetup``）。"""
        dr = self.design_region.box
        lines = [
            f"算例        : {self.name} — {self.description}",
            f"设计区域    : x∈{dr.x} y∈{dr.y} mm, 网格步长 {self.design_region.grid_step_mm} mm",
            f"目标函数    : max |S{self.objective.to_port}{self.objective.from_port}|",
            f"边界采样    : 点距 {self.sampling.point_spacing_mm} mm, "
            f"偏移 {self.sampling.sample_offset_mm} mm ({self.sampling.sample_side}), "
            f"z={self.sampling.field_z_mm} mm",
            f"约束        : 最小间距 {self.constraints.min_gap_mm} mm"
            + (f", 活动范围 {self.constraints.allowed_region}" if self.constraints.allowed_region else ""),
            f"优化        : 固定步长 {self.optimizer.step_size}, "
            f"最大 {self.optimizer.max_iterations} 次迭代",
            f"输出目录    : {self.output.dir}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------- #
# YAML 字典 -> 嵌套 dataclass 的递归构造
# ---------------------------------------------------------------------- #
def _build(tp: Any, value: Any) -> Any:
    """按类型注解递归构造：dataclass / list[T] / Optional[T] / tuple。"""
    if value is None:
        return None
    origin = get_origin(tp)
    if origin is list:
        (item_tp,) = get_args(tp)
        return [_build(item_tp, v) for v in value]
    if origin is tuple:
        return tuple(value)
    if origin in (typing.Union, types.UnionType):  # Optional[X] 等
        inner = next(a for a in get_args(tp) if a is not type(None))
        return _build(inner, value)
    if dataclasses.is_dataclass(tp) and isinstance(value, dict):
        known = {f.name for f in dataclasses.fields(tp)}
        unknown = sorted(set(value) - known)
        if unknown:
            # 拒绝而不是忽略：旧 YAML（solver/substrate/metal/ports 已被删）
            # 若被静默丢弃，跑出来的就是"看着对、其实是默认值"的结果。
            raise ValueError(
                f"{tp.__name__} 不认识这些键：{unknown}；"
                f"合法键：{sorted(known)}")
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name in value:
                kwargs[f.name] = _build(f.type, value[f.name])
        return tp(**kwargs)
    if isinstance(value, dict):
        raise TypeError(f"类型 {tp} 期望字典，收到 {type(value).__name__}")
    return value
