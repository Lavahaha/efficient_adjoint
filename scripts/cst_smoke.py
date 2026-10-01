"""CST 服务器 smoke 测试：验证 COM 命令链并实测各后端。

用法（服务器，CST 2024 已启动或可启动）:
    python scripts/cst_smoke.py configs/coupler.yaml
    python scripts/cst_smoke.py configs/coupler.yaml --stim "1"   # 只试一种激励写法

依次验证并打印：
  1. COM 连接（Dispatch / GetActiveObject）；
  2. 打开模板副本（并打印模板大小/最后修改时间——旧时间戳说明宏的
     SaveAs 没覆盖掉旧文件）；
  2b. `.cst` 与**同名文件夹**里的命令字符串（看宏到底把什么存了进去）；
  3. 工程状态探针（`GetSolverType` / `Solver.GetNumberOfPorts`——CST 的
     GetXxx 多为**属性**，加括号调用会报 "'str' object is not callable"；
     `ObjectExists` 是宏宿主内部函数、不是 COM 成员，只能记录这条结论）；
  3b. 激励设置：逐个候选试到 Solver.Start 成功为止；
  3c. **COM 方法枚举**（问类型库要真实成员表，不再逐个猜 API 名字；含
     模型/几何查询那一组）；
  4. S 参数读取：先列结果树真实条目，再用解析到的**真实路径**逐候选读取
     （GetResultIDsFromTreeItem / GetResultFromTreeItem / GetArray），
     诊断原始错误一并打印；
  4b. S 参数曲线（4.5–5.5 GHz，对标论文初始设计）；
  4c. 备选链 GetFileFromTreeItem + Result1DComplex（不依赖结果树数组布局）；
  4d. 备选：Touchstone 导出（走文件，绕开结果树读取 API）；
  5. 结果树中的监视器条目路径（惯例路径 vs 实际匹配）+ SelectTreeItem；
  6. E 场 ASCII 导出：属性探针 + 候选配置 + 文件头（用于核对
     ascii_fields.parse_ascii_field 与 CST 2024 真实格式）。

输出即诊断报告：把完整输出贴回给开发者。
"""

import argparse
import time
from pathlib import Path

import numpy as np


def _short(exc: BaseException, limit: int = 140) -> str:
    """异常信息压成一行（pywin32 的错误元组很长）。"""
    s = str(exc).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def enum_com_methods(obj, label: str, keyword: str | None = None) -> None:
    """打印 COM 对象的真实成员名（读 IDispatch 类型库）。

    比逐个猜名字可靠得多：CST 各版本方法名有出入，这里直接问 COM 要
    成员表。keyword 非空时只打印名字含该串的成员（工程对象有几百个方法，
    全打印会淹掉输出）。
    """
    ole = getattr(obj, "_oleobj_", obj)     # 已包成 PyIDispatch 的也能用
    try:
        ti = ole.GetTypeInfo()
        ta = ti.GetTypeAttr()
        n = ta.cFuncs
    except Exception as e:
        print(f"    {label}: 拿不到类型信息（{_short(e)}）")
        return
    names: list[str] = []
    for i in range(n):
        try:
            fd = ti.GetFuncDesc(i)
            nm = ti.GetNames(fd.memid)[0]
        except Exception:
            continue
        if keyword and keyword.lower() not in nm.lower():
            continue
        names.append(nm)
    names = sorted(set(names))
    head = f"    {label}: {n} 个成员"
    if keyword:
        head += f"，名字含 {keyword!r} 的 {len(names)} 个"
    print(head)
    for nm in names:
        print("      " + nm)


# 在工程数据里搜这些命令串：搜到 => 这条命令真的执行过（宏里几何/端口段
# 报错就中止，不会"半执行"）。搜不到**不能**当反证——内容可能整体压缩。
_PROBE_KEYS = ("With Port", "PortNumber", "StimulationPort", "StimulationMode",
               "With Monitor", "Brick", "Extrude", "SaveAs", "Sub Main",
               "FrequencyRange", "With Solver")


def probe_project_bytes(path: Path) -> None:
    """在 `.cst` 与**同名文件夹**里搜命令字符串。

    上一轮实测：只看 `.cst`（0.04 MB，二进制读不动）什么也搜不到，而几何
    其实在同名文件夹里——所以两处都要搜。
    """
    from eaopt.solver import cst_project

    sub = cst_project.companion_dir(path)
    for target, label in ((path, ".cst"), (sub, f"{sub.name}/")):
        if target.is_file():
            print(f"    文件大小 {target.stat().st_size / 1e6:.2f} MB")
        elif not target.is_dir():
            continue
        print(f"    --- 在 {label} 里搜命令字符串 ---")
        rows = cst_project.grep_ascii(target, _PROBE_KEYS)
        for r in rows[:40]:
            print(f"      {r}")
        if len(rows) > 40:
            print(f"      （…共 {len(rows)} 条，只列前 40）")
        if not rows:
            print(f"      （搜不到可读的命令字符串：内容可能整体压缩，"
                  f"本节对 {label} 无效）")


