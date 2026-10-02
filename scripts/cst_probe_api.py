"""CST 官方 Python API 探针——**上服务器第一件事就跑这个**。

第一轮（2026-10-02，服务器 py310 + CST 2024 @ D:\\CST 2024）已经确认：

* ``model3d.add_to_history(header, vba_code, /, timeout=None)`` ——**位置参数、
  返回 True、坏 VBA 抛 RuntimeError**。失败从此完全可见（这是换官方库最大的收益）。
* ``prj`` 上**没有** SelectTreeItem/ASCIIExport/ResultTree/Solver/Component ——
  只有 ``model3d`` / ``modeler`` / ``save`` / ``close`` / ``activate``。
  所以"官方对象直接暴露 VBA 对象模型"对这个版本**不成立**，后处理得另找路。
* 没有 ``StartMode``；没有 ``quiet_mode`` 上下文管理器（是
  ``set_quiet_mode`` / ``quiet_mode_enabled`` / ``in_quiet_mode``）。
* ``model3d`` 上**没有 full_history**（只有 ``full_history_rebuild``）——
  "读历史长度"这个判据用不了，改用 ``cst_project.describe`` 的文件层检查。
* ``cst.results`` 的 ``get_tree_items(filter='0D/1D')`` 能列条目（裸工程里只有
  ``Excitation Signals\\default``）。
* 保存的 ``.cst`` **不是 zip**，模型在**同名文件夹**里 —— 复制必须整份搬。

本脚本第二轮要补的（都是上一轮**答不了**的）：

  §5 用 ``template_builder.template_blocks`` 建**真模板**（带 4 端口 + E/H 监视器）
  §6 真求解一次
  §7 ``cst.results`` 里 S 参数与**场条目**的真名；场能否用 ``get_data()`` 读出来
  §8 若读不出场：把 prj / model3d / modeler 的**全部成员**打出来找导出 API
  §4 ``Component.Delete`` 用**正确的组件名**再试，并试带 ``On Error Resume Next``
     的容错写法（形状更新每轮都要删 design_region）
  §9 重放验证（存盘 → 关闭 → 重开 → 文件层检查）

用法（服务器）::

    python scripts/cst_probe_api.py configs/coupler.yaml --template fwd
    python scripts/cst_probe_api.py --reconnect          # 另开终端，看实例是否存活

把**完整输出**贴回给开发者。
"""

from __future__ import annotations

import argparse
import tempfile
import time
import traceback
from pathlib import Path

HDR = "=" * 68
PROBE_COMPONENT = "probecomp"          # 探针自己建的组件名（Delete 测试要用对名字）


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


def _sig(obj, name: str) -> str:
    import inspect
    try:
        attr = getattr(obj, name)
    except Exception as e:
        return f"{name}: **没有**（{_short(e, 60)}）"
    try:
        return f"{name}{inspect.signature(attr)}"
    except Exception:
        return f"{name}(?)"


def _all_members(obj) -> list[str]:
    try:
        return sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception as e:
        return [f"<dir() 失败：{_short(e)}>"]


def _dump(obj, label: str, keywords=None) -> None:
    names = _all_members(obj)
    if keywords:
        names = [n for n in names if any(k.lower() in n.lower() for k in keywords)]
    print(f"  {label}（{len(names)}）：{names}")


def _brick_cmd(name: str, component: str) -> str:
    """一条**看得见**的 Brick 命令——返回值靠不住时按模型树判断。"""
    return (f'With Brick\n    .Reset\n    .Name "{name}"\n    .Component "{component}"\n'
            f'    .Material "PEC"\n'
            f'    .Xrange "0", "1"\n    .Yrange "0", "1"\n    .Zrange "0", "1"\n'
            f'    .Create\nEnd With\n')


