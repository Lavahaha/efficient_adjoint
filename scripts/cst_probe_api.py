"""CST 官方 Python API 探针——**上服务器第一件事就跑这个**。

为什么需要它：我们这一层从 COM 换到官方库 ``cst.interface`` / ``cst.results``，
但"文档写的"和"这台机器上真实长什么样"经常不是一回事（旧 COM 路径就是这么
栽的：``AddToHistory`` 返回值拿不到、``GetActiveProject`` 根本不存在）。与其
继续猜，不如把该问的一次性问完，把输出贴回来，方案里所有"待核实"项就都落地了。

本脚本**只读 + 建临时工程**，不动任何既有工程；除了写明的地方不删东西。
每一步都单独 try/except——某节炸掉不影响其余节，正好看出是哪一层不对。

它回答这些问题（对应计划 §8.1）：
  1. ``import cst`` 到底拿到什么（含仓库 ``cst/`` 目录遮蔽检测）
  2. ``DesignEnvironment`` 有哪些成员、``new()``/``connect_to_any()`` 哪个能起来
  3. ``de.new_mws()`` 给的工程对象上有什么、``add_to_history`` 的调用形状
     （title/cmd 顺序、关键字名、坏 VBA 抛什么）
  4. ``Component.Delete`` 在历史表里合不合法（形状更新要靠它）
  5. **重放验证**：存盘 → 关闭 → 重开，实体还在不在（CST 模型是历史表重放出来的）
  6. ``SelectTreeItem``/``ASCIIExport``/``ResultTree``/``Solver`` 能否直接调、
     会不会进历史表
  7. ``run_solver()`` 一次真实求解
  8. ``cst.results`` 能读到哪些条目（S 参数的**真名**）、xdata/ydata 的形状
  9. ASCIIExport 的 ``Mode``/``Step`` 语义与导出文件头（核对我们的解析器）
 10. 脚本退出后 CST 实例是否还活着（决定 init 脚本要不要 ``--close``）

用法（服务器）::

    python scripts/cst_probe_api.py configs/coupler.yaml
    python scripts/cst_probe_api.py --cst-dir "C:/Program Files/CST Studio Suite 2024"
    python scripts/cst_probe_api.py --reconnect      # 隔一会儿再跑一次，看实例还在不在

把**完整输出**贴回给开发者。
"""

from __future__ import annotations

import argparse
import tempfile
import traceback
from pathlib import Path

TMP_PREFIX = "probeapi"
HDR = "=" * 68


def _short(exc: BaseException, limit: int = 300) -> str:
    s = " ".join(str(exc).split())
    return s if len(s) <= limit else s[:limit] + "…"


def _try(label: str, fn, *args, **kwargs):
    """跑一步并打印结果/原始异常；返回 ``(ok, 值)``。绝不往外抛。"""
    try:
        val = fn(*args, **kwargs)
    except Exception as e:
        print(f"  [FAIL] {label}: {type(e).__name__}: {_short(e)}")
        return False, None
    print(f"  [ OK ] {label} → {val!r}")
    return True, val


def _filtered_dir(obj, keywords) -> list[str]:
    try:
        names = dir(obj)
    except Exception as e:
        return [f"<dir() 失败：{_short(e)}>"]
    return sorted(n for n in names
                  if not n.startswith("_") and any(k.lower() in n.lower() for k in keywords))


def _dump_members(obj, label: str, keywords) -> None:
    names = _filtered_dir(obj, keywords)
    print(f"  {label} 含 {keywords} 的成员（{len(names)}）：")
    for n in names:
        print(f"    {n}")


def _signature(obj, name: str) -> str:
    import inspect
    try:
        attr = getattr(obj, name)
    except Exception as e:
        return f"<取不到 {name}: {_short(e)}>"
    try:
        return f"{name}{inspect.signature(attr)}"
    except Exception as e:
        return f"{name}(?)  <拿不到签名: {_short(e)}>"


