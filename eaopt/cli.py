"""命令行脚本的公共小事：控制台安全、参数表、日志、结果报告。

这里全是**纯展示/纯 argparse**——不碰 CST，也不碰仿真流程。三个 CST 程序
（``scripts/cst_init_fwd.py`` / ``cst_init_bwd.py`` / ``cst_update.py``）与
``scripts/run_coupler.py`` 共用这一份；各脚本自己的流程代码在各自文件里。

（脚本也可以直接 ``import cst.interface``——本模块不代劳，那不是它的职责。）
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from eaopt.config import CaseConfig
from eaopt.optimize.objective import make_fom
from eaopt.solver.case import load_case
from eaopt.solver.cst_setup import CstSetup

__all__ = ["safe_console", "base_parser", "make_log", "banner",
           "report_solution", "db"]


def safe_console() -> None:
    """把 stdout/stderr 的编码错误策略改成 ``replace``。

    服务器控制台常是 GBK（cp936）：中文本身没问题，但个别符号（如
    U+21D2 arrow、"≠" 之类）不在 GBK 里，一 print 就 UnicodeEncodeError
    ——**整个脚本带着前面所有有用的输出一起崩掉**（实测踩过）。改成
    ``errors="replace"`` 后，编不出的字符打印成 ``?``，其余照常。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:          # 非 TextIOWrapper（如被重定向到管道）
            pass


def base_parser(description: str,
                config_default: str | None = None) -> argparse.ArgumentParser:
    """三个 CST 脚本 + run_coupler 共用的命令行参数。

    没有 ``--lib-dir``：CST 官方库在**脚本顶层**就被 import 了（不封装它的
    前提），运行期再指定库路径不可能生效。库要能被找到，只有两条路——CST
    安装时写入的 .pth，或 ``PYTHONPATH``（见 docs/server_runbook.md 第 0 节）。
    """
    p = argparse.ArgumentParser(description=description)
    if config_default is None:
        p.add_argument("config", help="算例 YAML（如 configs/coupler.yaml）")
    else:
        p.add_argument("config", nargs="?", default=config_default,
                       help=f"算例 YAML（默认 {config_default}）")
    p.add_argument("--attach", action="store_true",
                   help="只附接已运行的 CST 实例（GUI 调试用；连不上就报错）")
    p.add_argument("--new", dest="force_new", action="store_true",
                   help="强制新建一个无 GUI 的静态实例")
    p.add_argument("--save-fields", dest="save_fields", action="store_true",
                   default=None, help="把 E/H 场也写进 iter_NNN/（.npz）")
    p.add_argument("--no-save-fields", dest="save_fields", action="store_false",
                   help="不写场文件（覆盖 CstSetup.save_fields）")
    p.add_argument("--log", default=None,
                   help="把输出同时写到这个文件（UTF-8）")
    return p


def make_log(path):
    """日志函数：stdout + （可选）UTF-8 文件。"""
    if not path:
        return print
    fh = open(path, "w", encoding="utf-8")

    def log(msg=""):
        print(msg)
        fh.write(str(msg) + "\n")
        fh.flush()

    return log


def banner(log, title: str, cfg: CaseConfig, setup: CstSetup | None = None) -> None:
    """开跑前的抬头：算例名、两个工程路径、激励端口、频点、导出步长。

    ``setup`` 不给就按 ``cfg.name`` 现取（见 ``solver/case.py``）——算例名拼错
    会**在这里**就报错，而不是等建完模板才发现几何不对。
    """
    if setup is None:
        setup, _ = load_case(cfg)
    step_xy, step_z = setup.resolve_export_steps(cfg.field_export_step_mm)
    log("=" * 72)
    log(f"{title} —— 算例 {cfg.name}")
    log("=" * 72)
    log(f"工程 fwd    : {setup.project_path('fwd', cfg.output.dir, cfg.name)}")
    log(f"工程 bwd    : {setup.project_path('bwd', cfg.output.dir, cfg.name)}")
    log(f"激励端口    : fwd={setup.stimulus('fwd', cfg.objective)} "
        f"bwd={setup.stimulus('bwd', cfg.objective)}  "
        f"（建工程时写死，之后不再触碰激励 API）")
    log(f"频点        : {setup.frequency_ghz} GHz")
    log(f"场导出步长  : {step_xy:g} mm（面内）× {step_z:g} mm（z）")
    log("-" * 72)


def report_solution(log, cfg: CaseConfig, sol, tag: str, n: int,
                    project) -> None:
    """统一的结果报告块（用户直接贴回来就能定位问题）。"""
    log("-" * 72)
    log(f"[OK] {tag} @ iter {n:03d}  工程 {project}")
    for (i, j), v in sorted(sol.s_params.items()):
        log(f"    |S{i},{j}| = {abs(v):8.5f}  ({db(v):7.2f} dB)")
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


def db(v: complex) -> float:
    m = abs(v)
    return 20.0 * np.log10(m) if m > 0 else float("-inf")