# --------------------------------------------------------------------------- #
def section_import(args):
    print(HDR)
    print("§1 import cst（含仓库 cst/ 目录遮蔽检测）")
    print(HDR)
    from eaopt.solver import cst_session as S

    for line in S.describe_import_state():
        print(f"  {line}")
    libs = S.cst_library_candidates(install_dir=args.cst_dir,
                                    extra=[args.lib_dir] if args.lib_dir else ())
    print(f"  候选库目录（{len(libs)}）：{[str(p) for p in libs]}")
    on_path = [p for p in __import__("sys").path if "python_cst_libraries" in p]
    print(f"  sys.path 里已有的 CST 库目录：{on_path}")
    try:
        cst, iface, results = S.load_cst(install_dir=args.cst_dir, lib_dir=args.lib_dir)
    except S.CstImportError as e:
        print(f"[FAIL] load_cst：\n{e}")
        return None, None, None
    print(f"  [ OK ] cst.__file__ = {cst.__file__}")
    print(f"  [ OK ] cst.interface = {iface.__file__}")
    print(f"  [ OK ] cst.results   = {results.__file__}")
    return cst, iface, results


def section_environment(iface):
    print()
    print(HDR)
    print("§2 DesignEnvironment 成员与签名")
    print(HDR)
    _dump(iface.DesignEnvironment, "DesignEnvironment")
    for n in ("new", "connect_to_any", "connect_to_any_or_new", "new_mws",
              "open_project", "active_project", "get_open_project",
              "list_open_projects", "set_quiet_mode", "close"):
        print(f"    {_sig(iface.DesignEnvironment, n)}")


def _launch(iface, mode: str):
    if mode == "new":
        try:
            return iface.DesignEnvironment.new(), "DesignEnvironment.new()"
        except Exception as e:
            print(f"  [FAIL] new(): {type(e).__name__}: {_short(e)}")
            return None, ""
    if mode == "connect":
        try:
            return iface.DesignEnvironment.connect_to_any(), "connect_to_any()"
        except Exception as e:
            print(f"  [FAIL] connect_to_any(): {type(e).__name__}: {_short(e)}")
            return None, ""
    try:
        return iface.DesignEnvironment.connect_to_any_or_new(), "connect_to_any_or_new()"
    except Exception as e:
        print(f"  [FAIL] connect_to_any_or_new(): {type(e).__name__}: {_short(e)}")
    return _launch(iface, "new")


def section_project(iface, args):
    print()
    print(HDR)
    print("§3 起实例 + 建工程 + 全量成员")
    print(HDR)
    de, how = _launch(iface, args.launch)
    if de is None:
        print("  [FAIL] 起不来（看上面原始异常）")
        return None, None
    print(f"  [ OK ] 实例：{how}")
    print(f"    in_quiet_mode = {getattr(de, 'in_quiet_mode', '<无>')}")
    _dump(de, "de 全部成员")

    ok, prj = _try("de.new_mws()", de.new_mws)
    if not ok:
        return de, None
    _dump(prj, "prj 全部成员")
    for obj, label in ((prj, "prj"), (getattr(prj, "model3d", None), "prj.model3d"),
                       (getattr(prj, "modeler", None), "prj.modeler")):
        if obj is None:
            print(f"  {label} = <没有>")
            continue
        print(f"  --- {label} ---")
        _dump(obj, label)
    return de, prj