# --------------------------------------------------------------------------- #
# 各节
# --------------------------------------------------------------------------- #
def section_import(args) -> tuple:
    """§1 导入 cst —— 含仓库 cst/ 目录遮蔽检测。"""
    print(HDR)
    print("§1 import cst（含仓库 cst/ 目录遮蔽检测）")
    print(HDR)
    from eaopt.solver import cst_session as S

    for line in S.describe_import_state():
        print(f"  {line}")

    libs = S.cst_library_candidates(install_dir=args.cst_dir,
                                    extra=[args.lib_dir] if args.lib_dir else ())
    print(f"  候选库目录（{len(libs)}）：")
    for p in libs:
        print(f"    {p}")

    try:
        cst, iface, results = S.load_cst(install_dir=args.cst_dir, lib_dir=args.lib_dir)
    except S.CstImportError as e:
        print(f"  [FAIL] load_cst：\n{e}")
        return None, None, None
    except Exception as e:
        print(f"  [FAIL] load_cst 抛了非预期异常：{type(e).__name__}: {_short(e)}")
        traceback.print_exc()
        return None, None, None

    print(f"  [ OK ] cst.__file__ = {cst.__file__}")
    print(f"  [ OK ] cst.__version__ = {getattr(cst, '__version__', '<无>')}")
    print(f"  [ OK ] cst.interface  = {iface.__file__}")
    print(f"  [ OK ] cst.results    = {results.__file__}")
    return cst, iface, results


def section_environment(iface):
    """§2 DesignEnvironment 成员与候选方法（不建工程）。"""
    print()
    print(HDR)
    print("§2 DesignEnvironment：成员 + 候选方法逐个试")
    print(HDR)
    _dump_members(iface.DesignEnvironment, "DesignEnvironment",
                  ("new", "connect", "project", "close", "active", "quiet", "mode", "list"))
    print(f"  StartMode: {getattr(iface, 'StartMode', '<无>')}")
    for name in ("new", "connect_to_any", "connect_to_any_or_new", "connect_to_new",
                 "active_project", "get_open_project", "get_open_projects",
                 "list_open_projects", "has_active_project", "is_connected", "close"):
        print(f"    {_signature(iface.DesignEnvironment, name)}")
    return None


def _launch(iface, mode: str):
    """按 mode 起/连实例，返回 (de, 说明)。失败把原始异常打出来。"""
    if mode == "new":
        try:
            de = iface.DesignEnvironment.new()
            return de, "DesignEnvironment.new()"
        except Exception as e:
            print(f"  [FAIL] DesignEnvironment.new(): {type(e).__name__}: {_short(e)}")
            return None, ""
    if mode == "connect":
        try:
            de = iface.DesignEnvironment.connect_to_any()
            return de, "DesignEnvironment.connect_to_any()（附接已有实例）"
        except Exception as e:
            print(f"  [FAIL] DesignEnvironment.connect_to_any(): {type(e).__name__}: {_short(e)}")
            return None, ""
    try:
        de = iface.DesignEnvironment.connect_to_any_or_new()
        return de, "DesignEnvironment.connect_to_any_or_new()"
    except Exception as e:
        print(f"  [FAIL] connect_to_any_or_new(): {type(e).__name__}: {_short(e)}")
    return _launch(iface, "new")


