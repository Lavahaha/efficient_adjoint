"""CST 求解器：pipeline ↔ CST 工程的适配器（顶层直接 ``import cst.interface``）。

``SolverInterface``（``begin_iteration`` / ``build_model`` / ``solve_forward``
/ ``solve_backward`` / ``close``）在这里落到具体动作上。改形状、求解、取结果的
**流程代码与三个 ``scripts/cst_*.py`` 是同一套**（各自内联一份；重复是"不封装
官方库"的代价——换来的是没有中间层可以藏状态）。

工程生命周期：两个工程由 ``scripts/cst_init_fwd.py`` / ``cst_init_bwd.py``
建；``auto_init=True`` 时本类在第一轮就地建（``run_coupler.py`` 走这条，默认
False——静默建工程等于在用户可能开着的 GUI 里凭空冒出两个工程）。建工程 =
模板命令块进历史表，激励端口在那时写死；pipeline 全程**不碰**激励 API。

``iter_NNN/`` 产物（shape.json、s_params_*.json、meta.json、可选场）由本类按
``begin_iteration`` 给的轮次写，写入口只有 ``artifacts`` 一处。

无人值守：连上就切静默模式、每轮改形状前先清掉上一轮结果——CST 在"已有结果"
的工程上重跑仿真会弹确认框问要不要删旧结果，GUI 模式下整个脚本就卡在那行
（``CstSetup.quiet_mode`` / ``clear_results``，细节见各自的 docstring）。
"""

from __future__ import annotations

import time
from pathlib import Path

try:                        # CST 官方库只支持 Python 3.6–3.11
    import cst.interface as csti
except ImportError as e:    # pragma: no cover - 无 CST 的机器上才有意义
    raise SystemExit(
        f"import cst.interface 失败：{e}\n"
        "  官方库只支持 Python 3.6–3.11（服务器用 conda py310）；让解释器"
        "找到它：\n"
        "    pip install --no-index --find-links "
        "\"<CST>/Library/Python/repo/simple\" cst-studio-suite-link\n"
        "    或 set PYTHONPATH=<CST>/AMD64/python_cst_libraries\n"
        "  仓库根**不能**有叫 cst/ 的目录（PEP 420 遮蔽，报错不指向真因）。\n"
        "  详见 docs/server_runbook.md 第 0 节。")

from eaopt import artifacts
from eaopt.config import CaseConfig
from eaopt.geometry.contour import close_open_contours
from eaopt.solver import cst_results as R
from eaopt.solver.base import Solution, SolverInterface
from eaopt.solver.case import load_case
from eaopt.solver.cst_setup import CstSetup

__all__ = ["CstSolver"]