def section_history_commands(prj):
    """§4 add_to_history 形状 + Component.Delete（用对组件名）。"""
    print()
    print(HDR)
    print("§4 历史命令：add_to_history / Component.Delete / 容错写法")
    print(HDR)
    m3d = prj.model3d
    print(f"  签名：{_sig(m3d, 'add_to_history')}")

    ok, _ = _try("add_to_history(标题, 建 Brick 命令)",
                 m3d.add_to_history, "probe-brick", _brick_cmd("probeA-1", PROBE_COMPONENT))
    if not ok:
        return
    print("  --- Component.Delete：这次用**正确的组件名** ---")
    for label, cmd in (
        (f'删除存在的组件 "{PROBE_COMPONENT}"', f'Component.Delete "{PROBE_COMPONENT}"\n'),
        ('删除不存在的组件', 'Component.Delete "definitely-not-here"\n'),
        ("容错写法（On Error Resume Next）",
         'On Error Resume Next\n'
         'Component.Delete "definitely-not-here"\n'
         'Err.Clear\n'
         'On Error Goto 0\n'),
    ):
        try:
            ret = m3d.add_to_history(f"probe-del: {label}", cmd)
            print(f"    [ OK ] {label} → 返回 {ret!r}")
        except Exception as e:
            print(f"    [FAIL] {label} → {type(e).__name__}: {_short(e)}")

    print("  --- 读历史：找找有没有能回读历史表的成员 ---")
    for name in _all_members(m3d):
        if "hist" not in name.lower():
            continue
        f = getattr(m3d, name, None)
        try:
            v = f() if callable(f) else f
            print(f"    model3d.{name} → {type(v).__name__} "
                  f"{('len=%d' % len(v)) if hasattr(v, '__len__') else v!r}")
        except Exception as e:
            print(f"    model3d.{name} 调用失败：{_short(e, 120)}")


def section_template(prj, args) -> None:
    """§5 用真模板建工程（带 4 端口 + E/H 监视器）——上一轮缺的就是这个。"""
    print()
    print(HDR)
    print(f"§5 建真模板（--template {args.template}）：逐块 add_to_history")
    print(HDR)
    if args.template == "none":
        print("  （未指定 --template，跳过；加了才能测 S 参数与场导出）")
        return
    from eaopt.config import CaseConfig
    from eaopt.solver.template_builder import template_blocks
    cfg = CaseConfig.from_yaml(args.config)
    portnum = 1 if args.template == "fwd" else 3
    blocks = template_blocks(args.template, portnum)
    print(f"  {len(blocks)} 块，激励端口 {portnum}")
    n_ok = n_fail = 0
    for i, (header, cmd) in enumerate(blocks, 1):
        try:
            ret = prj.model3d.add_to_history(header, cmd)
        except Exception as e:
            print(f"  [FAIL] {i:2d} {header:<24} {type(e).__name__}: {_short(e)}")
            n_fail += 1
            continue
        tag = "[ ?? ]" if ret is None else ("[ OK ]" if ret else "[FAIL]")
        print(f"  {tag} {i:2d} {header:<24} 返回 {ret!r}")
        n_ok += ret is not False
    print(f"  → 成功 {n_ok} / 失败 {n_fail}")
    _dump(prj.model3d, "建完后的 model3d")


def section_solve(prj, args, workdir: Path) -> Path | None:
    """§6 真求解一次。"""
    print()
    print(HDR)
    print("§6 求解 run_solver")
    print(HDR)
    if args.no_solve:
        print("  （--no-solve 跳过）")
        return None
    t0 = time.time()
    ok, _ = _try(f"model3d.run_solver()（{args.template} 模板，可能要几分钟）",
                 prj.model3d.run_solver)
    print(f"  耗时 {time.time() - t0:.1f} s")
    path = workdir / f"probe_{args.template}.cst"
    _try("存盘（cst.results 从磁盘读结果）", prj.save, str(path), allow_overwrite=True)
    return path if ok or path.exists() else None


