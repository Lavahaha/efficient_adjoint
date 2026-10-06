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
    # intersection=边界点取 marching 交点（亚格点）+ WLS 取场 + 反距离平方回写（现行）|
    # contour=沿轮廓弧长重采样 + 三线性取场 + 最近节点回写（旧，留作 A/B）
    scheme: str = "intersection"
    point_spacing_mm: float = 0.2  # contour：边界导数采样点间距（论文经验 0.1~0.5）
    sample_offset_mm: float = 0.05  # contour：采样点沿法向的偏移量（对应论文 delta_z）
    sample_side: str = "outside"  # 两方案共用：outside=金属外侧(PEC) | inside=金属内侧
    # --- intersection 方案参数 ---
    intersection_offset_mm: float = 0.0
    # 交点沿外法向的额外偏移：0 = 场就取在交点（边界）上，用介质侧节点做**单侧**
    # 拟合得到空气侧极限；调成半格（如 0.05）可把外推变内插，对 CST 近金属场
    # 的阶梯噪声更鲁棒（代价是采样点与边界错开半格）。
    wls_radius_cells: float = 2.0   # WLS 邻域半径（单位 = 场导出网格步长）
    wls_order: int = 1              # 1=局部线性 | 2=局部二次
    scatter_max_cells: float = 1.0  # 速度回写的作用半径（φ 网格格数）
    field_z_mm: float = -0.1  # 场采样平面的 z（mm，基板顶面=金属底面为 0，金属在其上方）
                              # 吸附到最近的导出网格面；**必须为负**，否则可能
                              # 吸附进金属体内（PEC 里 E/H≈0 → δp 恒 0，pipeline 会报错）


@dataclass
class OptimizerSpec:
    # 每轮迭代的**最大边界位移** = step_cells × 网格步长（论文 Fig.3b ≈ 1 格）。
    # 由 HJ 演化 steps = round(step_cells/cfl) 个子步实现，每子步位移 = cfl·dx。
    step_cells: float = 1.0
    cfl: float = 0.5  # 子步长系数（HJ 一阶 Godunov 的稳定上界约 0.707＝1/√2）
    velocity_sign: float = 1.0  # 形状导数→速度的符号开关（±1），FD 验证定号
    max_iterations: int = 30
    fom_tolerance: float = 1e-4
    convergence_window: int = 5  # 连续 N 次迭代 FoM 变化小于容差则收敛


@dataclass
class LevelSetSpec:
    reinit_every: int = 5  # 每 N 次迭代重初始化一次
    extension_band_mm: float = 0.5  # 速度延拓带宽（mm）
    # 速度窄带延拓的实现：auto（有 scikit-fmm 就用，缺库打印一行告警后退回
    # PDE 上风延拓）| skfmm（缺库直接报错）| pde（旧实现，留作对照）
    extension_method: str = "auto"


