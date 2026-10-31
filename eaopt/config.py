"""算例配置系统（**只描述优化问题**）。

分工：本模块管优化侧——设计区、初始/固定金属、采样、约束、优化器、水准集、
目标函数与输出目录；**CST 侧的一切**（模板布局、频点、基板/金属材料、端口、
求解设置、场导出步长）在 ``eaopt/solver/cst_setup.py``，不在这里出现。
两者共享的物理量（频点、εr）以 cst_setup 为单一来源，runner 从那取值。

一个算例 = 一份 YAML（优化配置）+ 一个 ``CstSetup``（CST 配置）。

坐标约定（全局）：
  - 设计平面为 x-y 平面，单位 mm；
  - 金属层沿 z 向挤出（厚度在 cst_setup）；
  - 水准集定义域为设计区域包围盒 DesignRegionSpec.box。
"""

# 注意：不引入 from __future__ import annotations —— _build 依赖运行期
# 真实类型对象来递归构造嵌套 dataclass，字符串注解会使其失效。
import dataclasses
import math
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
    # Optional multi-frequency target. When set, the optimizer maximizes a
    # smooth minimum of the dB margins relative to target_db.
    frequencies_ghz: list[float] | None = None
    target_db: float = -3.0
    beta_per_db: float = 1.0


@dataclass
class DesignRegionSpec:
    box: BoxSpec
    grid_step_mm: float = 0.05
    # 场数据提取范围在设计区基础上向外扩展的余量（mm）
    field_margin_mm: float = 1.0


@dataclass
class SamplingSpec:
    sample_side: str = "outside"
    intersection_offset_mm: float = 0.0
    field_z_mm: float = 0.0


@dataclass
class OptimizerSpec:
    pseudo_time_step: float = 1.0e-8
    cfl: float = 0.5
    velocity_sign: float = 1.0
    max_iterations: int = 30


@dataclass
class LevelSetSpec:
    extension_band_mm: float = 0.5