class CstSolver(SolverInterface):
    """一次会话、两个工程（fwd/bwd），逐轮改设计区 + 求解。"""

    def __init__(self, cfg: CaseConfig, *, setup: CstSetup | None = None,
                 model=None, auto_init: bool = False,
                 save_fields: bool | None = None,
                 attach: bool | None = None, quiet: bool | None = None,
                 clear_results: bool | None = None, log=print):
        # 算例（CstSetup + 模板模块）成套取自 cfg.name；调用方给了哪一个就
        # 用哪一个（测试要注入改动过的设置时只覆盖 setup，模板仍按算例取）。
        if setup is None or model is None:
            case_setup, case_model = load_case(cfg)
            setup = case_setup if setup is None else setup
            model = case_model if model is None else model
        setup.validate_objective(cfg.objective)     # 端口规则在 CST 侧，装配点校验
        self.cfg = cfg
        self.setup = setup
        self.M = model
        self.auto_init = auto_init
        self.save_fields = (setup.save_fields if save_fields is None
                            else bool(save_fields))
        self.attach = setup.attach_gui if attach is None else bool(attach)
        self.quiet = setup.quiet_mode if quiet is None else bool(quiet)
        self.clear_results = (setup.clear_results if clear_results is None
                              else bool(clear_results))
        self.log = log
        self.de = None
        self.projects: dict = {}
        self._it: int | None = None                 # begin_iteration 给的轮次

    # ------------------------------------------------------------------ #
    # 会话 / 工程
    # ------------------------------------------------------------------ #
    def _environment(self):
        """惰性连实例：不碰 CST 就不连（装配/导入阶段不碰外部资源）。"""
        if self.de is None:
            self.de = _connect(self.attach, False, self.log, quiet=self.quiet)
        return self.de

    def project_path(self, tag: str) -> Path:
        """该 tag 的工程路径（规则在 cst_setup，值来自配置）。"""
        return self.setup.project_path(tag, self.cfg.output.dir, self.cfg.name)

    def _open(self, tag: str):
        """打开该 tag 的工程（已开着就复用），~20 轮迭代共用同一个对象。"""
        if self.projects.get(tag) is not None:
            return self.projects[tag]
        path = self.project_path(tag)
        if not path.is_file():
            raise FileNotFoundError(
                f"{tag} 工程不存在：{path}\n"
                f"  先跑 scripts/cst_init_{tag}.py 初始化（它会建工程并跑"
                f"第一次仿真）；或让 scripts/run_coupler.py 自动建。")
        prj = _open_or_reuse(self._environment(), path, self.log)
        self.projects[tag] = prj
        return prj

    def _ensure_projects(self) -> None:
        """确认两个工程都可用；``auto_init`` 时缺哪个建哪个。"""
        for tag in artifacts.TAGS:
            if self.projects.get(tag) is not None:
                continue
            if not self.project_path(tag).is_file():
                if not self.auto_init:
                    self._open(tag)             # 报错并给出 init 脚本提示
                self.log(f"[ .. ] {tag} 工程不存在 → 自动初始化")
                self._initialize(tag)
        for tag in artifacts.TAGS:
            self._open(tag)

    def _initialize(self, tag: str):
        """建该 tag 的工程：模板命令块逐块进历史表 → 存盘（不跑仿真）。"""
        path = self.project_path(tag)
        if path.is_file():
            raise FileExistsError(
                f"{tag} 工程已经存在：{path}\n"
                "  要重建请先删掉它（连同同名文件夹），或直接用 "
                "scripts/cst_update.py 改形状。")
        prj = self._environment().new_mws()
        port = self.setup.stimulus(tag, self.cfg.objective)
        blocks = self.M.template_blocks(f"{self.cfg.name}_{tag}", port)
        self.log(f"[ .. ] 建 {tag} 工程：{len(blocks)} 个命令块，"
                 f"激励端口 = {port}")
        _apply_blocks(prj.model3d, blocks,
                      what=f"建 {tag} 工程（激励端口 {port}）", log=self.log)
        _save(prj, path, self.log)
        self.projects[tag] = prj
        return prj

    # ------------------------------------------------------------------ #
    # SolverInterface
    # ------------------------------------------------------------------ #
    def begin_iteration(self, iteration: int) -> None:
        """记下轮次：产物（iter_NNN/）才能落在正确的目录里。"""
        self._it = int(iteration)

    def build_model(self, movable: list, fixed: list | None = None) -> None:
        """把新轮廓写进两个工程的设计区（各一条历史记录）。

        ``clear_results`` 打开时，每轮**先清上一轮的结果再改形状**：CST 在
        "已有结果 + 模型要变"时会弹确认框，GUI 模式下脚本会卡在那一行。

        movable: 可动金属轮廓（世界坐标 mm，开放路径按设计区裁剪闭合）；
        fixed: 固定金属与馈线——**模板里已经有了，这里忽略**（固定几何只在
        建模板时写一次，pipeline 不重复施加）。
        """
        polys = close_open_contours(movable, self.cfg.design_region.box)
        self._ensure_projects()
        for tag in artifacts.TAGS:
            prj = self._open(tag)
            if self.clear_results:
                _clear_results(prj.model3d, what=f"{tag} 工程", log=self.log)
            _update_design_region(prj.model3d, polys, self.setup, self.M,
                                  what=f"更新 {tag} 工程设计区", log=self.log)
        if self._it is not None:
            path = artifacts.save_shape(self.cfg.output.dir, self._it, polys)
            self.log(f"[OK] 形状落盘：{path}（{len(polys)} 个多边形）")

    def solve_forward(self) -> Solution:
        """端口 from_port 激励（工程 fwd，模板里激励已写死）。"""
        return self._solve("fwd")

    def solve_backward(self) -> Solution:
        """端口 to_port 激励（工程 bwd）= 论文的伴随仿真。"""
        return self._solve("bwd")

    def close(self) -> None:
        """存盘收尾（**不关 CST 实例**：常驻会话供下一次运行复用）。

        pipeline 在 finally 里调用——没连过也要能安全收尾。
        """
        if self.de is None:
            return
        for tag in artifacts.TAGS:
            prj = self.projects.get(tag)
            if prj is None:
                continue
            try:
                _save(prj, self.project_path(tag), self.log)
            except Exception as e:              # pragma: no cover - 尽力而为
                self.log(f"[ -- ] 保存 {tag} 工程失败（忽略）：{e}")

    # ------------------------------------------------------------------ #
    def _solve(self, tag: str) -> Solution:
        """求解 → 存盘 → 读 S 参数 → 导 E/H 场并裁剪 → 落盘 iter_NNN/。

        场**每轮都导**（伴随法要用），``save_fields`` 只决定是否写进
        ``iter_NNN/``（.npz 有 MB 量级，20 轮就是上百 MB）。
        """
        prj = self._open(tag)
        path = self.project_path(tag)
        m3d = prj.model3d

        seconds = _run_solver(m3d, self.log)
        # cst.results 读的是磁盘结果：先存盘再读，否则读到的可能是上一轮
        _save(prj, path, self.log, what="求解后存盘")

        sp = R.read_s_params(path, self.setup.stimulus(tag, self.cfg.objective),
                             response_ports=tuple(self.setup.ports),
                             freq_ghz=float(self.setup.frequency_ghz),
                             log=self.log)
        sol = Solution(
            s_params=sp,
            e_field=_export(m3d, "Efield", self.cfg, self.setup, tag, self.log),
            h_field=_export(m3d, "Hfield", self.cfg, self.setup, tag, self.log),
            pin=float(self.setup.port_power_w),
            extra={"tag": tag, "solver_seconds": seconds})
        if self._it is not None:
            paths = artifacts.save_solution(self.cfg.output.dir, self._it, tag,
                                            sol, save_fields=self.save_fields)
            self.log(f"[OK] 产物落盘："
                     f"{artifacts.iteration_dir(self.cfg.output.dir, self._it)}"
                     f"（{', '.join(sorted(paths))}）")
        return sol


