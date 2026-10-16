"""优化闭环（论文 Fig. 4）。

每轮迭代：
  提取轮廓 → 重建几何 → 正向仿真 → FoM 评估/收敛判断
  → 后向仿真 → 边界采样 + 形状导数（式 54–55）→ 速度装配（固定步长
  + 符号开关）→ 栅格化 + 速度延拓 → 掩膜 → HJ 演化
  → 最小间距投影 → 周期性重初始化 → 日志/快照。

输出（cfg.output.dir）：history.jsonl（逐迭代记录）、iter_NNN.png（左 = 形状
快照，右 = 采样面 |E|）、fom.png（FoM 曲线）、sparams_vs_iter.png（各 S 参数
随迭代，论文 Fig.11）、efield_evolution.png（电场分布演化，论文 Fig.10），
以及 iter_NNN/ls_phi.npz（本轮 φ 快照，续跑用）与 iter_NNN/efield_fwd.npz
（采样面 E 切片，画图用）；iter_NNN/ 里的仿真产物（shape.json、
s_params_*.json、meta.json）由求解器写。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from eaopt import artifacts
from eaopt.adjoint.derivative import shape_derivative
from eaopt.adjoint.fields import build_wls_stencil
from eaopt.adjoint.sampling import (
    boundary_points,
    boundary_samples,
    field_node_mask,
    scatter_inverse_distance,
)
from eaopt.config import CaseConfig
from eaopt.geometry.extension import extend_velocity
from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import (ConnectivityGuard, apply_min_gap,
                                        build_velocity_mask, interp_mask_at)
from eaopt.optimize.objective import make_fom
from eaopt.optimize.step import fixed_step_velocity
from eaopt.solver.cst_setup import COUPLER, CstSetup

__all__ = ["make_level_set", "movable_contours", "History", "OptimizerPipeline"]

#: 连通性约束连续失败（子步回滚）多少次就停。一次失败可能是某个子步速度场
#: 恰好把细颈剪断、而桥接也铺不出走廊；连续失败说明速度场整体在撕裂结构。
_MAX_ROLLBACKS = 3

# z=0 的界面坐标由 CST 导出并经规则网格重采样；容忍浮点舍入误差，但
# 容差远小于金属厚度，不能把真正位于 PEC 内的导出面当作界面面。
_Z_INTERFACE_TOL_MM = 1e-6


def make_level_set(cfg: CaseConfig) -> LevelSet2D:
    """由配置的初始金属多边形构造水准集。"""
    ls = LevelSet2D(cfg.design_region.box, cfg.design_region.grid_step_mm)
    ls.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg.initial_metal])
    return ls


def movable_contours(ls: LevelSet2D, cfg: CaseConfig) -> list[np.ndarray]:
    """提取可动金属轮廓（排除固定金属，如直通线），供 CST 重建几何。

    **模块级函数**，不是 OptimizerPipeline 的私有方法：cst_init_* 与
    cst_update 脚本要用同一份"初始轮廓"去建/改 CST 工程，各写一份迟早
    漂移（脚本里看到的形状 ≠ 迭代里演化的形状，而且不报错）。

    返回**零等值线交点折线**（亚格点，与灵敏度分析的采样点是同一份离散）。
    """
    return boundary_points(ls, cfg)


@dataclass
class History:
    records: list[dict] = field(default_factory=list)
    converged: bool = False
    stop_reason: str = "max_iterations"


class OptimizerPipeline:
    def __init__(self, cfg: CaseConfig, solver, ls: LevelSet2D | None = None,
                 setup: CstSetup = COUPLER):
        self.cfg = cfg
        self.solver = solver
        self.ls = ls if ls is not None else make_level_set(cfg)
        self.setup = setup
        self.fom = make_fom(cfg)
        # ω 与 εr 取自 CST 侧单一事实来源（模板里的频点/基板是同一份数据，
        # 改一处两边同时变；伴随公式与仿真模型不会悄悄脱节）。
        self.omega = 2.0 * np.pi * setup.frequency_ghz * 1e9
        self.eps_r = setup.eps_r
        # 连通性硬约束（论文 III-B）：锚点在**初始** φ 上推导，此后不变
        self.guard = (ConnectivityGuard(self.ls, cfg)
                      if cfg.constraints.require_connected else None)
        self._rollbacks = 0        # 连续回滚计数（成功一次即清零）
        self.history = History()
        self._z_plane_logged = False
        self.outdir = Path(cfg.output.dir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self._log_file = self.outdir / "history.jsonl"

    # ------------------------------------------------------------------ #
    def run(self) -> History:
        cfg = self.cfg
        opt = cfg.optimizer
        try:
            for it in range(opt.max_iterations):
                t0 = time.perf_counter()
                # 轮次号告诉求解器，产物（iter_NNN/）才能落在正确的目录里
                self.solver.begin_iteration(it)
                movable = movable_contours(self.ls, cfg)
                fixed = [np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region]

                self.solver.build_model(movable, fixed)
                # 存产生本轮形状的 φ_it（续跑/`cst_update --from-ls` 的唯一
                # 事实来源）。不能放在 ls.update 之后：那记录的是尚未仿真的
                # φ_{it+1}，重算的轮廓会和本轮 shape.json 对不上。
                artifacts.save_ls_phi(self.outdir, it, self.ls)
                sol_f = self.solver.solve_forward()
                fom = self.fom(sol_f)
                rec = {
                    "iteration": it,
                    "fom": float(fom),
                    "s31_db": float(20 * np.log10(abs(sol_f.s_params[(3, 1)]))),
                    "s21_db": float(20 * np.log10(abs(sol_f.s_params[(2, 1)]))),
                    # 复数相角：目标 |S21| 必须用 Re[e^{-j·argS21}·∂S21] 才等价，
                    # 缺相角是形状导数反号的根源（2026-10-12 已由场数据坐实）
                    "arg_s21_deg": float(np.angle(sol_f.s_params[(2, 1)], deg=True)),
                    "arg_s31_deg": float(np.angle(sol_f.s_params[(3, 1)], deg=True)),
                    "time_s": time.perf_counter() - t0,
                }
                self._record(rec)
                self._snapshot(it, sol_f)
                print(f"[iter {it:3d}] FoM={fom:.6f}  |S31|={rec['s31_db']:7.2f} dB  "
                      f"({rec['time_s']:.2f}s)")

                if self._converged():
                    self.history.converged = True
                    self.history.stop_reason = "converged"
                    break

                # ---- 形状导数 -> 速度 -> 几何更新 ----
                sol_b = self.solver.solve_backward()
                V = self._assemble_velocity(sol_f, sol_b, movable)

                steps = max(1, round(opt.step_cells / opt.cfl))
                for _ in range(steps):
                    # 子步前留一份 φ：连通性补不上来时整子步回滚（不能带着
                    # "断成两半"的中间态继续演化）
                    phi_before = self.ls.phi.copy() if self.guard else None
                    self.ls.update(V, steps=1, cfl=opt.cfl)
                    # 投影下沉到每个子步：一轮走多步时中间态也可能穿进禁区
                    apply_min_gap(self.ls, cfg)
                    if self.guard is not None:
                        if self.guard.apply(self.ls):
                            self._rollbacks = 0
                        else:
                            self.ls.phi = phi_before
                            self._rollbacks += 1
                            if self._rollbacks >= _MAX_ROLLBACKS:
                                break   # 剩下的子步会同样失败，别再空转
                if self._rollbacks >= _MAX_ROLLBACKS:
                    self.history.stop_reason = "connectivity"
                    print(f"\n[约束] 连续 {self._rollbacks} 次子步回滚：速度场在"
                          "持续撕裂结构，停止。检查 optimizer.step_cells / cfl、"
                          "或约束参数（见 ConnectivityGuard）。")
                    break
                if it % cfg.level_set.reinit_every == 0:
                    self.ls.reinitialize(band=2.0 * cfg.level_set.extension_band_mm, iters=40)
            else:
                self.history.stop_reason = "max_iterations"
        finally:
            # 求解器持有外部资源（CST 会话、打开的工程）——异常退出也要收尾，
            # 否则下一次运行会附接到一个状态不明的实例上。
            self.solver.close()

        self._plot_fom()
        self._plot_results()
        print(f"\n优化结束（{self.history.stop_reason}）：{len(self.history.records)} 次迭代，"
              f"输出目录 {self.outdir}")
        return self.history

    # ------------------------------------------------------------------ #
    def _assemble_velocity(self, sol_f, sol_b, movable) -> np.ndarray:
        """将一轮的边界形状导数装配为网格速度场 V。"""
        cfg = self.cfg
        s = cfg.sampling
        v_mask_grid = build_velocity_mask(
            self.ls, cfg, frozen=self.guard.frozen if self.guard else None)

        pts, nrm = boundary_samples(movable, self.ls, cfg)
        if len(pts) == 0:      # 没有可动边界（被投影/掩膜吃光）——不更新
            return np.zeros(self.ls.phi.shape)
        self._check_field_grids(sol_f, sol_b)
        # 按当前设置直接使用式 (54) 的复数 S 参数导数，不乘 |S| 的相位链式因子。

        # 采样点是亚格点，场值按坐标用 WLS 取。四个场共用一套算子：
        # fwd/bwd 必须用同一离散，否则伴随梯度会静默失真。
        # z=0 是金属底界面；若导出网格无精确 z=0，优先取最近的上侧切片。
        z_side = "above" if s.field_z_mm == 0.0 else "nearest"
        iz, z_used = sol_f.e_field.nearest_z_plane(s.field_z_mm, side=z_side)
        self._check_sample_plane(z_used)
        mask = field_node_mask(self.ls, sol_f.e_field, cfg)
        st = build_wls_stencil(
            pts, sol_f.e_field.axes()[:2], mask,
            radius_cells=s.wls_radius_cells, order=s.wls_order)
        e_f, h_f, e_b, h_b = (
            f.sample_wls(pts, iz, stencil=st).values
            for f in (sol_f.e_field, sol_f.h_field, sol_b.e_field, sol_b.h_field)
        )
        dp = shape_derivative(e_f, h_f, e_b, h_b, sol_f.pin,
                              self.omega, self.eps_r)
        # 速度回填到偏移前的边界点，确保 φ=0 邻域有延拓种子。
        side = 1.0 if s.sample_side == "outside" else -1.0
        bnd = pts - side * s.intersection_offset_mm * nrm
        # 掩膜先于归一化：被禁点不参与 max|δp|，避免拖慢全场。
        active = interp_mask_at(self.ls, bnd, v_mask_grid) > 0.5
        v = fixed_step_velocity(dp, cfg.optimizer.velocity_sign, active)
        v_seed, counts = scatter_inverse_distance(
            self.ls, bnd, v, max_dist_cells=s.scatter_max_cells)
        V, method = extend_velocity(
            self.ls, v_seed, cfg.level_set.extension_band_mm,
            method=cfg.level_set.extension_method, known=counts > 0)
        print(f"[采样] 交点 {len(pts)} 个（{s.sample_side} δz="
              f"{s.intersection_offset_mm:g} mm，WLS {s.wls_order} 阶 R="
              f"{s.wls_radius_cells} 格，回退 {st.n_fallback}），种子节点 "
              f"{int((counts > 0).sum())} 个，延拓 {method}")
        return V * v_mask_grid

    def _check_field_grids(self, sol_f, sol_b) -> None:
        """四个场必须导出在同一张 (x, y) 网格上（z 由各自的采样面吸附决定）。

        网格不一致时"按坐标取场"会取到别处的值、而且不报错——伴随法要求
        fwd/bwd 用同一离散。旧节点方案靠"逐点重合"隐式保证，现在必须显式查。
        """
        ref = sol_f.e_field
        for name, f in (("fwd H", sol_f.h_field), ("bwd E", sol_b.e_field),
                        ("bwd H", sol_b.h_field)):
            for k, ax_name in ((0, "x"), (1, "y")):
                a, b = ref.axes()[k], f.axes()[k]
                if a.shape != b.shape or not np.allclose(a, b, atol=1e-9):
                    raise ValueError(
                        f"{name} 场的 {ax_name} 网格与 fwd E 不一致（原点 "
                        f"{b[0]:g} vs {a[0]:g}、步长 {f.spacing[k]:g} vs "
                        f"{ref.spacing[k]:g}、点数 {b.size} vs {a.size}）："
                        "fwd/bwd 必须导出在同一张网格上，否则按坐标取场会静默取错值")

    def _check_sample_plane(self, z_used: float) -> None:
        """采样面吸附到导出网格面：容忍 z=0 舍入误差；金属内部则报错。

        z 的约定来自 cst_setup：z=0 是基板顶面 = 金属底面，金属占
        [0, metal_thickness]。1e-9 mm 的界面容差用于吸收浮点数误差；它比当前
        35 µm 金属厚度小很多，因此不会放行真正处于 PEC 内部的采样面。
        """
        if self._z_plane_logged:
            return
        self._z_plane_logged = True
        req, t = float(self.cfg.sampling.field_z_mm), float(self.setup.metal_thickness_mm)
        tol = min(_Z_INTERFACE_TOL_MM, t * 0.1)
        if abs(z_used) <= tol:
            if z_used != 0.0:
                print(f"[采样] z={z_used:.12g} mm 与界面 z=0 的差小于 "
                      f"容差 {tol:g} mm，按 z=0 处理")
            return
        if tol < z_used <= t:
            raise ValueError(
                f"场采样面 z={req:g} mm 吸附到导出网格面 z={z_used:g} mm，落在金属"
                f"体内（z ∈ [0, {t:g}] 是 {t * 1e3:.0f} µm 金属，内部场≈0，"
                "δp 会恒为 0）。若要采 z=0 边界，请让 CST 导出网格包含精确的 z=0 面；"
                "否则请将采样面设在金属外部的介质区域。")
        if abs(z_used - req) > 1e-9:
            print(f"[采样] 场采样面 z={req:g} mm 吸附到导出网格面 z={z_used:g} mm")

    # ------------------------------------------------------------------ #
    def _converged(self) -> bool:
        opt = self.cfg.optimizer
        w = opt.convergence_window
        if len(self.history.records) < w + 1:
            return False
        recent = [r["fom"] for r in self.history.records[-w:]]
        return max(recent) - min(recent) < opt.fom_tolerance

    def _record(self, rec: dict) -> None:
        self.history.records.append(rec)
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _snapshot(self, it: int, sol_f=None) -> None:
        """本轮快照 ``iter_NNN.png``：左 = φ 金属俯视，右 = 采样面 |E|（论文 Fig.10）。

        右栏用的 E 切片顺手落一份 ``iter_NNN/efield_fwd.npz``（~0.3 MB/轮）：
        事后重画与电场演化图都用它，不用重跑仿真。画面的两条硬约定（金属按
        φ=0 零等值线填色、视野夹在设计区 ± 0.5 mm）在 ``eaopt.plotting``，
        与 scripts/plot_results.py 共用一套，见该模块说明。
        """
        from eaopt import plotting

        e_slice, z_used = None, None
        grid = getattr(sol_f, "e_field", None)
        if grid is not None:
            z_req = self.cfg.sampling.field_z_mm
            z_side = "above" if z_req == 0.0 else "nearest"
            iz, z_used = grid.nearest_z_plane(z_req, side=z_side)
            e_slice = grid.slice_z(iz)
            artifacts.save_field_slice(self.outdir, it, "fwd", e_slice)
        plotting.snapshot_figure(self.cfg, self.ls, it=it, e_slice=e_slice,
                                 z_used=z_used,
                                 path=self.outdir / f"iter_{it:03d}.png")

    def _plot_fom(self) -> None:
        import matplotlib.pyplot as plt

        xs = [r["iteration"] for r in self.history.records]
        ys = [r["fom"] for r in self.history.records]
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.plot(xs, ys, "o-")
        ax.set_xlabel("iteration")
        ax.set_ylabel("FoM")
        ax.set_title(self.cfg.name)
        fig.savefig(self.outdir / "fom.png", dpi=120)
        plt.close(fig)

    def _plot_results(self) -> None:
        """整套过程图（论文 Fig.10/11）：S 参数曲线 + 电场演化。

        都从 ``iter_NNN/`` 的产物重建（不依赖内存状态），所以中断续跑、
        服务器事后重画（scripts/plot_results.py）走的是同一条路。
        """
        from eaopt import plotting

        plotting.sparams_figure(self.cfg, self.outdir,
                                self.outdir / "sparams_vs_iter.png")
        plotting.efield_evolution_figure(self.cfg, self.outdir,
                                         self.outdir / "efield_evolution.png")
