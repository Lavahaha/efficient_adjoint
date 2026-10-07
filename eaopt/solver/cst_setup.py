"""CST 一侧的单一事实来源：物理常量、会话参数与工程规则（纯数据，不 import cst）。

这些量以前散在 ``configs/*.yaml``（solver 块、substrate/metal/ports）与
模板模块两处，靠测试锁一致性——改一处忘一处**不会报错**，只会出错数据。
现在统一到这里：YAML 只留优化配置（设计区、采样、优化器……），
CST 侧的一切（模板布局、频点、材料、端口、求解设置、场导出步长）都在本模块。

算例数据 = ``COUPLER``（论文 III-A 高定向性定向耦合器）与 ``DIVIDER``
（论文 III-B 不等分 Wilkinson 功分器）；"算例名 → (常量, 模板模块)"的分发在
``eaopt.solver.case.load_case``。**改算例就改这里**，模板命令块
（``cst_model`` / ``cst_model_divider``）与 pipeline（伴随公式用的 ω、εr）
都从这里取值：改 ``frequency_ghz`` 时监视器频点、扫频带、S 参数读取频点与
形状导数会同时跟着变。

几何布局（论文 Fig. 5；单位 mm，z=0 为基板顶面；**功分器的布局在
``cst_model_divider`` 模块头部**）：
    直通线（固定，端口 1-2）：y∈[1.0,2.6]（w=1.6），x 贯通整块板
    耦合臂（"⊓"形）：横段 y∈[−1.6,0]（w=1.6）位于两腿之间，
        两端各一条腿 x∈[−1.6,0] / [12,13.6] 垂直下到板底；
        设计区 x∈[0,12]（= 两腿内边缘之间 = d）内的横段可动，
        腿与直通线固定
    拐弯过渡：耦合臂是等宽条带以圆角拐弯（外缘 R=2.0、内缘 R=0.4 同心）
    耦合间距 g = 1.0（直通线下边缘 y=1.0 与臂上边缘 y=0 之间）
    基板 Rogers4350B 30mil；接地 PEC；空气盒把计算域撑到端口面所需高度
    端口：1/2 在直通线两端，3/4 在两腿底（fwd 激励 1、bwd 激励 3）

端口角色（论文 III-A）：1 input、2 through、3 observation（反向激励 + FoM 观测）、
4 auxiliary。fwd/bwd 各自激励哪个端口由 ``stimulus()`` 从 YAML 的 objective
取值——**规则在 CST 代码里，值在优化配置里**，不重复定义。

场导出步长（``export_step_mm``）是 CST 侧参数，缺省跟随优化侧的
``CaseConfig.field_export_step_mm``：``sampling.scheme=intersection`` 时
= 设计区网格步长（WLS 邻域半径以场格数计，步长 = 拟合分辨率；**不要求
导出网格与 φ 网格对齐**——导出原点由 CST 包围盒定，实测 y −3.55 与设计区
−2.6 差半格，取场按坐标做）；``contour`` 时 = 采样点距（论文经验
0.1~0.5 mm）。导出点数按步长的立方增长，改步长前先算一下文件大小
（0.2 mm → 13 MB，0.1 mm → 4 倍）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = ["CstSetup", "COUPLER", "DIVIDER"]


@dataclass(frozen=True)
class CstSetup:
    """一个算例在 CST 侧的全部常量与规则（默认值 = 论文 III-A 耦合器）。"""

    # ---- 物理 / 材料（同时供优化器用：pipeline 的 ω 与伴随公式的 εr）----
    frequency_ghz: float = 5.0      # 优化频点 = 监视器频点 = S 参数读取频点
    eps_r: float = 3.66             # Rogers4350B
    loss_tangent: float = 0.0037
    substrate_h_mm: float = 0.762   # 30 mil
    metal_thickness_mm: float = 0.035   # 35 um
    metal_material: str = "pec"     # 推导假设 PEC；FD 不达标再换 copper
    ports: tuple[int, ...] = (1, 2, 3, 4)   # 模板里建了 4 个端口

    # ---- 会话 / 运行（原 YAML 的 solver 块）----
    attach_gui: bool = False            # True = 只附接运行中的 CST 实例（无 headless 许可时用）
    port_power_w: float = 0.5           # 端口功率（CST 默认 0.5 W）
    export_step_mm: Optional[float] = None    # 面内(x,y)场导出步长；None=跟随 CaseConfig.field_export_step_mm
    export_step_z_mm: Optional[float] = None  # z 向导出步长；None=与面内同值（见 resolve_export_steps）
    save_fields: bool = False           # 是否把 E/H 场也写进 iter_NNN/（.npz，MB 量级）
    project_dir: Optional[str] = None   # 工程目录；None=<output.dir>/cst
    project_names: tuple[str, str] = ("fwd", "bwd")   # 两个工程的 tag（正向/反向激励）

    # ---- 无人值守（模态框）----
    # CST 在"已有上一轮结果"的工程上重跑仿真会弹确认框问要不要删掉旧结果，
    # GUI 模式下脚本会一直卡在那行等人工点。两道闸：① 连上就切静默模式
    # （抑制消息框）；② 每轮改形状前先把结果清掉（旧结果不存在，框就无从弹起）。
    # 关掉它们只在有人盯着点框时才合理。
    quiet_mode: bool = True             # 连上就 DesignEnvironment.set_quiet_mode(True)
    clear_results: bool = True          # 每轮改形状前 DeleteResults（控制宏，不进历史表）

    # ------------------------------------------------------------------ #
    @property
    def fmax_ghz(self) -> float:
        """时域求解频段上界 = 2×f0（决定自适应网格与脉冲带宽，显式写进模板）。"""
        return 2.0 * self.frequency_ghz

    def project_path(self, tag: str, outdir, name: str) -> Path:
        """该 tag 的 CST 工程路径。

        缺省放在输出目录里（``<output.dir>/cst/<name>_<tag>.cst``）：工程是
        **中间产物**，与结果同生共死；放在仓库里既污染工作区，也会和仓库根的
        ``cst/`` 撞名（那个目录会被当成 Python 命名空间包，把官方库挡住——
        见 docs/server_runbook.md 第 0 节）。
        """
        if tag not in self.project_names:
            raise ValueError(f"tag 只能是 {self.project_names}，收到 {tag!r}")
        base = Path(self.project_dir) if self.project_dir else Path(outdir) / "cst"
        return base / f"{name}_{tag}.cst"

    def stimulus(self, tag: str, objective) -> int:
        """该工程只激励哪个端口：fwd = objective.from_port，bwd = to_port。

        激励"烤"在工程里（建工程时写死 StimulationPort），此后 pipeline
        永不触碰激励 API——多激励叠加的场会让伴随梯度静默失效，而 S 参数
        照样出数，光看 S 参数发现不了。
        """
        if tag == "fwd":
            return int(objective.from_port)
        if tag == "bwd":
            return int(objective.to_port)
        raise ValueError(f"tag 只能是 {self.project_names}，收到 {tag!r}")

    def resolve_export_step(self, sampling_step_mm: float) -> float:
        """面内场导出步长（mm）：显式给了就用它，否则跟随边界采样点距。"""
        return float(self.export_step_mm) if self.export_step_mm \
            else float(sampling_step_mm)

    def resolve_export_steps(self, sampling_step_mm: float) -> tuple[float, float]:
        """(面内, z) 两个场导出步长（mm）——ASCIIExport 逐轴给步长。

        面内步长决定 WLS 拟合分辨率（半径以场格数计），z 步长只决定采样面
        能吸附到哪些 z 面（每轴点数按各自步长线性增长）。功分器算例把面内
        放到 0.2 mm 省导出体积（±1.6 mm 厚的平板层在 5 GHz 下沿 z 变化很慢），
        但 z 保持 0.1 mm 以便 `sampling.field_z_mm=-0.1` 附近有近处的网格面。
        """
        xy = self.resolve_export_step(sampling_step_mm)
        return xy, (float(self.export_step_z_mm) if self.export_step_z_mm else xy)

    def validate_objective(self, objective) -> None:
        """优化目标用的端口必须是模板里建了的端口（替代 config 侧的校验）。"""
        for attr in ("from_port", "to_port"):
            port = int(getattr(objective, attr))
            if port not in self.ports:
                raise ValueError(
                    f"objective.{attr}={port} 不是模板中的端口 {self.ports}")


# 论文 III-A：高定向性定向耦合器（5 GHz；初始 w=1.6, d=12, g=1；
# 目标 max|S31|：-17.9 dB → 约 -10 dB，约 20 次迭代）
COUPLER = CstSetup()

# 论文 III-B：不等分 Wilkinson 功分器（5 GHz；初始 w=2，输入 λg/2、分路臂 λg/4；
# 目标 max|S21|，即把 1:1 的等分推到 9:1）。基板换成 Rogers3003（论文原文），
# 端口只有三个（1 输入、2/3 输出）。几何布局见 cst_model_divider。
# 场导出面内放到 0.2 mm：导出体积按步长的立方走，而这个算例的计算域是三端口
# 的大平面板，0.1 mm 下每份文件要 200 MB 以上；z 仍留 0.1 mm 给采样面吸附。
DIVIDER = CstSetup(eps_r=3.0, loss_tangent=0.001, ports=(1, 2, 3),
                   export_step_mm=0.2, export_step_z_mm=0.1)
