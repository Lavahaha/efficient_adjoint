"""算例配置系统。

设计原则：一个算例 = 一份 YAML 配置。核心模块只依赖 CaseConfig 数据，
不含任何算例硬编码；耦合器、功分器等仅表现为 configs/ 下不同的文件。

坐标约定（全局）：
  - 设计平面为 x-y 平面，单位 mm；
  - 金属层沿 z 向挤出，厚度由 MetalSpec.thickness_mm 给定；
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
    "BoxSpec", "PolygonSpec", "PortSpec", "SubstrateSpec", "MetalSpec",
    "ObjectiveSpec", "DesignRegionSpec", "SamplingSpec", "OptimizerSpec",
    "LevelSetSpec", "ConstraintSpec", "SolverSpec", "OutputSpec", "CaseConfig",
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
class PortSpec:
    id: int
    role: str  # input / through / observation / auxiliary


@dataclass
class SubstrateSpec:
    height_mm: float
    eps_r: float
    loss_tangent: float = 0.0


@dataclass
class MetalSpec:
    thickness_mm: float
    material: str  # "pec" | "copper"
    # copper 时的电导率（S/m），论文原型为 5.8e7
    conductivity_s_per_m: Optional[float] = None


@dataclass
class ObjectiveSpec:
    type: str = "transmission"  # 目前实现: maximize |S_ij|
    from_port: int = 1
    to_port: int = 3
    frequency: float = 5.0


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
class SolverSpec:
    type: str = "mock"  # "mock" | "cst"
    # --- cst 相关（服务器端使用，本地 mock 忽略） ---
    # 两个 CST 工程文件的路径。**缺省**（推荐）= <output.dir>/cst/<name>_fwd.cst
    # 与 _bwd.cst —— 放在输出目录里，与结果同生共死，也不会污染仓库。
    project_fwd: Optional[str] = None   # 正向工程（输入端口激励）
    project_bwd: Optional[str] = None   # 反向工程（观测端口激励）
    # 旧名（COM 时代把这两个路径叫"模板"，由人工建好）：仅作兼容别名，
    # 显式给了 project_* 就以 project_* 为准。新配置不要再用。
    template_fwd: Optional[str] = None
    template_bwd: Optional[str] = None
    # CST 的 Python 库目录（.../AMD64/python_cst_libraries）；None = 自动探测
    cst_python_libs: Optional[str] = None
    # True = 只附接运行中的 CST 实例（服务器无 headless 许可、或想看 GUI 时用）
    attach_gui: bool = False
    # 场导出的采样步长（mm）；None = 取 sampling.point_spacing_mm（0.2）。
    # **不要**用 grid_step_mm（0.05）：导出点数按步长的立方增长。
    export_step_mm: Optional[float] = None
    # 单次求解的超时（秒）；None = 不限时
    solver_timeout_s: Optional[float] = None
    port_power_w: float = 0.5  # 端口功率（CST 默认 0.5 W）


@dataclass
class OutputSpec:
    dir: str = "results/default"  # 输出目录（迭代日志、形状快照、图）
    save_fields: bool = False


@dataclass
class CaseConfig:
    """一个算例的完整配置。"""

    name: str = ""
    description: str = ""
    solver: SolverSpec = field(default_factory=SolverSpec)
    frequency: float = 5.0  # GHz，优化频点
    substrate: Optional[SubstrateSpec] = None
    metal: Optional[MetalSpec] = None
    ports: list[PortSpec] = field(default_factory=list)
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
        if self.frequency <= 0:
            raise ValueError("frequency 必须为正")
        if self.substrate is None:
            raise ValueError("缺少 substrate 配置")
        if self.metal is None:
            raise ValueError("缺少 metal 配置")
        if self.metal.material not in ("pec", "copper"):
            raise ValueError(f"未知金属材料 {self.metal.material}")
        if self.design_region is None:
            raise ValueError("缺少 design_region 配置")
        self.design_region.box.validate("design_region.box")
        if self.design_region.grid_step_mm <= 0:
            raise ValueError("design_region.grid_step_mm 必须为正")
        ids = [p.id for p in self.ports]
        if len(set(ids)) != len(ids):
            raise ValueError("端口 id 重复")
        roles = {p.role for p in self.ports}
        if "input" not in roles or "observation" not in roles:
            raise ValueError("至少需要一个 input 端口和一个 observation 端口")
        if self.objective.to_port not in ids:
            raise ValueError(f"objective.to_port={self.objective.to_port} 不是已定义的端口")
        if abs(self.objective.frequency - self.frequency) > 1e-12:
            raise ValueError("objective.frequency 与顶层 frequency 不一致")
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
        if self.solver.type not in ("mock", "cst"):
            raise ValueError(f"未知求解器类型 {self.solver.type}")
        if self.solver.export_step_mm is not None and self.solver.export_step_mm <= 0:
            raise ValueError("solver.export_step_mm 必须为正（mm）")
        if self.solver.solver_timeout_s is not None and self.solver.solver_timeout_s <= 0:
            raise ValueError("solver.solver_timeout_s 必须为正（秒）")

    def project_path(self, tag: str) -> Path:
        """CST 工程文件的路径（tag ∈ {"fwd", "bwd"}）。

        解析顺序：``solver.project_<tag>`` → 兼容别名 ``solver.template_<tag>``
        → 缺省 ``<output.dir>/cst/<name>_<tag>.cst``。

        缺省值刻意放在输出目录里：工程是**中间产物**，与结果同生共死；
        放在仓库里既会污染工作区，也会和仓库根的 ``cst/`` 宏目录撞名
        （那个目录会被当成 Python 命名空间包，见 cst_session 模块头）。
        """
        if tag not in ("fwd", "bwd"):
            raise ValueError(f"tag 只能是 'fwd'/'bwd'，收到 {tag!r}")
        s = self.solver
        explicit = (s.project_fwd if tag == "fwd" else s.project_bwd) or \
                   (s.template_fwd if tag == "fwd" else s.template_bwd)
        if explicit:
            return Path(explicit)
        return Path(self.output.dir) / "cst" / f"{self.name}_{tag}.cst"

    def stimulus_port(self, tag: str) -> int:
        """该工程只激励哪个端口：fwd = objective.from_port，bwd = to_port。

        激励"烤"在工程里（建工程时写死 StimulationPort），之后 pipeline
        永不触碰激励 API——多激励叠加的场会让伴随梯度静默失效，
        而 S 参数照样出数，光看 S 参数发现不了。
        """
        if tag == "fwd":
            return int(self.objective.from_port)
        if tag == "bwd":
            return int(self.objective.to_port)
        raise ValueError(f"tag 只能是 'fwd'/'bwd'，收到 {tag!r}")

    def export_step_mm(self) -> float:
        """场导出步长（mm）：缺省跟随边界采样点距，不用网格步长。"""
        v = self.solver.export_step_mm
        return float(v) if v else float(self.sampling.point_spacing_mm)

    def summary(self) -> str:
        """生成便于日志/终端显示的配置摘要。"""
        dr = self.design_region.box
        lines = [
            f"算例        : {self.name} — {self.description}",
            f"频率        : {self.frequency} GHz（优化频点）",
            f"基板        : h={self.substrate.height_mm} mm, "
            f"eps_r={self.substrate.eps_r}, tan_d={self.substrate.loss_tangent}",
            f"金属        : {self.metal.thickness_mm} mm, {self.metal.material}",
            f"设计区域    : x∈{dr.x} y∈{dr.y} mm, 网格步长 {self.design_region.grid_step_mm} mm",
            f"目标函数    : max |S{self.objective.to_port}{self.objective.from_port}| "
            f"@ {self.objective.frequency} GHz",
            f"端口角色    : {[(p.id, p.role) for p in self.ports]}",
            f"边界采样    : 点距 {self.sampling.point_spacing_mm} mm, "
            f"偏移 {self.sampling.sample_offset_mm} mm ({self.sampling.sample_side}), "
            f"z={self.sampling.field_z_mm} mm",
            f"约束        : 最小间距 {self.constraints.min_gap_mm} mm"
            + (f", 活动范围 {self.constraints.allowed_region}" if self.constraints.allowed_region else ""),
            f"优化        : 固定步长 {self.optimizer.step_size}, "
            f"最大 {self.optimizer.max_iterations} 次迭代",
            f"求解器      : {self.solver.type}",
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
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name in value:
                kwargs[f.name] = _build(f.type, value[f.name])
        return tp(**kwargs)
    if isinstance(value, dict):
        raise TypeError(f"类型 {tp} 期望字典，收到 {type(value).__name__}")
    return value