def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(description="CST smoke 测试")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml")
    ap.add_argument("--template", default=None,
                    help="模板 .cst（默认取配置 template_fwd）")
    ap.add_argument("--stim", default=None,
                    help='只试这一种激励写法（如 "1" 或 "Port 1"），'
                         "跳过候选列表")
    args = ap.parse_args()

    from eaopt.config import CaseConfig
    from eaopt.solver import cst_api
    from eaopt.solver import vba as V

    cfg = CaseConfig.from_yaml(args.config)
    tpl = Path(args.template) if args.template else Path(cfg.solver.template_fwd)
    f0 = float(cfg.frequency)

    import win32com.client

    print("=" * 60)
    print("1) COM 连接")
    print("=" * 60)
    app = None
    for label, factory in (
        ("Dispatch", lambda: win32com.client.Dispatch("CSTStudio.Application")),
        ("GetActiveObject", lambda: win32com.client.GetActiveObject("CSTStudio.Application")),
    ):
        try:
            app = factory()
            print(f"[OK] {label} 成功")
            break
        except Exception as e:
            print(f"[FAIL] {label}: {e}")
    if app is None:
        print("无法连接 CST；请先启动 CST Studio GUI 后重试")
        return

    print("=" * 60)
    print("2) 打开模板副本")
    print("=" * 60)
    from eaopt.solver import cst_project
    try:                       # 检查只是诊断，失败也要继续往下走
        for line in cst_project.describe(tpl):
            print("    " + line)
    except Exception as e:
        print(f"    模板内容检查失败（继续跑其它步骤）: {_short(e)}")
    print("    （服务器实测：只复制 .cst 打开是空工程，连同名文件夹一起搬"
          "几何就出来了——所以模型可能在同名文件夹里，上面两处都查了）")
    workdir = Path(cfg.output.dir) / "cst_work"
    workdir.mkdir(parents=True, exist_ok=True)
    # **整份**复制（.cst + 同名文件夹）——只复制 .cst 会打开成空工程
    try:
        dst = cst_project.copy_project(tpl, workdir, dst_name=f"smoke_{tpl.name}")
    except Exception as e:
        print(f"[FAIL] 复制模板失败: {_short(e)}")
        return
    mws = app.OpenFile(str(dst.resolve()))
    print(f"[OK] 已打开 {dst}")

    print("=" * 60)
    print("2b) 模板文件里内嵌的激励设置字符串")
    print("=" * 60)
    probe_project_bytes(dst)

    print("=" * 60)
    print("3) 工程状态探针（CST 的 GetXxx 多是属性，不能加括号调用）")
    print("=" * 60)
    for label, expr in (
        ("mws.GetSolverType", lambda: mws.GetSolverType),
        ("Solver.GetNumberOfPorts", lambda: mws.Solver.GetNumberOfPorts),
        ("Solver.GetPortNames", lambda: mws.Solver.GetPortNames),
        # ObjectExists 是 CST **宏宿主内部**的函数，不是 COM 成员（实测
        # `<unknown>.ObjectExists`）——留着只是记录这条结论。
        ("ObjectExists('substrate')", lambda: mws.ObjectExists("substrate")),
    ):
        try:
            print(f"    {label} = {expr()!r}")
        except Exception as e:
            print(f"    {label}: 读不到 ({_short(e)})")
    n_ports = -1
    try:
        n_ports = int(mws.Solver.GetNumberOfPorts)
        if n_ports <= 0:
            print("    !! 端口数为 0 —— 打开的工程里**没有端口**（这条是 CST "
                  "自己报的，可信）")
        else:
            print(f"    >>> 工程里有 {n_ports} 个端口")
    except Exception as e:
        print(f"    !! 端口数读不到（{_short(e)}）—— 工程状态未知")
    empty = n_ports == 0
    if empty:
        print("    " + "!" * 56)
        print("    !! 端口数为 0 => 模板里没有端口，激励必然设不上、S 参数也")
        print("       不会有。几何在不在**本条判不出来**（COM 上没有查模型的")
        print("       成员），先看上一节 describe 的文件扫描结果 + 3c 的成员表。")
        print("       跳过求解（候选必然全失败，省几分钟）。")
        print("    " + "!" * 56)

    print("=" * 60)
    print("3b) 激励设置（失败会在 Start 时立刻报错，不耗时）")
    print("=" * 60)
    for label, getter in (
        ("Solver.StimulationPort", lambda: mws.Solver.StimulationPort),
        ("Solver.StimulationMode", lambda: mws.Solver.StimulationMode),
        ("Solver.Method", lambda: mws.Solver.Method),
    ):
        try:
            print(f"    {label} = {getter()!r}（bound method => 只写读不出）")
        except Exception as e:
            print(f"    {label}: 读不到 ({e})")

    candidates = ([(args.stim, "1")] if args.stim else [
        ("1", "1"),        # Dassault 教程写法（端口 1 + 模式 1）
        ("Port 1", "1"),   # 已知报「positive integer or All」→ 证明要的是数字
        (1, 1),            # 万一要的是整数类型而不是字符串
        ("1", "All"),      # 模板早先的写法（已知失败）
        ("All", "All"),    # 兜底：至少让工程跑起来（场是多端口叠加，仅供验证 API）
    ])
    print("    若 CST 界面弹出报错对话框，点掉即可，脚本会继续试下一个候选")
    winner = None
    if empty:
        print("    （工程是空的，跳过求解——候选必然全失败，省几分钟）")
    for port, mode in ([] if empty else candidates):
        try:
            mws.Solver.StimulationPort(port)
        except Exception as e:
            print(f"[FAIL] StimulationPort({port!r}): {_short(e)}")
            continue
        try:
            mws.Solver.StimulationMode(mode)
        except Exception as e:
            print(f"[FAIL] StimulationMode({mode!r})（端口 {port!r} 已设）: "
                  f"{_short(e)}")
            continue
        try:
            t0 = time.perf_counter()
            mws.Solver.Start()
        except Exception as e:
            print(f"[FAIL] Start()（端口 {port!r} 模式 {mode!r}）: {_short(e)}")
            continue
        winner = (port, mode)
        print(f"[OK] StimulationPort={port!r} + StimulationMode={mode!r}"
              f" → 求解成功，耗时 {time.perf_counter() - t0:.0f} s")
        break
    if winner is None and not empty:
        print("!! 所有候选都失败——请改用 GUI 手工设置并录宏：")
        print("   Simulation → Time Domain Solver → Source type 选端口 1 → OK")
        print("   Edit → History List → 选中刚出现的行 → 点 Macro 按钮")
        print("   → 把生成的含 Solver.xxx 的 VBA 贴回给开发者")
    elif winner is not None:
        port, mode = winner
        print(f'    >>> 可用写法：Solver.StimulationPort "{port}" + '
              f'Solver.StimulationMode "{mode}"')
        if port == "All":
            print("    !! 注意：这是兜底写法，场监视器里是四端口叠加场，"
                  "只能用来验证 API，不能用于伴随梯度")

    print("=" * 60)
    print("3c) COM 方法枚举（直接问类型库，不再猜 API 名字）")
    print("=" * 60)
    rt = mws.ResultTree
    for kw in ("Result", "Export", "Tree", "Field", "ASCII", "Touch"):
        enum_com_methods(mws, "mws", keyword=kw)
    enum_com_methods(rt, "mws.ResultTree")
    enum_com_methods(mws.ASCIIExport, "mws.ASCIIExport")
    enum_com_methods(mws.Solver, "mws.Solver")
    enum_com_methods(mws.Solver, "mws.Solver", keyword="Stimulation")
    # 模型（几何/端口）查询：`ObjectExists` 不是 COM 成员，所以"工程里
    # 有没有几何"目前只能靠这里问出来的**真名**——不再猜。
    # "History" 一组是给 pipeline 探路的：COM 侧改模型必须用
    # AddToHistory（既执行、又写进 History List），否则历史表重放会把
    # 旧的 design_region 复活（见 cst.py::_rebuild_design 的说明）。
    for kw in ("Object", "Solid", "Model", "Shape", "Component",
               "Brick", "Port", "Count", "Number", "History", "Script"):
        enum_com_methods(mws, "mws", keyword=kw)
    for attr in ("Model", "Model3D", "Objects", "Ports", "Component"):
        try:
            enum_com_methods(getattr(mws, attr), f"mws.{attr}")
        except Exception as e:
            print(f"    mws.{attr}: 拿不到（{_short(e)}）")

    p_in = cfg.objective.from_port

    def resolve_sparam(i_to: int):
        """S 参数条目的真实路径（先按前缀在结果树里找）。"""
        folder = "1D Results\\S-Parameters"
        notes: list[str] = []
        got = cst_api.find_item(rt, folder, f"S{i_to},{p_in}", notes)
        return (got or f"{folder}\\S{i_to},{p_in}"), notes

    def s_complex(p_to: int, freq_ghz: float):
        path, notes = resolve_sparam(p_to)
        notes.append(f"path={path!r}")
        v = cst_api.s_param_at(rt, path, freq_ghz, notes, project=mws)
        return v, notes

    print("=" * 60)
    print("4) S 参数读取（结果树真实条目 + 候选调用链）")
    print("=" * 60)
    for folder in ("1D Results", "1D Results\\S-Parameters"):
        notes: list[str] = []
        kids = cst_api.tree_children(rt, folder, notes)
        print(f"    {folder} 子条目：{kids if kids else '（空/取不到）'}")
        for n in notes:
            print(f"      [诊断] {n}")
    for p_to in sorted({p.id for p in cfg.ports}):
        v, notes = s_complex(p_to, f0)
        if v is None:
            print(f"    S{p_to},{p_in}: n/a   [{' | '.join(notes)}]")
        else:
            print(f"    S{p_to},{p_in}: |S|={abs(v):.4g} = "
                  f"{20 * np.log10(abs(v)):6.1f} dB   [{' | '.join(notes)}]")

    def s_db(p_to: int, freq_ghz: float):
        v, _ = s_complex(p_to, freq_ghz)
        if v is None or abs(v) == 0:
            return None
        return 20 * np.log10(abs(v))

    print("=" * 60)
    print("4b) S 参数曲线（对标论文初始设计：5 GHz 处 |S31| 约 -17.9 dB、"
          "定向性约 4.6 dB）")
    print("=" * 60)
    for p_to in sorted({p.id for p in cfg.ports}):
        row = [f"S{p_to},{p_in}:"]
        for f in (4.5, 4.75, 5.0, 5.25, 5.5):
            v = s_db(p_to, f)
            row.append(f"{f:g}GHz " + ("  n/a  " if v is None else f"{v:7.1f}dB"))
        print("    " + "  ".join(row))
    roles = {p.role: p.id for p in cfg.ports}
    p_cpl, p_iso = roles.get("observation"), roles.get("auxiliary")
    if p_cpl and p_iso:
        c, i = s_db(p_cpl, f0), s_db(p_iso, f0)
        if c is not None and i is not None:
            print(f"    -> 5 GHz: |S{p_cpl},{p_in}| = {c:.1f} dB, "
                  f"|S{p_iso},{p_in}| = {i:.1f} dB, 定向性 = {c - i:.1f} dB"
                  f"（论文初始设计约 4.6 dB，优化后约 17.1 dB）")

    print("=" * 60)
    print("4c) 备选链：GetFileFromTreeItem + Result1DComplex")
    print("=" * 60)
    path, _ = resolve_sparam(p_in)
    try:
        fpath = rt.GetFileFromTreeItem(path)
        print(f"    [OK] GetFileFromTreeItem({path!r}) -> {fpath!r}")
    except Exception as e:
        fpath = None
        print(f"    [FAIL] GetFileFromTreeItem({path!r}): {_short(e)}")
    if fpath is not None:
        for label, factory in (("mws.Result1DComplex",
                                lambda: mws.Result1DComplex(fpath)),
                               ("mws.Result1D", lambda: mws.Result1D(fpath))):
            try:
                obj = factory()
                print(f"    [OK] {label}(file) -> {obj!r}")
            except Exception as e:
                print(f"    [FAIL] {label}(file): {_short(e)}")
                continue
            for meth in ("GetN", "GetClosestIndexFromX", "GetX", "GetY",
                         "GetYImag", "GetYReal", "GetYPhase"):
                try:
                    print(f"      {meth}() -> {getattr(obj, meth)()!r}")
                except Exception:
                    try:
                        print(f"      {meth}(0) -> {getattr(obj, meth)(0)!r}")
                    except Exception as e2:
                        print(f"      {meth}: 不可用 ({_short(e2)})")
            break

    print("=" * 60)
    print("4d) 备选：Touchstone 导出（走文件，绕开结果树读取 API）")
    print("=" * 60)
    tsp = workdir / "smoke_sparams.s4p"
    try:
        enum_com_methods(mws.TOUCHSTONE, "mws.TOUCHSTONE")
    except Exception as e:
        print(f"    mws.TOUCHSTONE 取不到: {_short(e)}")
    for term in ("Write", "Export", "Execute"):
        try:
            t = mws.TOUCHSTONE
            t.Reset()
            t.FileName(str(tsp))
            t.Impedance("50")
            for meth in ("FrequencyRange", "SetNSamples"):
                for call in ((str(0.0), str(10.0)), ("1001",)):
                    try:
                        getattr(t, meth)(*call)
                        break
                    except Exception:
                        continue
            getattr(t, term)()
            print(f"    [OK] TOUCHSTONE.{term}(): "
                  f"{tsp.stat().st_size / 1e3:.1f} kB -> {tsp}")
            for ln in tsp.read_text(encoding="utf-8",
                                    errors="replace").splitlines()[:12]:
                print("      " + ln)
            break
        except Exception as e:
            print(f"    [FAIL] TOUCHSTONE.{term}(): {_short(e)}")

    print("=" * 60)
    print("5) 结果树中监视器条目的真实路径（供 SelectTreeItem 核对）")
    print("=" * 60)
    notes5: list[str] = []
    for n in cst_api.tree_children(rt, "2D/3D Results", notes5):
        print(f"    2D/3D Results 子条目：{n}")
    for n in notes5:
        print(f"      [诊断] {n}")
    items: dict[str, str] = {}
    for ftype in V.FIELD_TYPES:
        folder = f"2D/3D Results\\{V.FIELD_TYPES[ftype][1]}"
        notes: list[str] = []
        kids = cst_api.tree_children(rt, folder, notes)
        real = cst_api.find_item(rt, folder, V.field_monitor_name(ftype, f0), notes)
        conventional = V.field_result_path(ftype, f0)
        print(f"    {folder} 子条目：{kids if kids else '（空/取不到）'}")
        for n in notes:
            print(f"      [诊断] {n}")
        print(f"      惯例路径: {conventional!r}")
        print(f"      实际匹配: {real!r}")
        items[ftype] = real or conventional
    for tag, p in items.items():
        try:
            ok = mws.SelectTreeItem(p)
            print(f"    [OK] SelectTreeItem({tag}) {p!r} -> {ok!r}")
        except Exception as e:
            print(f"    [FAIL] SelectTreeItem({tag}) {p!r}: {_short(e)}")

    print("=" * 60)
    print("6) E 场 ASCII 导出")
    print("=" * 60)
    dx = cfg.design_region.grid_step_mm
    path = workdir / "smoke_efield.txt"
    exported = False
    for mode, exec_name in (("FixedNumber", V.ASCII_EXPORT_EXECUTE),
                            ("FixedWidth", V.ASCII_EXPORT_EXECUTE),
                            ("FixedNumber", "Export")):
        try:
            mws.SelectTreeItem(items["Efield"])
            a = mws.ASCIIExport
            a.Reset()
            a.FileName(str(path))
            for prop, val in V.ascii_export_params(dx, mode=mode):
                getattr(a, prop)(val)
            getattr(a, exec_name)()
            print(f"    [OK] Mode={mode!r} + {exec_name}(): "
                  f"{path.stat().st_size / 1e6:.2f} MB -> {path}")
            exported = True
            break
        except Exception as e:
            print(f"    [FAIL] Mode={mode!r} + {exec_name}(): {_short(e)}")

    # 属性存在性探针：CST 2024 没有 XStart/XEnd/...（实测 <unknown>.XStart）。
    # 注意：探针给的值是 "0.1"，对**会校验取值**的属性（如 Mode）会得到
    # "值被拒" 而不是 "属性不存在"——所以这里打印原始报错，别只看成败。
    print("    属性探针（给值 0.1，打印原始报错）：")
    try:
        a = mws.ASCIIExport
        a.Reset()
        for prop in ("Mode", "StepX", "StepY", "StepZ", "FileName",
                     "XStart", "Xmin", "SubvolumeXmin", "UseSubvolume",
                     "SetPoints", "SetPointFile"):
            try:
                getattr(a, prop)("0.1")
                print(f"      {prop}: 可设置")
            except Exception as e:
                print(f"      {prop}: 失败 -> {_short(e)}")
        a.Reset()
    except Exception as e:
        print(f"      探针失败: {_short(e)}")

    if exported and path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"    导出文件共 {len(lines)} 行；头 20 行：")
        for ln in lines[:20]:
            print("      " + ln)
        print("    数据行样例（第 21-24 行）：")
        for ln in lines[20:24]:
            print("      " + ln)

    try:
        mws.Save()
    except Exception:
        pass
    try:
        app.Quit()
    except Exception:
        pass
    print("=" * 60)
    print("smoke 结束：请把以上全部输出贴回给开发者")
    print("=" * 60)


if __name__ == "__main__":
    main()
