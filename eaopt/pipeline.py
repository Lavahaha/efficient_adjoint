"""优化闭环（论文 Fig. 4）。

每轮迭代：
  提取轮廓 → 重建几何 → 正向仿真 → FoM 评估/收敛判断
  → 后向仿真 → 边界采样 + 形状导数（式 25）→ 速度装配（固定步长
  + 符号开关）→ 栅格化 + 速度延拓 → 掩膜 → HJ 演化
  → 最小间距投影 → 周期性重初始化 → 日志/快照。

输出（cfg.output.dir）：history.jsonl（逐迭代记录）、iter_NNN.png（形状快照）、
fom.png（收敛曲线），以及 iter_NNN/ls_phi.npz（本轮 φ 快照，续跑用）；
iter_NNN/ 里的仿真产物（shape.json、s_params_*.json、meta.json）由求解器写。
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
from eaopt.adjoint.sampling import (boundary_points, boundary_samples,
                                    field_node_mask, is_fixed_contour,
                                    make_fixed_sdf, scatter_inverse_distance,
                                    scatter_to_grid)
from eaopt.config import CaseConfig
from eaopt.geometry.contour import extract_contours, smooth_resample
from eaopt.geometry.extension import extend_velocity
from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import apply_min_gap, build_velocity_mask, interp_mask_at
from eaopt.optimize.objective import make_fom
from eaopt.optimize.step import fixed_step_velocity
from eaopt.solver.cst_setup import COUPLER, CstSetup

__all__ = ["make_level_set", "movable_contours", "History", "OptimizerPipeline"]


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

    ``scheme=intersection``：返回**零等值线交点折线**（亚格点，与灵敏度
    分析的采样点是同一份离散）；``contour``：按点距重采样（旧，A/B）。
    """
    if cfg.sampling.scheme == "intersection":
        return boundary_points(ls, cfg)
    contours = extract_contours(ls.xs, ls.ys, ls.phi)
    fixed_sdf = make_fixed_sdf(ls, cfg)
    out = []
    for c in contours:
        if fixed_sdf is not None and is_fixed_contour(c, fixed_sdf):
            continue
        closed = np.allclose(c[0], c[-1])
        out.append(
            smooth_resample(c, cfg.sampling.point_spacing_mm,
                            smoothing=0.0, closed=closed)
        )
    return out


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
                    "time_s": time.perf_counter() - t0,
                }
                self._record(rec)
                self._snapshot(it)
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
                    self.ls.update(V, steps=1, cfl=opt.cfl)
                    # 投影下沉到每个子步：一轮走多步时中间态也可能穿进禁区
                    apply_min_gap(self.ls, cfg)
                if it % cfg.level_set.reinit_every == 0:
                    self.ls.reinitialize(band=2.0 * cfg.level_set.extension_band_mm, iters=40)
            else:
                self.history.stop_reason = "max_iterations"
        finally:
            # 求解器持有外部资源（CST 会话、打开的工程）——异常退出也要收尾，
            # 否则下一次运行会附接到一个状态不明的实例上。
            self.solver.close()

        self._plot_fom()
        print(f"\n优化结束（{self.history.stop_reason}）：{len(self.history.records)} 次迭代，"
              f"输出目录 {self.outdir}")
        return self.history

    # ------------------------------------------------------------------ #
    def _assemble_velocity(self, sol_f, sol_b, movable) -> np.ndarray:
        """一轮的形状导数 → 网格速度场 V（两种采样方案，见 sampling.py）。"""
        cfg = self.cfg
        s = cfg.sampling
        v_mask_grid = build_velocity_mask(self.ls, cfg)

        pts, nrm = boundary_samples(movable, self.ls, cfg)
        if len(pts) == 0:      # 没有可动边界（被投影/掩膜吃光）——不更新
            return np.zeros(self.ls.phi.shape)
        self._check_field_grids(sol_f, sol_b)
        nrm3 = np.column_stack([nrm, np.zeros(len(nrm))])

        if s.scheme == "intersection":
            # 交点方案：采样点是亚格点，场值按**坐标**用 WLS 取（导出网格
            # 原点由 CST 包围盒定，与 φ 网格不必对齐——这正是旧节点方案
            # 在服务器上做不到的事）。四个场共用一套算子：fwd/bwd 必须用
            # 同一离散，否则伴随梯度静默失真。
            iz, z_used = sol_f.e_field.nearest_z_plane(s.field_z_mm)
            self._check_sample_plane(z_used)
            mask = field_node_mask(self.ls, sol_f.e_field, cfg)
            st = build_wls_stencil(
                pts, sol_f.e_field.axes()[:2], mask,
                radius_cells=s.wls_radius_cells, order=s.wls_order)
            e_f, h_f, e_b, h_b = (
                f.sample_wls(pts, iz, stencil=st).values
                for f in (sol_f.e_field, sol_f.h_field, sol_b.e_field, sol_b.h_field)
            )
            dp = shape_derivative(e_f, h_f, e_b, h_b, nrm3, sol_f.pin,
                                  self.omega, self.eps_r)
            # 掩膜先于归一化：被禁点（角点奇异性、固定区邻域）不参与
            # max|δp|，否则它们会劫持速度尺度拖慢全场
            active = interp_mask_at(self.ls, pts, v_mask_grid) > 0.5
            v = fixed_step_velocity(dp, cfg.optimizer.velocity_sign, active)
            v_seed, counts = scatter_inverse_distance(
                self.ls, pts, v, max_dist_cells=s.scatter_max_cells)
            V, method = extend_velocity(
                self.ls, v_seed, cfg.level_set.extension_band_mm,
                method=cfg.level_set.extension_method, known=counts > 0)
            print(f"[采样] 交点 {len(pts)} 个（WLS {s.wls_order} 阶 R="
                  f"{s.wls_radius_cells} 格，回退 {st.n_fallback}），种子节点 "
                  f"{int((counts > 0).sum())} 个，延拓 {method}")
            return V * v_mask_grid

        # 不走吸附（直接按 z 插值），但采样面落进金属体内的错误同样要拦
        self._check_sample_plane(float(s.field_z_mm))
        pts3 = np.column_stack([pts, np.full(len(pts), s.field_z_mm)])
        e_f = sol_f.e_field.interp(pts3)
        h_f = sol_f.h_field.interp(pts3)
        e_b = sol_b.e_field.interp(pts3)
        h_b = sol_b.h_field.interp(pts3)
        dp = shape_derivative(e_f, h_f, e_b, h_b, nrm3, sol_f.pin,
                              self.omega, self.eps_r)
        # 栅格化必须回填到边界点（bnd，偏移前），否则落在速度延拓
        # 种子带（|φ|≤0.75·dx）之外，速度场无法播种
        side = 1.0 if s.sample_side == "outside" else -1.0
        bnd = pts - side * s.sample_offset_mm * nrm
        active = interp_mask_at(self.ls, bnd, v_mask_grid) > 0.5
        v = fixed_step_velocity(dp, cfg.optimizer.velocity_sign, active)
        v_boundary = scatter_to_grid(self.ls, bnd, v)
        V, method = extend_velocity(
            self.ls, v_boundary, cfg.level_set.extension_band_mm,
            method=cfg.level_set.extension_method)
        print(f"[采样] 轮廓点 {len(pts)} 个，延拓 {method}")
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
        """采样面吸附到导出网格面：说一声；**落进金属体内则报错**。

        z 的约定来自 cst_setup：z=0 是基板顶面 = 金属底面，金属占
        [0, metal_thickness]。导出网格的 z 面由 CST 包围盒定，请求的面吸附到
        最近的面——0.1 mm 步长下 z=0 最近的面是 +0.0015（金属内部），PEC 里
        E、H ≈ 0，δp 会全是 0：优化一轮都不动，却不报任何错。
        """
        if self._z_plane_logged:
            return
        self._z_plane_logged = True
        req, t = float(self.cfg.sampling.field_z_mm), float(self.setup.metal_thickness_mm)
        if 0.0 < z_used <= t:
            raise ValueError(
                f"场采样面 z={req:g} mm 吸附到导出网格面 z={z_used:g} mm，落在金属"
                f"体内（z ∈ [0, {t:g}] 是 {t * 1e3:.0f} µm 金属，内部场≈0，"
                "δp 会恒为 0、优化静默空转）。请把 sampling.field_z_mm 设到金属"
                "下方的介质里（如 -0.1 mm）。")
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

    def _snapshot(self, it: int) -> None:
        """本轮 φ 的金属俯视图（金属深色、空气浅色，与 layout_reference.png 同款）。

        图上必须画出**设计区**（红虚线）——早先只画了固定区（直通线）的红框，
        被当成了"设计区画错了"。三类区域的样式与图例文案在 ``eaopt.plotting``
        （与 `scripts/plot_layout.py` 共用一套，见该模块说明）。
        """
        import matplotlib.pyplot as plt

        from eaopt import plotting

        fig, ax = plt.subplots(figsize=(9, 4.6))
        ax.set_facecolor(plotting.OUTSIDE_COLOR)     # 设计区之外不是优化对象
        box_patch = self.cfg.design_region.box
        ax.add_patch(plt.Rectangle((box_patch.x[0], box_patch.y[0]),
                                   box_patch.width, box_patch.height,
                                   facecolor=plotting.AIR_COLOR, lw=0, zorder=1))
        # 金属按 **φ=0 的零等值线**填色（与 extract_contours 提取轮廓用的是
        # 同一条线）。早先用 pcolormesh 给节点上色：单元画在节点**上方**，
        # 整块金属看起来平移了半格（臂顶边 y=0 画到 +0.1），对着设计区边界
        # 看就像形状不对——而 φ 本身没问题。也别用 ``phi < 0`` 判断：界面
        # 正好压着节点时（臂顶边 y=0 就压着）φ 是 ±1e-15 的噪声，会画出一
        # 排假毛刺。等值线插值把这两件事一起解决了。
        ax.contourf(self.ls.xs, self.ls.ys, self.ls.phi.T,
                    levels=[-1e9, 0.0], colors=[plotting.METAL_COLOR], zorder=2)
        plotting.draw_regions(ax, self.cfg)
        box = self.cfg.design_region.box
        # 视野 = 设计区 ± 0.5 mm。固定区（直通线）在 x 上比设计区宽，不夹住
        # 坐标轴的话它会把视野撑开，设计区在图上只占中间一小块
        ax.set_xlim(box.x[0] - 0.5, box.x[1] + 0.5)
        ax.set_ylim(box.y[0] - 0.5, box.y[1] + 0.5)
        ax.set_aspect("equal")
        ax.set_xlabel("x (mm)")
        ax.set_ylabel("y (mm)")
        ax.set_title(f"iteration {it}", pad=24)
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3,
                  fontsize=8, frameon=False)
        fig.savefig(self.outdir / f"iter_{it:03d}.png", dpi=110,
                    bbox_inches="tight")
        plt.close(fig)

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
