"""CST 服务器 smoke 测试：验证 COM 命令链并实测各后端。

用法（服务器，CST 2024 已启动或可启动）:
    python scripts/cst_smoke.py configs/coupler.yaml
    python scripts/cst_smoke.py configs/coupler.yaml --stim "1"   # 只试一种激励写法

依次验证并打印：
  1. COM 连接（Dispatch / GetActiveObject）；
  2. 打开模板副本；
  2b. 模板 .cst 里内嵌的激励相关字符串（看宏到底把什么存了进去）；
  3. 激励设置：先读回当前值，再逐个候选试到 Solver.Start 成功为止
     —— CST 2024 实测 `.StimulationPort "1"` + `.StimulationMode "All"`
     会让 Start 报 "Invalid stimulation port, please specify."，端口与
     模式必须成对（Dassault 教程：`.StimulationPort "1"` + `.StimulationMode "1"`）；
  4. S 参数读取：列结果树子条目 + 逐候选调用链
     （GetResultIDsFromTreeItem / GetResultFromTreeItem / GetArray）；
  5. 结果树中的监视器条目路径（SelectTreeItem 必须能选中）；
  6. E 场 ASCII 导出：属性探针 + 候选配置（打印文件头——用于核对
     ascii_fields.parse_ascii_field 与 CST 2024 真实格式）。

输出即诊断报告：把完整输出贴回给开发者。
"""

import argparse
import shutil
import time
from pathlib import Path

import numpy as np


def probe_project_bytes(path: Path) -> None:
    """打印 .cst 里内嵌的激励相关 ASCII 片段。

    CST 工程是复合二进制文件，设置/建模命令以文本内嵌（若整体压缩则
    找不到——那只是本节无效，不影响其它步骤）。
    """
    raw = path.read_bytes()
    print(f"    文件大小 {len(raw) / 1e6:.2f} MB")
    text = raw.decode("latin-1")
    found = False
    for key in ("StimulationPort", "StimulationMode", "Excitation",
                "FrequencyRange"):
        start = 0
        for _ in range(6):
            i = text.find(key, start)
            if i < 0:
                break
            snippet = "".join(c if 32 <= ord(c) < 127 else "."
                              for c in text[i:i + 60])
            print(f"    [{key}] {snippet}")
            found = True
            start = i + len(key)
    if not found:
        print("    未找到可读的激励相关字符串（文件可能整体压缩，本节无效）")


