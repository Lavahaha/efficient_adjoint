#!/usr/bin/env python
"""形状更新：改两个 CST 工程的优化区 → 两个都求解 → 提取 S 参数与场。

    # 用一个手写的形状文件（iter_NNN/shape.json 或 [[x,y],...] / [[[x,y],...],...]）
    python scripts/cst_update.py configs/coupler.yaml --shape my_shape.json

    # 用最近一轮 pipeline 留下的 φ 快照（iter_NNN/ls_phi.npz）重算轮廓
    python scripts/cst_update.py configs/coupler.yaml --from-ls

    # 指定写到哪一轮（缺省 = 已有最新轮次 + 1）；附接 GUI 看模型变化
    python scripts/cst_update.py configs/coupler.yaml --shape s.json --iteration 7 --attach

一次调用做的事（直接调 CST 官方库，中间没有封装层）：
  1. 连实例（默认复用已运行的；``--attach`` 只附接，连不上报错）；
  2. 两个工程**复用或打开**（同一个 ``.cst`` 绝不打开两次）；
  3. 每个工程写**恰好一条**历史记录：``Component.Delete "design_region"``
     + 按新轮廓逐个 Extrude——走 ``add_to_history``，所以工程重放历史得到
     的就是当前形状；
  4. 两个工程都改成功后，才把**真正施加的**形状写进
     ``iter_NNN/shape.json``（覆盖写）；
  5. 每个工程：求解 → 存盘 → 读 S 参数（cst.results）→ 导 E/H 场并裁到
     设计区 ± field_margin_mm → 写 ``iter_NNN/``。
"""

from __future__ import annotations

import sys
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
from eaopt.cli import base_parser, banner, make_log, report_solution, safe_console
from eaopt.geometry.contour import close_open_contours
from eaopt.config import CaseConfig
from eaopt.solver import cst_model as M
from eaopt.solver import cst_results as R
from eaopt.solver.base import Solution
from eaopt.solver.cst_setup import COUPLER


def main(argv=None) -> int:
    safe_console()
    p = base_parser("更新两个 CST 工程的优化区形状并求解")
    p.add_argument("--shape", default=None,
                   help="形状 JSON（iter_NNN/shape.json 或 [[x,y],...] / "
                        "[[[x,y],...],...]）；与 --from-ls 二选一")
    p.add_argument("--from-ls", action="store_true",
                   help="形状取自最近一轮的 φ 快照（iter_NNN/ls_phi.npz）")
    p.add_argument("--iteration", type=int, default=None,
                   help="写到哪一轮目录（缺省 = 已有最新轮次 + 1）")
    args = p.parse_args(argv)
    log = make_log(args.log)
    cfg = CaseConfig.from_yaml(args.config)
    banner(log, "更新形状", cfg)

    setup = COUPLER
    try:
        polys = _resolve_shape(cfg, args, log=log)
        log(f"[ .. ] 新形状：{len(polys)} 个多边形 / "
            f"{sum(len(p) for p in polys)} 个点")
        n = _next_iteration(cfg, args.iteration)
        de = _connect(args.attach, args.force_new, log)
        projects = {tag: _open_or_reuse(
            de, setup.project_path(tag, cfg.output.dir, cfg.name), log)
            for tag in artifacts.TAGS}
        # 两个工程都改成功后才落盘形状——半施加状态不留形状记录
        for tag, prj in projects.items():
            if setup.clear_results:
                # 先清旧结果：CST 在"已有结果 + 模型要变"时会弹确认框卡住脚本
                _clear_results(prj.model3d, what=f"{tag} 工程", log=log)
            _update_design_region(prj.model3d, polys, setup,
                                  what=f"更新 {tag} 工程设计区", log=log)
        shape_path = artifacts.save_shape(cfg.output.dir, n, polys)
        log(f"[OK] 形状落盘：{shape_path}（{len(polys)} 个多边形）")

        sols = {tag: _solve_and_record(
            cfg, setup, prj, setup.project_path(tag, cfg.output.dir, cfg.name),
            tag=tag, iteration=n, save_fields=_save_fields(args, setup), log=log)
            for tag, prj in projects.items()}
    except Exception as e:
        log(f"[FAIL] 形状更新失败：{e}")
        return 1
    for tag in artifacts.TAGS:
        report_solution(log, cfg, sols[tag], tag, n,
                        setup.project_path(tag, cfg.output.dir, cfg.name))
    return 0