def section_project(cst, iface, args):
    """§3-§5 建工程、建模进历史表、Component.Delete、重放验证。"""
    print()
    print(HDR)
    print("§3 起实例 + 建工程 + add_to_history")
    print(HDR)
    de, how = _launch(iface, args.launch)
    if de is None:
        print("  [FAIL] 两个模式都起不来——看上面原始异常（许可证？无 GUI 模式？）")
        return None, None, None
    print(f"  [ OK ] 实例：{how}")
    print(f"    de 含 history/project 的成员：{_filtered_dir(de, ('open', 'project', 'close', 'active'))}")

    ok, prj = _try("de.new_mws()", de.new_mws)
    if not ok:
        return de, None, None
    _dump_members(prj, "prj", ("history", "model", "select", "result", "export",
                               "save", "ascii", "component", "port", "solver", "close"))
    for name in ("save", "close", "activate", "SelectTreeItem",
                 "add_to_history", "run_solver", "get_all_historical_results"):
        print(f"    {_signature(prj, name)}")

    print("  --- prj.model3d ---")
    m3d = None
    try:
        m3d = prj.model3d
        _dump_members(m3d, "prj.model3d", ("history", "add", "reset", "run", "solver", "full"))
        print(f"    {_signature(m3d, 'add_to_history')}")
        print(f"    {_signature(m3d, 'run_solver')}")
    except Exception as e:
        print(f"  [FAIL] prj.model3d：{_short(e)}")

    print("  --- add_to_history 调用形状矩阵（每条形如可见 Brick）---")
    obj, obj_label = (m3d, "prj.model3d") if m3d is not None else (prj, "prj")
    if obj is None:
        print("  !! 没有可用的建模对象，跳过")
        return de, prj, None
    for label, cargs, ckwargs in _history_shapes(TMP_PREFIX + "A"):
        try:
            ret = obj.add_to_history(*cargs, **ckwargs)
        except TypeError as e:
            print(f"    [{label}] TypeError（形状不对）：{_short(e)}")
            continue
        except Exception as e:
            print(f"    [{label}] 抛错：{type(e).__name__}: {_short(e)}")
            continue
        tag = "[ ?? ]" if ret is None else ("[ OK ]" if ret else "[FAIL]")
        print(f"    {tag} [{label}] 返回 {ret!r}")
    print("  --- 坏 VBA 抛什么（决定我们怎么判失败）---")
    try:
        obj.add_to_history("probe-bad", 'This is not valid VBA at all\n')
        print("    [ ?? ] 坏 VBA 没抛错——返回值不能当判据，得靠模型树/文件层")
    except Exception as e:
        print(f"    [ OK ] 坏 VBA 抛：{type(e).__name__}: {_short(e)}")

    print("  --- Component.Delete 两种写法（形状更新要靠它）---")
    for label, cmd in (("一行式", f'Component.Delete "{TMP_PREFIX}A-1"\n'),
                       ("With 块",
                        f'With Component\n    .Delete "{TMP_PREFIX}A-1"\nEnd With\n'),
                       ("删不存在的组件", f'Component.Delete "{TMP_PREFIX}-nope"\n')):
        try:
            ret = obj.add_to_history(f"probe-del-{label}", cmd)
            print(f"    [ OK ] {label}: 返回 {ret!r}")
        except Exception as e:
            print(f"    [FAIL] {label}: {type(e).__name__}: {_short(e)}")

    print("  --- 历史表长度 ---")
    for name in ("full_history", "history", "get_full_history", "reset_history"):
        f = getattr(m3d, name, None)
        if f is None:
            continue
        try:
            h = f() if callable(f) else f
            print(f"    {obj_label}.{name} → 长度 {len(h)}")
        except Exception as e:
            print(f"    {obj_label}.{name} 调用失败：{_short(e)}")
    return de, prj, obj


def _brick_cmd(name: str) -> str:
    """一条**看得见**的 Brick 命令——返回值靠不住时按模型树判断。"""
    return (f'With Brick\n    .Reset\n    .Name "{name}"\n    .Component "probe"\n'
            f'    .Material "PEC"\n'
            f'    .Xrange "0", "1"\n    .Yrange "0", "1"\n    .Zrange "0", "1"\n'
            f'    .Create\nEnd With\n')


def _history_shapes(prefix: str) -> list[tuple[str, tuple, dict]]:
    """add_to_history 的候选调用形状 ``(标签, 位置参数, 关键字参数)``。

    参数顺序与关键字名各版本可能不同（COM 时代就栽在"调用形状靠猜"上），
    所以一次把已知的几种都试一遍：跑完到 CST 模型树里数 Brick
    ``<prefix>-1/2/3`` 哪个真的出现了，那个形状才算数。
    """
    return [
        ("title,cmd", (f"probe-title-cmd", _brick_cmd(f"{prefix}-1")), {}),
        ("cmd,title", (_brick_cmd(f"{prefix}-2"), "probe-cmd-title"), {}),
        ("header=,contents=", (),
         {"header": "probe-kwargs-header", "contents": _brick_cmd(f"{prefix}-3")}),
        ("title=,vba=", (), {"title": "probe-kwargs-title",
                             "vba": _brick_cmd(f"{prefix}-4")}),
    ]


def section_replay(prj, de, args) -> None:
    """§5 重放验证：存盘 → 关闭 → 重开，实体还在不在。"""
    print()
    print(HDR)
    print("§5 重放验证（存盘 → 关闭 → 重开）—— CST 的模型是历史表重放出来的")
    print(HDR)
    tmp = Path(args.tmpdir or tempfile.mkdtemp(prefix="cst_probe_"))
    path = tmp / "probe_replay.cst"
    ok, _ = _try(f"prj.save({path}, allow_overwrite=True)", prj.save,
                 str(path), allow_overwrite=True)
    if not ok:
        _try("prj.save(path)（不带 allow_overwrite）", prj.save, str(path))

    from eaopt.solver import cst_project
    print("  --- 文件层检查（不用 CST）---")
    for ln in cst_project.describe(path):
        print(f"    {ln}")

    _try("prj.close()", prj.close)
    ok, prj2 = _try(f"de.open_project({path})", de.open_project, str(path))
    if ok and prj2 is not None:
        try:
            n = len(prj2.model3d.full_history())
            print(f"  [ OK ] 重开后历史长度 {n}")
        except Exception as e:
            print(f"  [ ?? ] 重开后读历史失败：{_short(e)}")
        _try("重开后 close()", prj2.close)
    print(f"  （临时工程在 {tmp}，确认后可删）")


