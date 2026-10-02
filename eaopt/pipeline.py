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
from eaopt.adjoint.sampling import (is_fixed_contour, make_fixed_sdf,
                                    sample_boundary, scatter_to_grid)
from eaopt.config import CaseConfig
from eaopt.geometry.contour import extract_contours, smooth_resample
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
    """提取可动金属轮廓（排除固定金属，如直通线），按点距重采样。

    **模块级函数**，不是 OptimizerPipeline 的私有方法：cst_init_* 与
    cst_update 脚本要用同一份"初始轮廓"去建/改 CST 工程，各写一份迟早
    漂移（脚本里看到的形状 ≠ 迭代里演化的形状，而且不报错）。
    """
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
                pts, nrm = sample_boundary(movable, self.ls, cfg)
                pts3 = np.column_stack([pts, np.full(len(pts), cfg.sampling.field_z_mm)])
                nrm3 = np.column_stack([nrm, np.zeros(len(nrm))])
                e_f = sol_f.e_field.interp(pts3)
                h_f = sol_f.h_field.interp(pts3)
                e_b = sol_b.e_field.interp(pts3)
                h_b = sol_b.h_field.interp(pts3)
                dp = shape_derivative(e_f, h_f, e_b, h_b, nrm3, sol_f.pin, self.omega, self.eps_r)

                v_mask_grid = build_velocity_mask(self.ls, cfg)
                # 栅格化必须回填到边界点（bnd，偏移前），否则落在速度延拓
                # 种子带（|φ|≤0.75·dx）之外，速度场无法播种
                side = 1.0 if cfg.sampling.sample_side == "outside" else -1.0
                bnd = pts - side * cfg.sampling.sample_offset_mm * nrm
                # 掩膜先于归一化：被禁点（角点奇异性、固定区邻域）不参与
                # max|δp|，否则它们会劫持速度尺度拖慢全场
                active = interp_mask_at(self.ls, bnd, v_mask_grid) > 0.5
                v = fixed_step_velocity(dp, opt.velocity_sign, active)
                v_boundary = scatter_to_grid(self.ls, bnd, v)
                V = self.ls.extend_velocity(v_boundary, cfg.level_set.extension_band_mm)
                V *= v_mask_grid

                steps = max(1, round(opt.step_size / (0.4 * self.ls.dx)))
                self.ls.update(V, steps=steps, cfl=0.4)
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
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.pcolormesh(self.ls.xs, self.ls.ys, (self.ls.phi < 0).T,
                      cmap="copper", vmin=0, vmax=1)
        cs = ax.contour(self.ls.xs, self.ls.ys, self.ls.phi.T, levels=[0.0],
                        colors="k", linewidths=0.8)
        for p in self.cfg.fixed_region:
            v = np.asarray(p.vertices, dtype=float)
            ax.plot(np.append(v[:, 0], v[0, 0]), np.append(v[:, 1], v[0, 1]),
                    "r-", linewidth=1.0)
        ax.set_aspect("equal")
        ax.set_title(f"iteration {it}")
        fig.savefig(self.outdir / f"iter_{it:03d}.png", dpi=100)
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