@dataclass
class ConstraintSpec:
    min_gap_mm: float = 0.0  # 金属间最小间距（论文 0.1 mm）
    # 可动金属的活动范围（速度掩膜区域），None 表示整个设计区都可动。
    # 论文只有一个设计区：design_region.box 就是可动范围，**这里留空**。
    # 保留该字段是为了"设计区比可动范围大"的算例（如盒子要覆盖固定障碍时）。
    allowed_region: Optional[BoxSpec] = None
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
        # 步长必须整除设计区：LevelSet2D 用 linspace 铺格，除不尽时**实际格距
        # ≠ grid_step_mm 而没有任何报错**——φ 网格被悄悄拉伸，所有按格数
        # 换算的量（WLS 半径、scatter 半径、步长格数）都会跟着偏。
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
        if self.constraints.taper_edges not in ("xy", "x", "y", "none"):
            raise ValueError(
                f"constraints.taper_edges={self.constraints.taper_edges!r} 未知："
                '只认 "xy" / "x" / "y" / "none"（速度掩膜 taper 作用在哪些边）')
        if self.sampling.scheme not in ("intersection", "contour"):
            raise ValueError(
                f"未知采样方案 {self.sampling.scheme}："
                '只认 "intersection"（交点+WLS）与 "contour"（旧，A/B）')
        if self.sampling.point_spacing_mm <= 0:
            raise ValueError("sampling.point_spacing_mm 必须为正")
        if self.sampling.sample_offset_mm < 0:
            raise ValueError("sampling.sample_offset_mm 不能为负")
        if self.sampling.sample_side not in ("outside", "inside"):
            raise ValueError(f"未知采样侧 {self.sampling.sample_side}")
        if self.sampling.intersection_offset_mm < 0:
            raise ValueError("sampling.intersection_offset_mm 不能为负")
        if self.sampling.wls_radius_cells <= 0:
            raise ValueError("sampling.wls_radius_cells 必须为正")
        if self.sampling.wls_order not in (1, 2):
            raise ValueError("sampling.wls_order 只支持 1（局部线性）或 2（局部二次）")
        if self.sampling.scatter_max_cells <= 0:
            raise ValueError("sampling.scatter_max_cells 必须为正")
        if self.optimizer.step_cells <= 0:
            raise ValueError("optimizer.step_cells 必须为正")
        if not 0.0 < self.optimizer.cfl <= 0.5:
            raise ValueError("optimizer.cfl 需在 (0, 0.5]（HJ 一阶格式稳定域）")
        if self.optimizer.velocity_sign not in (1.0, -1.0):
            raise ValueError("optimizer.velocity_sign 必须为 +1.0 或 -1.0")
        if self.optimizer.max_iterations <= 0:
            raise ValueError("optimizer.max_iterations 必须为正")
        if self.level_set.reinit_every < 1:
            raise ValueError("level_set.reinit_every 至少为 1")
        if self.level_set.extension_method not in ("auto", "skfmm", "pde"):
            raise ValueError(
                f"level_set.extension_method={self.level_set.extension_method!r} "
                '未知：只认 "auto" / "skfmm" / "pde"')

    @property
    def field_export_step_mm(self) -> float:
        """CST 场导出步长。

        ``intersection``：导出网格按坐标被 WLS 使用（邻域半径以场格数计），
        **不要求与 φ 网格对齐**——缺省取 φ 网格步长（分辨率对等）。
        服务器若要省导出体积/时间，可调粗此值并把 wls_radius_cells 相应
        放大（代价是拟合截断误差变大）。``contour``：沿用采样点距。
        """
        if self.sampling.scheme == "intersection":
            return float(self.design_region.grid_step_mm)
        return float(self.sampling.point_spacing_mm)

    def summary(self) -> str:
        """优化侧配置摘要（CST 侧常量见 ``cst_setup.CstSetup``）。"""
        dr = self.design_region.box
        s = self.sampling
        if s.scheme == "intersection":
            sample = (f"marching 交点（亚格点）{s.sample_side}, "
                      f"偏移 {s.intersection_offset_mm} mm, "
                      f"WLS {s.wls_order} 阶 R={s.wls_radius_cells} 格, "
                      f"z={s.field_z_mm} mm（吸附到导出网格面）")
        else:
            sample = (f"轮廓重采样 点距 {s.point_spacing_mm} mm, "
                      f"偏移 {s.sample_offset_mm} mm ({s.sample_side}), "
                      f"z={s.field_z_mm} mm")
        lines = [
            f"算例        : {self.name} — {self.description}",
            f"设计区域    : x∈{dr.x} y∈{dr.y} mm, 网格步长 {self.design_region.grid_step_mm} mm",
            f"目标函数    : max |S{self.objective.to_port}{self.objective.from_port}|",
            f"边界采样    : {s.scheme} — {sample}",
            f"场导出步长  : {self.field_export_step_mm} mm",
            f"速度延拓    : {self.level_set.extension_method}"
            f"（带宽 {self.level_set.extension_band_mm} mm）",
            f"约束        : 最小间距 {self.constraints.min_gap_mm} mm, "
            f"边缘 taper 作用于 {self.constraints.taper_edges} 边"
            + (f", 活动范围 {self.constraints.allowed_region}" if self.constraints.allowed_region else ""),
            f"优化        : 步长 {self.optimizer.step_cells} 格 "
            f"(= {self.optimizer.step_cells * self.design_region.grid_step_mm:g} mm), "
            f"cfl {self.optimizer.cfl}, 最大 {self.optimizer.max_iterations} 次迭代",
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