# --------------------------------------------------------------------------- #
# 会话 / 工程
# --------------------------------------------------------------------------- #
def _connect(attach: bool, force_new: bool, log):
    """拿到一个 ``DesignEnvironment``（语义与 init 脚本一致）。"""
    if attach:
        try:
            de = csti.DesignEnvironment.connect_to_any()
        except Exception as e:
            raise RuntimeError(
                "attach 模式要求 CST 已经在运行（GUI 或静态实例），但 "
                f"connect_to_any() 失败：{e}\n"
                "  对策：先打开 CST Studio 2024，或去掉 --attach（让脚本"
                "自己起静态实例）。") from e
        log("[OK] 附接到已运行的 CST 实例")
        return _with_quiet_mode(de, log)
    if not force_new:
        try:
            de = csti.DesignEnvironment.connect_to_any()
        except Exception:
            pass                                # 没有实例 → 新建，属正常路径
        else:
            log("[OK] 复用已运行的 CST 实例")
            return _with_quiet_mode(de, log)
    de = csti.DesignEnvironment.new()
    log("[OK] 新建 CST 实例（静态、无 GUI）")
    return _with_quiet_mode(de, log)


def _with_quiet_mode(de, log):
    """切静默模式（``CstSetup.quiet_mode``），返回 ``de`` 方便连写。"""
    if COUPLER.quiet_mode:
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
    结果"，GUI 模式下脚本会一直卡在那行。挡在源头：改形状之前先清。
    走**控制宏**（``_execute_vba_code``，不进历史表）；该版本没有控制宏通道时
    退回历史表并告警。命令名 ``DeleteResults`` 从 py4cst 来（照官方库自动生成
    的 ``Project.delete_results``）。
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
    """打开工程；**已经开着就复用**。

    同一个 ``.cst`` 打开两次会得到两个工程对象，两边各改各的、各自存盘，
    后存的覆盖先存的——所以先拿 ``prj.filename()`` 与已打开的列表比对。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"工程不存在：{path}\n"
            "  先跑 scripts/cst_init_fwd.py 与 scripts/cst_init_bwd.py 建工程。")
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


def _save(prj, path: Path, log, what: str = "保存工程") -> None:
    """保存工程（``allow_overwrite`` 两段式，见 init 脚本里的说明）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prj.save(str(path), allow_overwrite=True)
    except TypeError:                           # 该版本没有这个形参
        prj.save(str(path))
    log(f"[OK] {what}：{path}")


# --------------------------------------------------------------------------- #
# 改形状（每工程每轮**一条**历史记录）
# --------------------------------------------------------------------------- #
def _apply_blocks(m3d, blocks, what: str, log) -> int:
    """逐块 ``add_to_history``（上下文补充与返回 False 的处理同 init 脚本）。"""
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


def _update_design_region(m3d, polys, setup, *, what: str, log) -> None:
    """一轮形状更新 = **一条**历史记录：删整个组件 + 按新轮廓重建。

    整段作为一条进历史表（既改当前模型、又能被重放），每轮历史表只长一条。
    """
    polys = list(polys)
    cmd = M.design_region_update(polys, setup.metal_thickness_mm,
                                 component=M.DESIGN_COMPONENT,
                                 material=setup.metal_material.upper())
    n_pts = sum(len(p) for p in polys)
    log(f"[ .. ] {what}：{len(polys)} 个多边形 / {n_pts} 个点，"
        f"历史记录 {len(cmd)} 字符")
    _apply_blocks(m3d, [("design region", cmd)], what, log)


