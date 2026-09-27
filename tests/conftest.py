"""测试共用的小型耦合器配置与工厂。

几何（设计平面 mm）：设计区 4×2；耦合臂 y∈[−0.6, 0]（可动）；
直通线 y∈[0.4, 1.0]（固定）；初始间隙 0.4 mm。
"""

from copy import deepcopy

from eaopt.config import (
    BoxSpec,
    CaseConfig,
    ConstraintSpec,
    DesignRegionSpec,
    LevelSetSpec,
    MetalSpec,
    OptimizerSpec,
    OutputSpec,
    PolygonSpec,
    PortSpec,
    SamplingSpec,
    SubstrateSpec,
)

ARM_TOP = 0.0
LINE_BOTTOM = 0.4


def make_compact_cfg(tmp_path, min_gap: float = 0.1, velocity_sign: float = 1.0,
                     arm_top: float = ARM_TOP, max_iterations: int = 8) -> CaseConfig:
    return CaseConfig(
        name="compact-test",
        frequency=5.0,
        substrate=SubstrateSpec(height_mm=0.762, eps_r=3.66, loss_tangent=0.0037),
        metal=MetalSpec(thickness_mm=0.035, material="pec"),
        ports=[PortSpec(1, "input"), PortSpec(2, "through"), PortSpec(3, "observation")],
        design_region=DesignRegionSpec(
            box=BoxSpec(x=(0.0, 4.0), y=(-1.0, 1.0)),
            grid_step_mm=0.1,
            field_margin_mm=0.5,
        ),
        initial_metal=[
            PolygonSpec(vertices=[[0.0, -0.6], [4.0, -0.6], [4.0, arm_top], [0.0, arm_top]]),
            PolygonSpec(vertices=[[0.0, LINE_BOTTOM], [4.0, LINE_BOTTOM],
                                  [4.0, 1.0], [0.0, 1.0]]),
        ],
        fixed_region=[
            PolygonSpec(vertices=[[0.0, LINE_BOTTOM], [4.0, LINE_BOTTOM],
                                  [4.0, 1.0], [0.0, 1.0]]),
        ],
        constraints=ConstraintSpec(min_gap_mm=min_gap),
        # 偏移 0.06 > 半网格（dx=0.1/2）：插值窗口不得跨过金属边界，
        # 否则金属侧 E=0 会把采样场压掉一半（见 FD 测试教训）
        sampling=SamplingSpec(point_spacing_mm=0.2, sample_offset_mm=0.06,
                              sample_side="outside"),
        # convergence_window > max_iterations：mock 的量化 FoM 在
        # 亚台阶移动期间完全不变，会误触发收敛窗口提前退出
        # （真实求解器的平滑 S 参数无此问题，收敛逻辑本身不变）
        optimizer=OptimizerSpec(step_size=0.02, velocity_sign=velocity_sign,
                                max_iterations=max_iterations,
                                fom_tolerance=1e-4,
                                convergence_window=max_iterations + 10),
        level_set=LevelSetSpec(reinit_every=5, extension_band_mm=0.3),
        output=OutputSpec(dir=str(tmp_path / "results")),
    )


def with_arm_top(cfg: CaseConfig, arm_top: float) -> CaseConfig:
    """返回臂上边缘抬到 arm_top 的配置副本。"""
    cfg2 = deepcopy(cfg)
    cfg2.initial_metal[0].vertices[2][1] = arm_top
    cfg2.initial_metal[0].vertices[3][1] = arm_top
    return cfg2