def section_direct_calls(prj, obj) -> None:
    """§6 即时执行探针：SelectTreeItem / ASCIIExport / ResultTree / Solver。"""
    print()
    print(HDR)
    print("§6 对象模型是否**直接可调**（不进历史表的那种）")
    print(HDR)
    if prj is None:
        print("  !! 没有工程对象，跳过")
        return
    for name in ("SelectTreeItem", "ASCIIExport", "ResultTree", "Solver",
                 "Component", "ResultTree2", "GetResultFromTreeItem"):
        has = hasattr(prj, name)
        print(f"    prj.{name:<22} {'有' if has else '**没有**'}")
    if hasattr(prj, "ASCIIExport"):
        _dump_members(prj.ASCIIExport, "prj.ASCIIExport",
                      ("file", "mode", "step", "execute", "reset", "point", "range"))
    if hasattr(prj, "Solver"):
        _dump_members(prj.Solver, "prj.Solver", ("start", "port", "freq", "stim"))
    if hasattr(prj, "ResultTree"):
        _dump_members(prj.ResultTree, "prj.ResultTree", ("item", "tree", "result", "id"))


def section_solve_and_results(prj, cst, results, args, workdir: Path) -> None:
    """§7-§9 求解 + cst.results 读数 + 场导出。"""
    print()
    print(HDR)
    print("§7 求解（run_solver）")
    print(HDR)
    if prj is None:
        return
    import time
    t0 = time.time()
    ok, _ = _try("prj.model3d.run_solver()", prj.model3d.run_solver)
    if ok:
        print(f"    求解耗时 {time.time() - t0:.1f} s")

    path = workdir / "probe_solve.cst"
    _try("存盘以便 cst.results 读结果", prj.save, str(path), allow_overwrite=True)

    print()
    print(HDR)
    print("§8 cst.results 读结果（S 参数的真名在这里）")
    print(HDR)
    try:
        pf = results.ProjectFile(str(path), allow_interactive=True)
    except TypeError:
        pf = results.ProjectFile(str(path))
    except Exception as e:
        print(f"  [FAIL] ProjectFile：{_short(e)}")
        return
    ok, rm = _try("ProjectFile.get_3d()", pf.get_3d)
    if not ok:
        return
    names: list[str] = []
    for flt in ("0D/1D", "1D", None):
        try:
            items = rm.get_tree_items(filter=flt) if flt else rm.get_tree_items()
        except Exception as e:
            print(f"  get_tree_items(filter={flt!r}) 失败：{_short(e)}")
            continue
        names = [getattr(i, "tree_path", None) or str(i) for i in items]
        print(f"  get_tree_items(filter={flt!r}) → {len(names)} 条：")
        for n in names[:40]:
            print(f"    {n}")
        if names:
            break
    for cand in [n for n in names if "S-Parameter" in n or "S1,1" in n][:3]:
        print(f"  --- 试着读 {cand} ---")
        ok, item = _try(f"get_result_item({cand!r})", rm.get_result_item, cand)
        if not ok:
            continue
        for meth in ("get_xdata", "get_ydata", "get_data"):
            f = getattr(item, meth, None)
            if f is None:
                print(f"    {meth}: **没有**")
                continue
            try:
                v = f()
                shape = getattr(v, "shape", None)
                print(f"    {meth}() → {type(v).__name__} shape={shape} "
                      f"前几个={list(v.ravel()[:4]) if shape else v!r}")
            except Exception as e:
                print(f"    {meth}() 失败：{_short(e)}")

    print()
    print(HDR)
    print("§9 ASCIIExport 导出（Mode/Step 语义 + 文件头）")
    print(HDR)
    e_name = next((n for n in names if "e-field" in n.lower()), None)
    if e_name is None:
        print("  结果树里没有 e-field 条目（模板里没建监视器？先跳过）")
        return
    raw = workdir / "probe_e.txt"
    _try(f"prj.SelectTreeItem({e_name!r})", prj.SelectTreeItem, e_name)
    ax = getattr(prj, "ASCIIExport", None)
    if ax is None:
        print("  !! 没有 prj.ASCIIExport")
        return
    _try("ASCIIExport.Reset()", ax.Reset)
    _try(f'ASCIIExport.FileName({raw})', ax.FileName, str(raw))
    for mode in ("FixedWidth", "FixedNumber"):
        _try(f'ASCIIExport.Mode("{mode}")', ax.Mode, mode)
        _try('ASCIIExport.StepX("0.2")', ax.StepX, "0.2")
        ok, _ = _try("ASCIIExport.Execute()", ax.Execute)
        if ok and raw.exists():
            lines = raw.read_text(errors="replace").splitlines()
            print(f"    文件 {raw.stat().st_size} 字节，共 {len(lines)} 行；头 12 行：")
            for ln in lines[:12]:
                print(f"      {ln}")
            break
    print("  注意：StepX 到底是「步长 mm」还是「点数」，看上面的行数/坐标跨度"
          "——这是我们要核对的最后一件事。")