def section_results(cst, results, path: Path | None, workdir: Path) -> None:
    """§7 cst.results：列全部条目 + 读 S 参数 + 试读 3D 场。"""
    print()
    print(HDR)
    print("§7 cst.results：条目真名 + S 参数 + 场能否直接读")
    print(HDR)
    if path is None:
        print("  （没有可读的工程文件，跳过）")
        return
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
    _dump(rm, "ResultModule")

    seen: list[str] = []
    for flt in ("Tree", "0D/1D", "2D/3D", "1D", "2D", "3D", None):
        try:
            items = rm.get_tree_items() if flt is None else rm.get_tree_items(filter=flt)
        except Exception as e:
            print(f"  get_tree_items(filter={flt!r}) 失败：{_short(e, 120)}")
            continue
        names = [getattr(i, "tree_path", None) or str(i) for i in items]
        print(f"  --- filter={flt!r} → {len(names)} 条 ---")
        for n in names:
            print(f"    {n}")
        for n in names:
            if n not in seen:
                seen.append(n)

    targets = [n for n in seen if "S-Parameter" in n or "\\S" in n]
    targets += [n for n in seen if "field" in n.lower()]
    for name in targets[:6]:
        print(f"  --- 读 {name} ---")
        ok, item = _try(f"get_result_item({name!r})", rm.get_result_item, name)
        if not ok:
            continue
        _dump(item, "  ResultItem")
        for meth in ("get_xdata", "get_ydata", "get_data", "get_zdata"):
            f = getattr(item, meth, None)
            if f is None:
                print(f"    {meth}: **没有**")
                continue
            try:
                v = f()
                shape = getattr(v, "shape", None)
                print(f"    {meth}() → {type(v).__name__} shape={shape} "
                      f"{list(v.ravel()[:4]) if shape else v!r}")
            except Exception as e:
                print(f"    {meth}() 失败：{_short(e, 160)}")

    print()
    print("  --- 结果模块里含 export/ascii/field 的成员（场导出的可能入口）---")
    hits = [n for n in _all_members(rm)
            if any(k in n.lower() for k in ("export", "ascii", "touchstone", "field"))]
    print(f"    {hits}")


def section_export_search(prj) -> None:
    """§8 在场读不出来的情况下，把可能管导出的对象挖一遍。"""
    print()
    print(HDR)
    print("§8 找导出/后处理 API（prj / model3d / modeler 全量成员）")
    print(HDR)
    keys = ("export", "ascii", "result", "select", "touchstone", "field", "post",
            "tree", "plot", "get_", "run", "solver", "save")
    for label, obj in (("prj", prj), ("prj.model3d", getattr(prj, "model3d", None)),
                       ("prj.modeler", getattr(prj, "modeler", None))):
        if obj is None:
            print(f"  {label} = <没有>")
            continue
        hits = [n for n in _all_members(obj) if any(k in n.lower() for k in keys)]
        print(f"  {label} 里相关的（{len(hits)}）：")
        for n in hits:
            print(f"    {_sig(obj, n)}")

    print("  --- 有没有通往底层 COM 对象模型的逃生口 ---")
    for label, obj in (("prj", prj), ("prj.model3d", getattr(prj, "model3d", None)),
                       ("prj.modeler", getattr(prj, "modeler", None))):
        if obj is None:
            continue
        esc = [n for n in _all_members(obj)
               if any(k in n.lower() for k in ("com", "ole", "dispatch", "application",
                                               "vba", "execute", "macro", "script"))]
        print(f"    {label}: {esc}")
        for n in esc[:4]:
            try:
                v = getattr(obj, n)
                v = v() if callable(v) else v
                print(f"      {label}.{n}() → {type(v).__name__}: "
                      f"{[m for m in _all_members(v)][:15] if v is not None else v!r}")
            except Exception as e:
                print(f"      {label}.{n} 调用失败：{_short(e, 100)}")


def section_replay(prj, de, workdir: Path) -> None:
    """§9 重放验证：存盘 → 关闭 → 重开 → 文件层检查。"""
    print()
    print(HDR)
    print("§9 重放验证（存盘 → 关闭 → 重开）")
    print(HDR)
    path = workdir / "probe_replay.cst"
    _try(f"prj.save({path}, allow_overwrite=True)", prj.save, str(path),
         allow_overwrite=True)
    from eaopt.solver import cst_project
    print("  --- 文件层检查（不用 CST）---")
    for ln in cst_project.describe(path):
        print(f"    {ln}")
    _try("prj.close()", prj.close)
    ok, prj2 = _try("de.open_project(path)", de.open_project, str(path))
    if ok and prj2 is not None:
        m3d2 = getattr(prj2, "model3d", None)
        print(f"    重开后 model3d 的 history 相关成员："
              f"{[n for n in _all_members(m3d2) if 'hist' in n.lower()]}")
        if m3d2 is not None:
            _try("重开后 full_history_rebuild()（能看到历史长度吗）",
                 m3d2.full_history_rebuild)
        _try("重开后 close()", prj2.close)


