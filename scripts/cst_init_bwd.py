#!/usr/bin/env python
"""初始化一个 CST 仿真工程（fwd / bwd）并跑第一次仿真。

本文件是一个**完整、自包含**的程序；``cst_init_fwd.py`` 与
``cst_init_bwd.py`` **逐字相同，只有下面 TAG 常量那一块不同**
（tests/test_cst_init_scripts.py 锁定这一点，防止两份悄悄漂移）。

    python scripts/cst_init_fwd.py configs/coupler.yaml
    python scripts/cst_init_fwd.py configs/coupler.yaml --attach   # 附接 GUI

流程（直接调 CST 官方库，中间没有封装层）：
  1. 连实例：默认复用已运行的（没有就新建静态实例）；``--attach`` 只附接，
     连不上直接报错——绝不偷偷起新实例；
  2. 新建 MWS 工程 → ``model3d.add_to_history`` 逐块写模板命令
     （激励端口在这里写死：fwd = objective.from_port，bwd = to_port，
     此后**永不触碰**激励 API）；
  3. 存盘 → ``run_solver()`` → **再存盘**（``cst.results`` 读的是磁盘结果）；
  4. 读 S 参数 + 导 E/H 场并裁到设计区，结果写进 ``iter_000/``
     （**不写 shape.json**：首轮形状就是模板里的 initial_metal）。

产物：``<output.dir>/cst/<name>_<TAG>.cst``、``<output.dir>/iter_000/``。
两个 init 都跑完，``iter_000/`` 才算完成（见 artifacts.list_iterations）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# ===================== 两个 init 脚本唯一的差异 =====================
TAG = "bwd"                 # "fwd" = 输入端口激励；"bwd" = 观测端口激励
# ===================================================================

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
from eaopt.config import CaseConfig
from eaopt.solver import cst_model as M
from eaopt.solver import cst_results as R
from eaopt.solver.base import Solution
from eaopt.solver.cst_setup import COUPLER


def main(argv=None) -> int:
    safe_console()
    p = base_parser(f"初始化 {TAG} 仿真工程并跑第一次仿真")
    args = p.parse_args(argv)
    log = make_log(args.log)
    cfg = CaseConfig.from_yaml(args.config)
    banner(log, f"初始化 {TAG} 工程", cfg)

    setup = COUPLER
    path = setup.project_path(TAG, cfg.output.dir, cfg.name)
    try:
        _check_absent(path)
        de = _connect(args.attach, args.force_new, log)
        prj = de.new_mws()
        log("[OK] 新建 MWS 工程")
        port = setup.stimulus(TAG, cfg.objective)
        blocks = M.template_blocks(f"{cfg.name}_{TAG}", port)
        log(f"[ .. ] 建 {TAG} 工程：{len(blocks)} 个命令块，激励端口 = {port}")
        _apply_blocks(prj.model3d, blocks, f"建 {TAG} 工程（激励端口 {port}）",
                      log)
        _save(prj, path, log)
        sol = _solve_and_record(cfg, setup, prj, path, tag=TAG, iteration=0,
                                save_fields=_save_fields(args, setup), log=log)
    except Exception as e:
        log(f"[FAIL] {TAG} 初始化失败：{e}")
        return 1
    report_solution(log, cfg, sol, TAG, 0, path)
    log("[OK] 完成。另一个工程跑同目录下的另一个 cst_init 脚本；之后用 "
        "scripts/cst_update.py 做形状迭代，或直接跑 scripts/run_coupler.py "
        "一条命令跑完整个循环。")
    return 0


# --------------------------------------------------------------------------- #
# 会话 / 工程
# --------------------------------------------------------------------------- #
def _connect(attach: bool, force_new: bool, log):
    """拿到一个 ``DesignEnvironment``。

    ``attach`` 只附接已运行的实例（连不上**必须**报错）；``--new`` 只新建；
    默认先试附接、没有才新建（复用上一次会话与已打开的工程）。
    """
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
    """切静默模式（``CstSetup.quiet_mode``），返回 ``de`` 方便连写。

    本脚本不用清结果：工程是**新建**的（``_check_absent`` 挡着），求解时
    没有旧结果可删，也就没有那个确认框。
    """
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


def _check_absent(path: Path) -> None:
    if path.is_file():
        raise FileExistsError(
            f"{TAG} 工程已经存在：{path}\n"
            "  要重建请先删掉它（连同同名文件夹），或直接用 "
            "scripts/cst_update.py 改形状。")


def _save(prj, path: Path, log, what: str = "保存工程") -> None:
    """保存工程到 ``path``（父目录自动建）。

    ``prj.save`` 是本仓库唯一没在服务器实测过的调用，所以保留两段式：
    先按官方文档的 ``save(path, allow_overwrite=True)``，形参不存在
    （TypeError）再退回 ``save(path)``。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prj.save(str(path), allow_overwrite=True)
    except TypeError:                           # 该版本没有这个形参
        prj.save(str(path))
    log(f"[OK] {what}：{path}")


def _apply_blocks(m3d, blocks, what: str, log) -> int:
    """逐块 ``add_to_history``；成功返回 True，命令有错抛 RuntimeError。

    这里只补"第几块 / 哪一块"的上下文与命令原文，CST 的原始诊断原样带出。
    ``add_to_history`` 是**同步**的（返回即模型已改好）；返回 False 说明
    CST 既没执行也没报错——最危险的一种，当场停。
    """
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


# --------------------------------------------------------------------------- #
# 求解 / 取结果
# --------------------------------------------------------------------------- #
def _solve_and_record(cfg: CaseConfig, setup, prj, path, *, tag: str,
                      iteration: int, save_fields: bool, log) -> Solution:
    """求解 → 存盘 → 读 S 参数 → 导 E/H 场 → 写 ``iter_NNN/``。

    **存盘必须排在读结果前面**：``cst.results`` 读的是磁盘上的结果文件，
    顺序反了会读到上一轮的值（不报错，只是数据错）。
    """
    m3d = prj.model3d
    seconds = _run_solver(m3d, log)
    _save(prj, path, log, what="求解后存盘")

    sp = R.read_s_params(path, setup.stimulus(tag, cfg.objective),
                         response_ports=tuple(setup.ports),
                         freq_ghz=float(setup.frequency_ghz), log=log)
    e_field = h_field = None
    if save_fields:
        e_field = _export(m3d, "Efield", cfg, setup, tag, log)
        h_field = _export(m3d, "Hfield", cfg, setup, tag, log)
    sol = Solution(s_params=sp, e_field=e_field, h_field=h_field,
                   pin=float(setup.port_power_w),
                   extra={"tag": tag, "solver_seconds": seconds})
    if iteration is not None:
        paths = artifacts.save_solution(cfg.output.dir, int(iteration), tag,
                                        sol, save_fields=bool(save_fields))
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
    """导出该场并裁到设计区 ± field_margin_mm（整域网格没有保留价值）。"""
    out = Path(cfg.output.dir) / "cst_work" / f"{tag}_{field_type}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    return R.export_field_cropped(
        m3d, field_type, float(setup.frequency_ghz),
        setup.resolve_export_step(cfg.field_export_step_mm), out,
        cfg.design_region.box, float(cfg.design_region.field_margin_mm), log=log)


def _save_fields(args, setup) -> bool:
    """``--save-fields`` / ``--no-save-fields`` 没给时跟随 CstSetup。"""
    return setup.save_fields if args.save_fields is None else bool(args.save_fields)


if __name__ == "__main__":
    sys.exit(main())