def section_final(de, args) -> None:
    print()
    print(HDR)
    print("§10 收尾")
    print(HDR)
    if args.close:
        _try("de.close()", de.close)
        print("  已关闭实例。**现在再跑一次 `python scripts/cst_probe_api.py --reconnect`"
              "看还能不能连上**——这决定 init 脚本退出后要不要 --close。")
    else:
        print("  实例**保持打开**（默认）。请另开一个终端跑：")
        print("    python scripts/cst_probe_api.py --reconnect")


# --------------------------------------------------------------------------- #
def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(
        description="CST 官方 Python API 探针（上服务器第一件事就跑这个）")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml")
    ap.add_argument("--cst-dir", default=None,
                    help="CST 安装根，如 \"C:/Program Files/CST Studio Suite 2024\"")
    ap.add_argument("--lib-dir", default=None,
                    help="直接给 <CST>/AMD64/python_cst_libraries")
    ap.add_argument("--launch", choices=("auto", "new", "connect"), default="auto")
    ap.add_argument("--tmpdir", default=None, help="临时工程放哪（默认系统临时目录）")
    ap.add_argument("--close", action="store_true", help="结尾关掉 CST 实例")
    ap.add_argument("--reconnect", action="store_true",
                    help="只试 connect_to_any()：看之前的实例还在不在")
    args = ap.parse_args()

    # 配置里可能有 cst_install_dir / cst_python_libs，作为命令行缺省
    if args.cst_dir is None or args.lib_dir is None:
        try:
            from eaopt.config import CaseConfig
            spec = CaseConfig.from_yaml(args.config).solver
            args.cst_dir = args.cst_dir or getattr(spec, "cst_install_dir", None)
            args.lib_dir = args.lib_dir or getattr(spec, "python_library_path", None)
        except Exception as e:
            print(f"（读配置 {args.config} 失败，忽略：{_short(e)}）")

    print(HDR)
    print("CST 官方 Python API 探针")
    print(HDR)
    if args.reconnect:
        args.launch = "connect"

    cst, iface, results = section_import(args)
    if iface is None:
        print("\n!! 库都导不进来，后面的都做不了。先按上面的提示解决 import。")
        return

    if args.reconnect:
        de, how = _launch(iface, "connect")
        if de is None:
            print("\n=> 连不上：**上一个进程退出后 CST 实例也没了**。"
                  "init/update 脚本默认不要 --close，且每个脚本进来都应能"
                  "自行 new() 兜底。")
        else:
            print(f"\n=> 连上了（{how}）：**实例在进程退出后仍然活着**，"
                  "脚本之间可以附接同一个实例。")
            print(f"   当前打开的工程：{_filtered_dir(de, ('project',))}")
        return

    section_environment(iface)
    de, prj, obj = section_project(cst, iface, args)
    if de is not None and prj is not None:
        section_direct_calls(prj, obj)
        tmp = Path(args.tmpdir or tempfile.mkdtemp(prefix="cst_probe_"))
        tmp.mkdir(parents=True, exist_ok=True)
        section_solve_and_results(prj, cst, results, args, tmp)
        section_replay(prj, de, args)
    if de is not None:
        section_final(de, args)

    print()
    print(HDR)
    print("把以上**完整输出**贴回给开发者。重点看：")
    print("  · §1 有没有 cst/ 目录遮蔽；§3 add_to_history 哪一行返回 True")
    print("  · §5 重开后历史长度（== 0 说明存的还是空工程）")
    print("  · §8 S 参数条目的**真名**；§9 导出文件头的三行与点数")
    print("  · §10 再跑一次 --reconnect 的结果（实例是否存活）")
    print(HDR)


if __name__ == "__main__":
    main()