def main() -> None:
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
    workdir = Path(cfg.output.dir) / "cst_work"
    workdir.mkdir(parents=True, exist_ok=True)
    dst = workdir / f"smoke_{tpl.name}"
    shutil.copy(tpl, dst)
    mws = app.OpenFile(str(dst.resolve()))
    print(f"[OK] 已打开 {dst}")

    print("=" * 60)
    print("2b) 模板文件里内嵌的激励设置字符串")
    print("=" * 60)
    probe_project_bytes(dst)

    print("=" * 60)
    print("3) 激励设置（端口与模式必须成对；失败会在 Start 时立刻报错）")
    print("=" * 60)
    for label, getter in (
        ("Solver.StimulationPort", lambda: mws.Solver.StimulationPort),
        ("Solver.StimulationMode", lambda: mws.Solver.StimulationMode),
        ("Solver.Method", lambda: mws.Solver.Method),
        ("GetSolverType()", lambda: mws.GetSolverType()),
    ):
        try:
            print(f"    读回 {label} = {getter()!r}")
        except Exception as e:
            print(f"    读回 {label}: 失败 ({e})")

    candidates = ([(args.stim, "1")] if args.stim else [
        ("1", "1"),        # Dassault 教程写法（端口 1 + 模式 1）
        ("Port 1", "1"),   # 保险：万一 CST 用带前缀的端口名
        ("1", "All"),      # 模板当前写法（已知失败，再确认一次）
        ("All", "All"),    # 兜底：至少让工程跑起来（场是多端口叠加，仅供验证 API）
    ])
    print("    若 CST 界面弹出报错对话框，点掉即可，脚本会继续试下一个候选")
    winner = None
    for port, mode in candidates:
        try:
            mws.Solver.StimulationPort(port)
            mws.Solver.StimulationMode(mode)
            t0 = time.perf_counter()
            mws.Solver.Start()
            winner = (port, mode)
            print(f"[OK] StimulationPort={port!r} + StimulationMode={mode!r}"
                  f" → 求解成功，耗时 {time.perf_counter() - t0:.0f} s")
            break
        except Exception as e:
            print(f"[FAIL] StimulationPort={port!r} + StimulationMode={mode!r}: {e}")
    if winner is None:
        print("!! 所有候选都失败——请改用 GUI 手工设置并录宏：")
        print("   Simulation → Time Domain Solver → Source type 选端口 1 → OK")
        print("   Edit → History List → 选中刚出现的行 → 点 Macro 按钮")
        print("   → 把生成的含 Solver.xxx 的 VBA 贴回给开发者")
    else:
        port, mode = winner
        print(f'    >>> 可用写法：Solver.StimulationPort "{port}" + '
              f'Solver.StimulationMode "{mode}"')
        if port == "All":
            print("    !! 注意：这是兜底写法，场监视器里是四端口叠加场，"
                  "只能用来验证 API，不能用于伴随梯度")

    rt = mws.ResultTree
    p_in = cfg.objective.from_port

    def s_complex(p_to: int, freq_ghz: float):
        notes: list[str] = []
        v = cst_api.s_param_at(
            rt, f"1D Results\\S-Parameters\\S{p_to},{p_in}", freq_ghz, notes)
        return v, notes

    print("=" * 60)
    print("4) S 参数读取（结果树子条目 + 候选调用链）")
    print("=" * 60)
    kids = cst_api.tree_children(rt, "1D Results\\S-Parameters")
    print(f"    1D Results\\S-Parameters 子条目：{kids if kids else '（空/取不到）'}")
    for p_to in sorted({p.id for p in cfg.ports}):
        v, notes = s_complex(p_to, cfg.frequency)
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
        c, i = s_db(p_cpl, cfg.frequency), s_db(p_iso, cfg.frequency)
        if c is not None and i is not None:
            print(f"    -> 5 GHz: |S{p_cpl},{p_in}| = {c:.1f} dB, "
                  f"|S{p_iso},{p_in}| = {i:.1f} dB, 定向性 = {c - i:.1f} dB"
                  f"（论文初始设计约 4.6 dB，优化后约 17.1 dB）")

    e_path = V.field_result_path("Efield", cfg.frequency)
    h_path = V.field_result_path("Hfield", cfg.frequency)
    print("=" * 60)
    print("5) 结果树中监视器条目路径（供 SelectTreeItem 核对）")
    print("=" * 60)
    print(f"    期望的 E 场条目: {e_path}")
    print(f"    期望的 H 场条目: {h_path}")
    for folder in ("2D/3D Results\\E-Field", "2D/3D Results\\H-Field"):
        kids = cst_api.tree_children(rt, folder)
        print(f"    {folder} 子条目：{kids if kids else '（空/取不到）'}")
    for tag, p in (("E", e_path), ("H", h_path)):
        try:
            ok = mws.SelectTreeItem(p)
            print(f"    [OK] SelectTreeItem({tag}) -> {ok!r}")
        except Exception as e:
            print(f"    [FAIL] SelectTreeItem({tag}) {p}: {e}")

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
            mws.SelectTreeItem(e_path)
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
            print(f"    [FAIL] Mode={mode!r} + {exec_name}(): {e}")

    # 属性存在性探针：CST 2024 没有 XStart/XEnd/...（实测 <unknown>.XStart）
    print("    属性探针（能设进去的才是存在的属性）：")
    try:
        a = mws.ASCIIExport
        a.Reset()
        for prop in ("Mode", "StepX", "StepY", "StepZ", "FileName",
                     "XStart", "Xmin", "SubvolumeXmin", "UseSubvolume",
                     "SetPoints", "SetPointFile"):
            try:
                getattr(a, prop)("0.1")
                print(f"      {prop}: 可设置")
            except Exception:
                print(f"      {prop}: 不可设置（不存在/签名不符）")
        a.Reset()
    except Exception as e:
        print(f"      探针失败: {e}")

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