@dataclass
class ConstraintSpec:
    min_gap_mm: float = 0.0  # 金属间最小间距（论文 0.1 mm）
    min_feature_mm: float = 0.0  # 金属线宽、孔径和间隙的最小过滤尺度；0=关闭
    # 可动金属的活动范围（速度掩膜区域），None 表示整个设计区都可动。
    # 论文只有一个设计区：design_region.box 就是可动范围，**这里留空**。
    # 保留该字段是为了"设计区比可动范围大"的算例（如盒子要覆盖固定障碍时）。
    allowed_region: Optional[BoxSpec] = None
    # 连通性硬约束（缺省关）。开启后：可动金属必须**始终是一块**、且贴着
    # 初始那几个"穿出设计区"的锚点（论文 III-B："to ensure the in-phase
    # output of the two output ports ... the microstrip lines are always
    # connected"）。被掐断时按最短路径桥接，桥接节点冻结（速度置 0）。
    # 锚点从初始 φ 在设计区边界上的金属段**自动推导**，不写进 YAML。
    require_connected: bool = False
    # 速度掩膜边缘 taper 作用在哪些边（"xy"/"x"/"y"/"none"）。
    # 语义：taper 让可动金属在**穿出设计区的边**上与区外固定馈线平滑衔接。
    # 因此只有当可动金属贴着该边穿过时才 taper 它——若设计区某条边本身就是
    # 可动金属的生长边界（论文设计区的上下边），在那条边上 taper 等于把
    # 优化目标本身削掉（本算例的上界离直通线只有 0.1 mm，1 mm 的 taper 会
    # 把整个耦合间隙的速度压掉）。-- 早期"四边全 taper"是因为盒子只是数值
    # 网格范围、比真正的设计区大得多；两域合一后必须按物理含义选边。
    taper_edges: str = "xy"


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
    # 完整初始金属多边形；设计区外的部分固定保留，区内用 φ<0 演化。
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
        if self.objective.frequencies_ghz is not None:
            freqs = self.objective.frequencies_ghz
            if len(freqs) < 2 or any(
                    not math.isfinite(float(f)) or float(f) <= 0 for f in freqs):
                raise ValueError("objective.frequencies_ghz 至少包含两个正频点")
            if any(float(a) >= float(b) for a, b in zip(freqs, freqs[1:])):
                raise ValueError("objective.frequencies_ghz 必须严格递增且无重复")
            if (not math.isfinite(float(self.objective.target_db)) or
                    not math.isfinite(float(self.objective.beta_per_db)) or
                    self.objective.beta_per_db <= 0):
                raise ValueError(
                    "objective.target_db 必须有限，beta_per_db 必须为有限正数")
        if self.design_region is None:
            raise ValueError("缺少 design_region 配置")
        self.design_region.box.validate("design_region.box")
        if self.design_region.grid_step_mm <= 0:
            raise ValueError("design_region.grid_step_mm 必须为正")
        # 步长必须整除设计区：LevelSet2D 用 linspace 铺格，除不尽时**实际格距
        # ≠ grid_step_mm 而没有任何报错**——φ 网格被悄悄拉伸，所有按格数
        # 几何距离与网格坐标的换算也会随之偏移。
        for axis in ("width", "height"):
            k = getattr(self.design_region.box, axis) / self.design_region.grid_step_mm
            if abs(k - round(k)) > 1e-9:
                raise ValueError(
                    f"design_region.grid_step_mm={self.design_region.grid_step_mm} "
                    f"除不尽设计区{axis} {getattr(self.design_region.box, axis)} mm"
                    f"（={k:.6g} 格）：φ 网格会被 linspace 悄悄拉伸。"
                    "请改步长或改设计区尺寸。")
        # 端口表在 CST 模板里（cst_setup.ports）；objective 的端口合法性由
        # CstSetup.validate_objective 在装配点校验（这里不知道模板）。
        for i, poly in enumerate(self.initial_metal):
            poly.validate(f"initial_metal[{i}]")
        for i, poly in enumerate(self.fixed_region):
            poly.validate(f"fixed_region[{i}]")
        if self.constraints.allowed_region is not None:
            self.constraints.allowed_region.validate("constraints.allowed_region")
        if self.constraints.min_feature_mm < 0:
            raise ValueError("constraints.min_feature_mm 不能为负")
        if (self.constraints.min_feature_mm > 0 and
                self.constraints.min_feature_mm <
                2.0 * self.design_region.grid_step_mm):
            raise ValueError(
                "constraints.min_feature_mm 至少应为两个设计网格单元")
        if self.constraints.require_connected and not self.initial_metal:
            raise ValueError(
                "constraints.require_connected=true 但没有 initial_metal："
                "锚点（金属穿出设计区的那几段）是从初始 φ 推出来的，没有"
                "初始金属就无从谈起——多半是配置写错了")
        if self.constraints.taper_edges not in ("xy", "x", "y", "none"):
            raise ValueError(
                f"constraints.taper_edges={self.constraints.taper_edges!r} 未知："
                '只认 "xy" / "x" / "y" / "none"（速度掩膜 taper 作用在哪些边）')
        if self.sampling.sample_side not in ("outside", "inside"):
            raise ValueError(f"未知采样侧 {self.sampling.sample_side}")
        if self.sampling.intersection_offset_mm < 0:
            raise ValueError("sampling.intersection_offset_mm 不能为负")
        if (not math.isfinite(self.optimizer.pseudo_time_step) or
                self.optimizer.pseudo_time_step <= 0.0):
            raise ValueError("optimizer.pseudo_time_step 必须为有限正数")
        if not 0.0 < self.optimizer.cfl <= 0.5:
            raise ValueError("optimizer.cfl 需在 (0, 0.5]（HJ 一阶格式稳定域）")
        if self.optimizer.velocity_sign not in (1.0, -1.0):
            raise ValueError("optimizer.velocity_sign 必须为 +1.0 或 -1.0")
        if self.optimizer.max_iterations <= 0:
            raise ValueError("optimizer.max_iterations 必须为正")

    @property
    def field_export_step_mm(self) -> float:
        """CST 场导出步长；缺省与水准集网格步长一致。"""
        return float(self.design_region.grid_step_mm)

    def summary(self) -> str:
        return (f"Case: {self.name} ({self.description})\n"
                f"Design box: {self.design_region.box}; dx={self.design_region.grid_step_mm} mm\n"
                f"Objective frequencies: {self.objective.frequencies_ghz or 'setup frequency'}\n"
                f"Sampling: {self.sampling.sample_side}, offset={self.sampling.intersection_offset_mm} mm, z={self.sampling.field_z_mm} mm\n"
                f"Optimizer: adjoint_levelset, pseudo_dt={self.optimizer.pseudo_time_step:g}, CFL={self.optimizer.cfl}, iterations={self.optimizer.max_iterations}\n"
                f"Output: {self.output.dir}")


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