# --------------------------------------------------------------------------- #
# 与三个 cst_*.py 脚本同款的流程片段（各自内联一份，重复是接受的代价）
# --------------------------------------------------------------------------- #
def _connect(attach: bool, force_new: bool, log, quiet: bool = True):
    """拿到一个 ``DesignEnvironment``（语义见脚本里的同名函数）。"""
    if attach:
        try:
            de = csti.DesignEnvironment.connect_to_any()
        except Exception as e:
            raise RuntimeError(
                "attach 模式要求 CST 已经在运行（GUI 或静态实例），但 "
                f"connect_to_any() 失败：{e}\n"
                "  对策：先打开 CST Studio 2024，或把 attach 关掉（让脚本"
                "自己起静态实例）。") from e
        log("[OK] 附接到已运行的 CST 实例")
        return _with_quiet_mode(de, quiet, log)
    if not force_new:
        try:
            de = csti.DesignEnvironment.connect_to_any()
        except Exception:
            pass                                # 没有实例 → 新建，属正常路径
        else:
            log("[OK] 复用已运行的 CST 实例")
            return _with_quiet_mode(de, quiet, log)
    de = csti.DesignEnvironment.new()
    log("[OK] 新建 CST 实例（静态、无 GUI）")
    return _with_quiet_mode(de, quiet, log)


def _with_quiet_mode(de, quiet: bool, log):
    """按配置切静默模式，返回 ``de``（方便在 return 里连写）。"""
    if quiet:
        _set_quiet_mode(de, log)
    return de


def _set_quiet_mode(de, log) -> bool:
    """切静默模式：模态框（含"是否删除已有结果"的确认框）不再弹、不等人点。

    真库方法名 ``DesignEnvironment.set_quiet_mode``（py4cst 一上来也这么调）。
    **这是会话级状态**：``--attach`` 下用户自己的 GUI 会话也会被静默，要恢复
    就重开 CST（或按 docs/server_runbook.md 常见问题一节处理）。
    """
    fn = getattr(de, "set_quiet_mode", None)
    if not callable(fn):
        log("[ -- ] 该 CST 版本没有 set_quiet_mode()：跳过静默模式")
        return False
    try:
        fn(True)
    except Exception as e:                      # pragma: no cover - 版本差异
        log(f"[ -- ] 切静默模式失败（{e}）：继续，但模态框可能仍需手工确认")
        return False
    log("[OK] CST 静默模式已开（模态框不再弹）")
    return True


def _clear_results(m3d, *, what: str, log) -> bool:
    """清掉工程里已有的结果——求解时没有旧结果，确认框就无从弹起。

    CST 在**已有结果的工程**上重跑仿真时会弹模态框问"要不要删掉上次的
    结果"，GUI 模式下脚本会一直卡在那行。挡在源头：每轮改形状之前先清。
    走**控制宏**（``_execute_vba_code``，不进历史表），轮的历史记录条数不变；
    该版本没有控制宏通道时退回历史表并告警。

    命令名 ``DeleteResults`` 从 py4cst 来（照官方库自动生成的
    ``Project.delete_results``）；若 CST 侧不认这个命令，会在这里告警而不是
    把整轮跑挂——按 docs/server_runbook.md 用 GUI 录制正确 VBA 再来改。
    """
    run = getattr(m3d, "_execute_vba_code", None)
    if callable(run):
        try:
            run("Sub Main\nDeleteResults\nEnd Sub")
        except Exception as e:
            log(f"[ -- ] {what}：清结果失败（{e}）；求解时 CST 可能弹确认框")
            return False
        log(f"[OK] {what}：已清掉已有结果（DeleteResults，控制宏，不进历史表）")
        return True
    try:                                        # 没有控制宏通道 → 退回历史表
        m3d.add_to_history("delete results", "DeleteResults")
    except Exception as e:
        log(f"[ -- ] {what}：清结果失败（{e}）；求解时 CST 可能弹确认框")
        return False
    log(f"[OK] {what}：已清掉已有结果（DeleteResults，历史表）")
    return True


