"""三个 CST 脚本共用的编排层（init fwd / init bwd / update）。

分工：``cst_session`` 管"怎么跟 CST 说话"（起实例、开工程、写历史表、
求解），``cst_results`` 管"怎么把结果取回来"（S 参数、场导出+裁剪），
本模块管**算例语义**：拿 ``CaseConfig`` 把上面两层串成一条完整的动作。

为什么要有这一层：三个脚本 + pipeline 里的 ``CstSolver`` 做的是同一件事
的四个入口。如果各写一份，"先存盘再读结果""场要裁到设计区""激励端口从
配置来"这些约束就会各处漂移——而它们错了都不报错，只出错的数据。

统一动作
--------
``initialize``
    建工程：模板命令块（``template_builder.template_blocks``）逐块写进
    历史表 → 存盘。激励端口在建工程时就写死（fwd = ``objective.from_port``，
    bwd = ``objective.to_port``），此后**永不触碰激励 API**。
``solve``
    求解 → 存盘 → 读 S 参数 → 导 E/H 场 → 裁到设计区 ± 余量 → Solution。
    存盘必须排在读结果前面：``cst.results`` 读的是磁盘上的结果文件。
``update_design``
    两个工程各发**一条**历史记录：删掉整个 design_region 组件 + 按新轮廓
    重建。因为走 ``add_to_history``，工程重放历史得到的就是当前形状
    （直接调 VBA 对象模型做不到这一点，见 cst_session 模块头）。

CLI 入口（``init_main`` / ``update_main``）也在这里——两个 init 脚本就是
一行调用，逻辑一份，不存在"哪个脚本先跑过什么"的隐形状态。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from eaopt import artifacts
from eaopt.config import CaseConfig
from eaopt.optimize.objective import make_fom
from eaopt.solver import cst_results as R
from eaopt.solver import cst_session as S
from eaopt.solver import template_builder as TB
from eaopt.solver import vba as V
from eaopt.solver.base import Solution

__all__ = ["CstSession", "initialize", "update_design", "solve",
           "init_main", "update_main"]


class CstSession:
    """两个 CST 工程（fwd / bwd）的常驻会话。

    一次连接、两个工程都开着，~20 轮迭代在同一会话里改模型+求解；
    每轮都存盘，所以实例崩了也能从磁盘状态接上（工程的**唯一**事实来源
    是配置里的 ``project_*.cst`` 与 ``iter_NNN/``，不是活进程）。
    """

    def __init__(self, cfg: CaseConfig, *, attach: bool | None = None,
                 force_new: bool = False, lib_dir: str | None = None,
                 log=print):
        self.cfg = cfg
        self.attach = cfg.solver.attach_gui if attach is None else bool(attach)
        self.force_new = force_new
        self.lib_dir = lib_dir or cfg.solver.cst_python_libs
        self.log = log
        self.de = None
        self.projects: dict = {}

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def connect(self):
        """连上（或新建）CST 实例；不打开工程。"""
        if self.de is None:
            self.de = S.ensure_environment(attach=self.attach,
                                           force_new=self.force_new,
                                           lib_dir=self.lib_dir, log=self.log)
        return self.de

    def open_project(self, tag: str):
        """打开该 tag 的工程（已开着则复用）。"""
        path = self.cfg.project_path(tag)
        if not path.is_file():
            raise FileNotFoundError(
                f"{tag} 工程不存在：{path}\n"
                f"  先跑 scripts/cst_init_{tag}.py 初始化（它会建工程并跑"
                f"第一次仿真）。")
        prj = S.open_or_reuse_project(self.connect(), path, log=self.log)
        self.projects[tag] = prj
        return prj

    def open_all(self, tags=artifacts.TAGS):
        for tag in tags:
            self.open_project(tag)
        return self.projects

    def initialize(self, tag: str):
        """建该 tag 的工程：模板命令块逐块进历史表 → 存盘。"""
        path = self.cfg.project_path(tag)
        if path.is_file():
            raise FileExistsError(
                f"{tag} 工程已经存在：{path}\n"
                f"  要重建请先删掉它（连同同名文件夹），或直接用 "
                f"scripts/cst_update.py 改形状。")
        prj = S.new_project(self.connect(), log=self.log)
        port = self.cfg.stimulus_port(tag)
        blocks = TB.template_blocks(f"{self.cfg.name}_{tag}", port)
        self.log(f"[ .. ] 建 {tag} 工程：{len(blocks)} 个命令块，"
                 f"激励端口 = {port}")
        S.apply_blocks(prj.model3d, blocks,
                       what=f"建 {tag} 工程（激励端口 {port}）", log=self.log)
        S.save_project(prj, path, log=self.log)
        self.projects[tag] = prj
        return prj

    def save(self, tags=artifacts.TAGS):
        for tag in tags:
            prj = self.projects.get(tag)
            if prj is not None:
                S.save_project(prj, self.cfg.project_path(tag), log=self.log)

    def close(self, *, close_environment: bool = False):
        """存盘（尽力而为）。默认**不关实例**——常驻会话的要点就是复用，
        关掉会让下一次调用重新起一个（GUI 许可下还要重新等启动）。"""
        try:
            self.save()
        except Exception as e:                      # pragma: no cover - 兜底
            self.log(f"[ -- ] 保存工程失败（忽略）：{e}")
        if close_environment:
            S.close_environment(self.de, log=self.log)
            self.de = None

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def update_design(self, polys, tags=artifacts.TAGS):
        """把新形状写进两个工程（各一条历史记录）。"""
        polys = [np.asarray(p, dtype=float) for p in polys]
        for tag in tags:
            prj = self.projects.get(tag) or self.open_project(tag)
            S.update_design_region(
                prj.model3d, polys,
                thickness_mm=self.cfg.metal.thickness_mm,
                material=self.cfg.metal.material.upper(),
                component=V.DESIGN_COMPONENT,
                what=f"更新 {tag} 工程设计区", log=self.log)

    def solve(self, tag: str, *, export_fields: bool = True) -> Solution:
        """求解 + 取结果，返回 Solution（场已裁到设计区 ± 余量）。"""
        prj = self.projects.get(tag) or self.open_project(tag)
        m3d = prj.model3d
        path = self.cfg.project_path(tag)

        seconds = S.run_solver(m3d, timeout=self.cfg.solver.solver_timeout_s,
                               log=self.log)
        # cst.results 读的是磁盘结果：先存盘再读，否则读到的可能是上一轮
        S.save_project(prj, path, log=self.log)

        sp = R.read_s_params(path, self.cfg.stimulus_port(tag),
                             response_ports=tuple(p.id for p in self.cfg.ports),
                             freq_ghz=float(self.cfg.frequency),
                             allow_interactive=True, model3d=m3d,
                             lib_dir=self.lib_dir, log=self.log)
        e_field = h_field = None
        if export_fields:
            e_field = self._export(m3d, "Efield", tag, path)
            h_field = self._export(m3d, "Hfield", tag, path)
        return Solution(s_params=sp, e_field=e_field, h_field=h_field,
                        pin=float(self.cfg.solver.port_power_w),
                        extra={"tag": tag, "solver_seconds": seconds})

    def _export(self, m3d, field_type: str, tag: str, project_path: Path):
        """导出该场并裁到设计区 ± field_margin_mm。"""
        out = Path(self.cfg.output.dir) / "cst_work" / f"{tag}_{field_type}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        grid = R.export_field_grid(m3d, field_type, float(self.cfg.frequency),
                                   self.cfg.export_step_mm(), out, log=self.log)
        box = self.cfg.design_region.box
        margin = float(self.cfg.design_region.field_margin_mm)
        cut = R.crop_grid(grid, box, margin)
        self.log(f"    [OK] {field_type} 裁剪：{grid.data.shape} → "
                 f"{cut.data.shape}（设计区 ±{margin:g} mm）")
        return cut


# --------------------------------------------------------------------------- #
# 高层动作（三个脚本 + CstSolver 都走这里）
# --------------------------------------------------------------------------- #
def initialize(cfg: CaseConfig, tag: str, *, attach=None, force_new=False,
               lib_dir=None, save_fields=None, log=print) -> dict:
    """建该 tag 的工程并跑第一次仿真，把结果写到 ``iter_000/``。

    返回一份报告字典（脚本负责排版打印）。
    """
    sess = CstSession(cfg, attach=attach, force_new=force_new,
                      lib_dir=lib_dir, log=log)
    try:
        sess.initialize(tag)
        sol = sess.solve(tag)
        _write_iteration(cfg, 0, tag, sol, save_fields=save_fields, log=log)
        return {"tag": tag, "project": str(cfg.project_path(tag)),
                "solution": sol, "iteration": 0}
    finally:
        sess.close()


def update_design(cfg: CaseConfig, polys, *, iteration=None, attach=None,
                  lib_dir=None, save_fields=None, log=print) -> dict:
    """把形状写进两个工程 → 两个都求解 → 结果写到 ``iter_NNN/``。"""
    n = _next_iteration(cfg, iteration)
    sess = CstSession(cfg, attach=attach, lib_dir=lib_dir, log=log)
    try:
        sess.open_all()
        sess.update_design(polys)
        # 形状记录先落盘：``shape.json`` 必须是**真正施加到工程上的那个形状**。
        # 让 _write_iteration 去算"当前水准集轮廓"会写进另一个形状——
        # 用 --shape 手工给形状时，水准集还停在初始值，两者根本不是一回事。
        _write_shape_once(cfg, n, polys)
        out = {}
        for tag in artifacts.TAGS:
            sol = sess.solve(tag)
            _write_iteration(cfg, n, tag, sol, save_fields=save_fields, log=log)
            out[tag] = sol
        return {"iteration": n, "solutions": out}
    finally:
        sess.close()


def solve(cfg: CaseConfig, tag: str, *, iteration=0, attach=None,
          lib_dir=None, save_fields=None, log=print) -> dict:
    """只求解一个已存在的工程（不建、不改形状）——补跑/排查用。"""
    sess = CstSession(cfg, attach=attach, lib_dir=lib_dir, log=log)
    try:
        sess.open_project(tag)
        sol = sess.solve(tag)
        _write_iteration(cfg, iteration, tag, sol, save_fields=save_fields,
                         log=log)
        return {"tag": tag, "iteration": iteration, "solution": sol}
    finally:
        sess.close()


def _write_iteration(cfg: CaseConfig, n: int, tag: str, sol, *,
                     save_fields=None, log=print) -> dict:
    """落盘：先 shape（首次）→ S 参数/场 → meta（由 save_solution 收尾）。"""
    a = artifacts
    _write_shape_once(cfg, n)
    save_f = cfg.output.save_fields if save_fields is None else bool(save_fields)
    paths = a.save_solution(cfg.output.dir, n, tag, sol, save_fields=save_f)
    log(f"[OK] 产物落盘：{a.iteration_dir(cfg.output.dir, n)}"
        f"（{', '.join(sorted(paths))}）")
    return paths


def _write_shape_once(cfg: CaseConfig, n: int, polys=None):
    """写该轮的 ``shape.json``（已存在则不动）。

    ``polys`` 给了就记它——那是**真正施加到工程上的形状**（``--shape`` 手工
    给形状时必须走这条：此时水准集还停在初始值，两者根本不是一回事，记错了
    事后完全没法复盘"第 N 轮的模型长什么样"）。没给才退回"当前水准集轮廓"。
    """
    a = artifacts
    path = a.iteration_dir(cfg.output.dir, n) / "shape.json"
    if path.is_file():
        return path
    return a.save_shape(cfg.output.dir, n,
                        _current_contours(cfg) if polys is None else polys)


def _current_contours(cfg: CaseConfig):
    """当前可动轮廓 = 水准集按配置初始化的零等值线（首轮 = initial_metal）。

    与 pipeline 用的是**同一个函数**（pipeline.movable_contours），不各写
    一份——否则"脚本里看到的形状"和"迭代里演化的形状"会悄悄不一致。
    """
    from eaopt.pipeline import make_level_set, movable_contours

    return movable_contours(make_level_set(cfg), cfg)


def _next_iteration(cfg: CaseConfig, iteration) -> int:
    if iteration is not None:
        return int(iteration)
    last = artifacts.latest_iteration(cfg.output.dir)
    return 0 if last is None else last + 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("config", help="算例 YAML（如 configs/coupler.yaml）")
    p.add_argument("--attach", action="store_true",
                   help="只附接已运行的 CST 实例（GUI 调试用；连不上就报错）")
    p.add_argument("--new", dest="force_new", action="store_true",
                   help="强制新建一个无 GUI 的静态实例")
    p.add_argument("--lib-dir", default=None,
                   help="CST 的 python_cst_libraries 目录（缺省自动探测）")
    p.add_argument("--save-fields", dest="save_fields", action="store_true",
                   default=None, help="把 E/H 场也写进 iter_NNN/（.npz）")
    p.add_argument("--no-save-fields", dest="save_fields", action="store_false",
                   help="不写场文件（覆盖配置里的 save_fields）")
    p.add_argument("--log", default=None,
                   help="把输出同时写到这个文件（UTF-8）")
    return p


def init_main(tag: str, argv=None) -> int:
    """``scripts/cst_init_{fwd,bwd}.py`` 的入口。"""
    p = _base_parser(f"初始化 {tag} 仿真工程并跑第一次仿真")
    args = p.parse_args(argv)
    log = _make_log(args.log)
    cfg = CaseConfig.from_yaml(args.config)
    _banner(log, f"初始化 {tag} 工程", cfg)
    try:
        rep = initialize(cfg, tag, attach=args.attach or None,
                         force_new=args.force_new, lib_dir=args.lib_dir,
                         save_fields=args.save_fields, log=log)
    except Exception as e:
        log(f"[FAIL] {tag} 初始化失败：{e}")
        return 1
    _report(log, cfg, rep["solution"], tag, 0, rep["project"])
    log("[OK] 完成。接着可以跑 scripts/cst_init_bwd.py（或 fwd）+ "
        "scripts/cst_update.py 做形状迭代。")
    return 0


def update_main(argv=None) -> int:
    """``scripts/cst_update.py`` 的入口。"""
    p = _base_parser("更新两个 CST 工程的优化区形状并求解")
    p.add_argument("--shape", default=None,
                   help="形状 JSON（iter_NNN/shape.json 或 [[x,y],...] / "
                        "[[[x,y],...],...]）；与 --from-ls 二选一")
    p.add_argument("--from-ls", action="store_true",
                   help="形状取自最近一轮的 φ 快照（iter_NNN/ls_phi.npz）")
    p.add_argument("--iteration", type=int, default=None,
                   help="写到哪一轮目录（缺省 = 已有最新轮次 + 1）")
    args = p.parse_args(argv)
    log = _make_log(args.log)
    cfg = CaseConfig.from_yaml(args.config)
    _banner(log, "更新形状", cfg)

    try:
        polys = _resolve_shape(cfg, args, log=log)
        log(f"[ .. ] 新形状：{len(polys)} 个多边形 / "
            f"{sum(len(p) for p in polys)} 个点")
        rep = update_design(cfg, polys, iteration=args.iteration,
                            attach=args.attach or None, lib_dir=args.lib_dir,
                            save_fields=args.save_fields, log=log)
    except Exception as e:
        log(f"[FAIL] 形状更新失败：{e}")
        return 1
    n = rep["iteration"]
    for tag in artifacts.TAGS:
        _report(log, cfg, rep["solutions"][tag], tag, n,
                str(cfg.project_path(tag)))
    return 0


def _resolve_shape(cfg: CaseConfig, args, *, log=print):
    if bool(args.shape) == bool(args.from_ls):
        raise SystemExit("必须且只能给 --shape 或 --from-ls 之一"
                         "（--shape 给文件，--from-ls 用最近一轮的 φ 快照）")
    if args.shape:
        polys = artifacts.load_shape(args.shape)
        log(f"[OK] 形状来自文件：{args.shape}")
        return polys
    last = artifacts.latest_iteration(cfg.output.dir)
    if last is None:
        raise SystemExit("还没有任何轮次，--from-ls 无从取起；先用 --shape 给"
                         "一个形状文件")
    npz = artifacts.iteration_dir(cfg.output.dir, last) / "ls_phi.npz"
    if not npz.is_file():
        raise SystemExit(f"缺少 φ 快照 {npz}（只有 pipeline 跑过的轮次才有）")
    return _contours_from_phi(cfg, artifacts.load_ls_phi(npz), log=log)


def _contours_from_phi(cfg: CaseConfig, snap: dict, *, log=print):
    """由 φ 快照提取可动轮廓（与 pipeline 同一套提取+重采样）。"""
    from eaopt.geometry.levelset import LevelSet2D
    from eaopt.pipeline import movable_contours

    ls = LevelSet2D(cfg.design_region.box, cfg.design_region.grid_step_mm)
    if snap["phi"].shape != (ls.nx, ls.ny):
        raise SystemExit(
            f"φ 快照尺寸 {snap['phi'].shape} 与当前配置的网格 "
            f"{(ls.nx, ls.ny)} 不符——配置改过了？删掉旧的 iter_*/ 再跑。")
    ls.phi = snap["phi"]
    log(f"[OK] 形状来自 φ 快照（{ls.nx}×{ls.ny}）")
    return movable_contours(ls, cfg)


def _make_log(path):
    """日志函数：stdout + （可选）UTF-8 文件。"""
    if not path:
        return print
    fh = open(path, "w", encoding="utf-8")

    def log(msg=""):
        print(msg)
        fh.write(str(msg) + "\n")
        fh.flush()

    return log


def _banner(log, title: str, cfg: CaseConfig) -> None:
    log("=" * 72)
    log(f"{title} —— 算例 {cfg.name}")
    log("=" * 72)
    log(f"求解器      : {cfg.solver.type}")
    log(f"工程 fwd    : {cfg.project_path('fwd')}")
    log(f"工程 bwd    : {cfg.project_path('bwd')}")
    log(f"激励端口    : fwd={cfg.stimulus_port('fwd')} "
        f"bwd={cfg.stimulus_port('bwd')}  "
        f"（建工程时写死，之后不再触碰激励 API）")
    log(f"频点        : {cfg.frequency} GHz")
    log(f"场导出步长  : {cfg.export_step_mm():g} mm")
    log("-" * 72)


def _report(log, cfg: CaseConfig, sol, tag: str, n: int, project: str) -> None:
    """统一的结果报告块（用户直接贴回来就能定位问题）。"""
    log("-" * 72)
    log(f"[OK] {tag} @ iter {n:03d}  工程 {project}")
    for (i, j), v in sorted(sol.s_params.items()):
        log(f"    |S{i},{j}| = {abs(v):8.5f}  ({_db(v):7.2f} dB)")
    log(f"    入射功率 {sol.pin:g} W，求解耗时 "
        f"{sol.extra.get('solver_seconds', float('nan')):.1f} s")
    if sol.e_field is not None:
        log(f"    E 场 {sol.e_field.data.shape} @ origin "
            f"{tuple(round(x, 4) for x in sol.e_field.origin)} mm")
    if sol.h_field is not None:
        log(f"    H 场 {sol.h_field.data.shape}")
    try:
        log(f"    FoM = {make_fom(cfg)(sol):.6f}")
    except Exception as e:                          # pragma: no cover - 兜底
        log(f"    [ -- ] FoM 计算失败（不影响仿真结果）：{e}")
    log("-" * 72)


def _db(v: complex) -> float:
    m = abs(v)
    return 20.0 * np.log10(m) if m > 0 else float("-inf")