def section_final(de, args) -> None:
    print()
    print(HDR)
    print("§10 收尾")
    print(HDR)
    if args.close:
        _try("de.close()", de.close)
        print("  已关闭。**再跑一次 `python scripts/cst_probe_api.py --reconnect`**"
              "看还能不能连上。")
    else:
        print("  实例**保持打开**。请另开终端跑：")
        print("    python scripts/cst_probe_api.py --reconnect")


# --------------------------------------------------------------------------- #
def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(
        description="CST 官方 Python API 探针（上服务器第一件事就跑这个）")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml")
    ap.add_argument("--cst-dir", default=None)
    ap.add_argument("--lib-dir", default=None)
    ap.add_argument("--launch", choices=("auto", "new", "connect"), default="auto")
    ap.add_argument("--template", choices=("none", "fwd", "bwd"), default="none",
                    help="建真模板（带端口与监视器）——测 S 参数与场导出必须加这个")
    ap.add_argument("--no-solve", action="store_true")
    ap.add_argument("--tmpdir", default=None)
    ap.add_argument("--close", action="store_true")
    ap.add_argument("--reconnect", action="store_true",
                    help="只试 connect_to_any()：看之前的实例还在不在")
    args = ap.parse_args()

    if args.cst_dir is None or args.lib_dir is None:
        try:
            from eaopt.config import CaseConfig
            spec = CaseConfig.from_yaml(args.config).solver
            args.cst_dir = args.cst_dir or getattr(spec, "cst_install_dir", None)
            args.lib_dir = args.lib_dir or getattr(spec, "python_library_path", None)
        except Exception as e:
            print(f"（读配置 {args.config} 失败，忽略：{_short(e)}）")

    print(HDR)
    print("CST 官方 Python API 探针（第二轮：模板 / 求解 / 结果 / 场导出）")
    print(HDR)

    cst, iface, results = section_import(args)
    if iface is None:
        print("\n!! 库导不进来，后面的都做不了。")
        return

    if args.reconnect:
        de, how = _launch(iface, "connect")
        if de is None:
            print("\n=> 连不上：**上一个进程退出后 CST 实例也没了**。"
                  "init/update 脚本默认别关实例，且每个脚本都要能自己 new() 兜底。")
        else:
            print(f"\n=> 连上了（{how}）：**实例在进程退出后仍然活着**，"
                  "脚本之间可以附接同一个实例。")
            _try("de.get_open_projects()", de.get_open_projects)
            _try("de.list_open_projects()", de.list_open_projects)
            _try("de.active_project()", de.active_project)
        return

    section_environment(iface)
    de, prj = section_project(iface, args)
    if de is None or prj is None:
        print("\n!! 没有工程对象，后面的都做不了。")
        return

    tmp = Path(args.tmpdir or tempfile.mkdtemp(prefix="cst_probe_"))
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        section_history_commands(prj)
        section_template(prj, args)
        path = section_solve(prj, args, tmp)
        section_results(cst, results, path, tmp)
        section_export_search(prj)
        section_replay(prj, de, tmp)
    except Exception:
        print("\n!! 探针自己抛了未捕获异常（把它贴回来）：")
        traceback.print_exc()
    section_final(de, args)

    print()
    print(HDR)
    print("把以上**完整输出**贴回给开发者。这一轮最要紧的三件事：")
    print("  · §5 真模板逐块是否都 [ OK ]（这是建模能不能成）")
    print("  · §7 里 S 参数条目的**真名**，以及 e-field 条目能否 get_data()")
    print("  · §8 有没有任何导出 API（没有的话场就得换一条路）")
    print(HDR)


if __name__ == "__main__":
    main()