def _open_or_reuse(de, path, log):
    """打开工程；**已经开着就复用**（同一个 .cst 不能打开两次）。"""
    path = Path(path)
    want = _norm(path)
    for prj in de.get_open_projects():
        got = prj.filename()
        if got and _norm(got) == want:
            log(f"[OK] 复用已打开的工程：{path}")
            return prj
    prj = de.open_project(str(path))
    log(f"[OK] 打开工程：{path}")
    return prj


def _norm(p) -> str:
    try:
        return str(Path(p).resolve()).lower()
    except (OSError, ValueError):               # pragma: no cover - 兜底
        return str(p).lower()


def _save(prj, path, log, what: str = "保存工程") -> None:
    """保存工程（``allow_overwrite`` 两段式，见 scripts/cst_init_fwd.py）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prj.save(str(path), allow_overwrite=True)
    except TypeError:                           # 该版本没有这个形参
        prj.save(str(path))
    log(f"[OK] {what}：{path}")


def _apply_blocks(m3d, blocks, what: str, log) -> int:
    """逐块 ``add_to_history``（上下文补充与返回 False 的处理见脚本）。"""
    blocks = list(blocks)
    for i, (header, cmd) in enumerate(blocks, 1):
        try:
            ret = m3d.add_to_history(header, cmd)
        except Exception as e:
            raise RuntimeError(
                f"{what}：第 {i}/{len(blocks)} 块 {header!r} 失败。\n"
                f"CST 原始诊断：{e}\n--- 该块命令 ---\n{cmd}") from e
        if ret is False:
            raise RuntimeError(
                f"{what}：第 {i}/{len(blocks)} 块 {header!r} 返回 False"
                f"（CST 没有报错，但命令也没生效）。\n--- 该块命令 ---\n{cmd}")
        log(f"    [{i:2d}/{len(blocks)}] {header}")
    log(f"[OK] {what}：{len(blocks)} 块已写入历史表")
    return len(blocks)


def _update_design_region(m3d, polys, setup, model, *, what: str, log) -> None:
    """一轮形状更新 = **一条**历史记录：删整个组件 + 按新轮廓重建。

    ``model`` = 该算例的模板模块（组件名与 Extrude 命令由它给，见 case.py）。
    """
    polys = list(polys)
    cmd = model.design_region_update(polys, setup.metal_thickness_mm,
                                     component=model.DESIGN_COMPONENT,
                                     material=setup.metal_material.upper())
    log(f"[ .. ] {what}：{len(polys)} 个多边形 / {sum(len(p) for p in polys)} 个点")
    _apply_blocks(m3d, [("design region", cmd)], what, log)


def _run_solver(m3d, log) -> float:
    """``run_solver()`` 是同步的（返回即求解完成），CST 2024 实测**无参**。"""
    if _solver_running(m3d):
        raise RuntimeError(
            "求解器已经在运行：可能上一轮还没结束，或上次崩溃留下了跑飞的"
            "求解进程。先在 CST 里确认（必要时 abort），再重试。")
    log("[ .. ] 求解中（同步等待，可能要几分钟）…")
    t0 = time.perf_counter()
    try:
        m3d.run_solver()
    except Exception as e:
        raise RuntimeError(f"求解失败：{e}") from e
    dt = time.perf_counter() - t0
    log(f"[OK] 求解完成，耗时 {dt:.1f} s")
    return dt


def _solver_running(m3d) -> bool:
    fn = getattr(m3d, "is_solver_running", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception:                           # pragma: no cover - 版本差异
        return False


def _export(m3d, field_type: str, cfg: CaseConfig, setup, tag: str, log):
    """导出该场并裁到设计区 ± field_margin_mm。"""
    out = Path(cfg.output.dir) / "cst_work" / f"{tag}_{field_type}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    step_xy, step_z = setup.resolve_export_steps(cfg.field_export_step_mm)
    return R.export_field_cropped(
        m3d, field_type, float(setup.frequency_ghz), step_xy, out,
        cfg.design_region.box, float(cfg.design_region.field_margin_mm),
        step_z_mm=step_z, log=log)