# --------------------------------------------------------------------------- #
# 求解 / 取结果
# --------------------------------------------------------------------------- #
def _solve_and_record(cfg: CaseConfig, setup, prj, path, *, tag: str,
                      iteration: int, save_fields: bool, log) -> Solution:
    """求解 → 存盘 → 读 S 参数 → 导 E/H 场 → 写 ``iter_NNN/``。

    **存盘必须排在读结果前面**：``cst.results`` 读的是磁盘上的结果文件。
    """
    m3d = prj.model3d
    seconds = _run_solver(m3d, log)
    _save(prj, path, log, what="求解后存盘")

    sp = R.read_s_params(path, setup.stimulus(tag, cfg.objective),
                         response_ports=tuple(setup.ports),
                         freq_ghz=float(setup.frequency_ghz), log=log)
    e_field = _export(m3d, "Efield", cfg, setup, tag, log)
    h_field = _export(m3d, "Hfield", cfg, setup, tag, log)
    sol = Solution(s_params=sp, e_field=e_field, h_field=h_field,
                   pin=float(setup.port_power_w),
                   extra={"tag": tag, "solver_seconds": seconds})
    paths = artifacts.save_solution(cfg.output.dir, int(iteration), tag, sol,
                                    save_fields=bool(save_fields))
    log(f"[OK] 产物落盘：{artifacts.iteration_dir(cfg.output.dir, int(iteration))}"
        f"（{', '.join(sorted(paths))}）")
    return sol


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
    return R.export_field_cropped(
        m3d, field_type, float(setup.frequency_ghz),
        setup.resolve_export_step(cfg.field_export_step_mm), out,
        cfg.design_region.box, float(cfg.design_region.field_margin_mm), log=log)


def _save_fields(args, setup) -> bool:
    """``--save-fields`` / ``--no-save-fields`` 没给时跟随 CstSetup。"""
    return setup.save_fields if args.save_fields is None else bool(args.save_fields)


# --------------------------------------------------------------------------- #
# 形状来源 / 轮次
# --------------------------------------------------------------------------- #
def _next_iteration(cfg: CaseConfig, iteration) -> int:
    if iteration is not None:
        return int(iteration)
    last = artifacts.latest_iteration(cfg.output.dir)
    return 0 if last is None else last + 1


def _resolve_shape(cfg: CaseConfig, args, *, log=print):
    """``--shape`` 与 ``--from-ls`` 必须且只能给一个。

    返回**可直接施加**的多边形：开放路径（金属穿出设计区两端）先按设计区
    闭合——Extrude 需要一个闭合截面，且 pipeline（``CstSolver.build_model``）
    走的是同一条规则。两处不一致的话，同一份 φ 会得到两种几何。
    """
    if bool(args.shape) == bool(args.from_ls):
        raise SystemExit("必须且只能给 --shape 或 --from-ls 之一"
                         "（--shape 给文件，--from-ls 用最近一轮的 φ 快照）")
    if args.shape:
        polys = artifacts.load_shape(args.shape)
        log(f"[OK] 形状来自文件：{args.shape}")
    else:
        last = artifacts.latest_iteration(cfg.output.dir)
        if last is None:
            raise SystemExit("还没有任何轮次，--from-ls 无从取起；先用 --shape "
                             "给一个形状文件")
        npz = artifacts.iteration_dir(cfg.output.dir, last) / "ls_phi.npz"
        if not npz.is_file():
            raise SystemExit(f"缺少 φ 快照 {npz}（只有 pipeline 跑过的轮次才有）")
        polys = _contours_from_phi(cfg, artifacts.load_ls_phi(npz), log=log)
    return close_open_contours(polys, cfg.design_region.box)


def _contours_from_phi(cfg: CaseConfig, snap: dict, *, log=print):
    """由 φ 快照提取可动轮廓（与 pipeline 同一套提取 + 重采样）。"""
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


if __name__ == "__main__":
    sys.exit(main())
