"""CST 服务器 smoke 测试：验证 COM 命令链并实测各后端。

用法（服务器，CST 2024 已启动或可启动）:
    python scripts/cst_smoke.py configs/coupler.yaml [--template coupler_fwd.cst]

依次验证并打印结果：
  1. COM 连接（Dispatch / GetActiveObject）；
  2. 打开模板副本；
  3. 时域求解；
  4. S 参数读取（逐候选方法打印哪个可用，并给出 5 GHz 处 S31/S21/S11）；
  5. E 场 ASCII 导出（打印耗时、导出文件头部 20 行——用于核对
     ascii_fields.parse_ascii_field 与 CST 2024 真实格式）；
  6. 打印结果树中 E/H 监视器条目的实际路径（供 SelectTreeItem 核对）。

输出即诊断报告：把完整输出贴回给开发者，即可收敛 CstSolver 的
候选 API 列表与场解析器。
"""

import argparse
import shutil
import time
from pathlib import Path

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser(description="CST smoke 测试")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml")
    ap.add_argument("--template", default=None,
                    help="模板 .cst（默认取配置 template_fwd）")
    args = ap.parse_args()

    from eaopt.config import CaseConfig

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
    print("3) 时域求解")
    print("=" * 60)
    t0 = time.perf_counter()
    try:
        mws.Solver.Start()
        print(f"[OK] 求解完成，耗时 {time.perf_counter() - t0:.1f} s")
    except Exception as e:
        print(f"[FAIL] Solver.Start: {e}")

    print("=" * 60)
    print("4) S 参数读取（候选方法逐个试）")
    print("=" * 60)
    for i in (1, 2, 3):
        name = f"1D Results\\S-Parameters\\S{i},1"
        try:
            item = mws.ResultTree.GetResultItem(name)
            print(f"[OK] GetResultItem({name!r})")
            for meth, args in (("GetValueAtFrequency", (5.0,)),
                               ("GetComplexValueAtFrequency", (5.0,))):
                try:
                    v = getattr(item, meth)(*args)
                    print(f"     {meth}: 可用 -> {v}")
                except Exception as e:
                    print(f"     {meth}: 不可用 ({type(e).__name__})")
            try:
                v = item.GetYData()
                arr = np.asarray(v)
                print(f"     GetYData: 可用 -> shape={arr.shape}, 前3={arr.ravel()[:3]}")
            except Exception as e:
                print(f"     GetYData: 不可用 ({type(e).__name__})")
        except Exception as e:
            print(f"[FAIL] GetResultItem({name!r}): {e}")

    print("=" * 60)
    print("5) 结果树中监视器条目路径（供 SelectTreeItem 核对）")
    print("=" * 60)
    try:
        items = mws.ResultTree.GetAllItems()
        n = items.GetCount()
        print(f"    结果树条目数: {n}")
        for k in range(min(n, 30)):
            try:
                print(f"    {items.GetItem(k).GetName()}")
            except Exception:
                pass
    except Exception as e:
        print(f"[FAIL] GetAllItems: {e}")

    print("=" * 60)
    print("6) E 场 ASCII 导出")
    print("=" * 60)
    dr = cfg.design_region
    box = dr.box
    m = dr.field_margin_mm
    dx = dr.grid_step_mm
    t0 = time.perf_counter()
    try:
        mws.SelectTreeItem("2D/3D Results\\E-Field\\e-field (f=5) [AC]")
        a = mws.ASCIIExport
        a.Reset()
        path = workdir / "smoke_efield.txt"
        a.FileName(str(path))
        a.Mode("FixedNumber")
        for prop, val in (("StepX", dx), ("StepY", dx), ("StepZ", dx),
                          ("XStart", box.x[0] - m), ("XEnd", box.x[1] + m),
                          ("YStart", box.y[0] - m), ("YEnd", box.y[1] + m),
                          ("ZStart", -0.1), ("ZEnd", 0.1)):
            getattr(a, prop)(str(val))
        a.Export()
        print(f"[OK] 导出完成 {time.perf_counter() - t0:.1f} s -> {path}")
        head = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:20])
        print("---- 文件头 20 行 ----")
        print(head)
        print("----------------------")
    except Exception as e:
        print(f"[FAIL] ASCII 场导出: {e}")

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
