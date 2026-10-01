"""在 CST 里直接生成模板工程（Python + COM，**不经过 GUI 宏菜单**）。

为什么要绕开宏：CST 的模型是 **History List 重放**出来的。历史表为空
⇒ 存盘写出来的工程重开就是空工程（"打开一片空白"的根因）。而宏这条路
我们踩了两次坑：
  - `.mcr`（控制宏）跑完几何/端口都正常，但历史表为空——按定义控制宏
    的动作就不进历史表；
  - 换成 `.mcs`（结构宏）并从 Macros 菜单运行，仍报"遇到不适当的参数"
    且历史表依旧为空——宏跑在 CST 自己的 VBA 上下文里，出错只弹一个
    对话框，**我们看不到是哪条命令、什么错误**。

本脚本改走 COM，直接调用 CST 文档里的::

    AddToHistory(string header, string contents) -> bool

它**既执行 contents 里的命令、又把这条记进 History List**。从 Python 调用
的好处是能拿到每一步的返回值与**原始异常**（哪一块失败、错误号是什么），
不再靠猜。命令文本复用 eaopt/solver/vba.py 与 template_builder.template_blocks
——与 pipeline 改模型用的是同一套字符串，不会各写一份。

用法（服务器，CST 已启动）:
    python scripts/cst_build_template.py configs/coupler.yaml
    python scripts/cst_build_template.py configs/coupler.yaml --which fwd
    python scripts/cst_build_template.py configs/coupler.yaml --probe   # 只查 API

`--probe` 不建任何东西，只把 mws / app 上名字含 Add/History/Save 的成员
**连形参名**一起列出来（`cst_api.member_signatures` 读 IDispatch 类型库）
——万一 AddToHistory 在这个版本里叫别的名字、或参数顺序不同，那张表就是
答案。

若所有块都返回 False（与命令内容无关的信号），先跑
`python scripts/cst_probe_history.py`：它专门查这件事（签名 + 调用形状
矩阵 + 读回历史表）。

产物：按 cfg.solver.template_fwd / template_bwd 的路径写出两个 .cst
（连同名文件夹一起，见 cst_project）。
"""

import argparse
from pathlib import Path

from eaopt.solver import cst_api


def _short(exc: BaseException, limit: int = 200) -> str:
    s = str(exc).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def probe(app) -> None:
    """打印 app / mws 上可能与"建工程 + 写历史表 + 保存"有关的成员**签名**。

    带形参名（member_signatures 读的是 IDispatch 类型库）——调用形状存疑
    时以它为准，不用反复试。
    """
    print("=" * 60)
    print("COM 成员探针（不建任何东西）")
    print("=" * 60)
    for obj, label in ((app, "app"),):
        for kw in ("New", "Open", "Save", "Active", "Project"):
            try:
                sigs = cst_api.member_signatures(obj, kw)
            except Exception as e:
                print(f"    {label} 含 {kw!r}: 拿不到类型信息（{_short(e)}）")
                continue
            print(f"    {label} 含 {kw!r} 的成员（{len(sigs)}）：")
            for s in sigs:
                print(f"      {s['name']}({', '.join(s['params'])})")
    for factory, label in ((lambda: app.GetActiveProject(), "app.GetActiveProject()"),
                           (lambda: app.ActiveProject(), "app.ActiveProject()"),
                           (lambda: app.NewMWS(), "app.NewMWS()")):
        try:
            mws = factory()
        except Exception as e:
            print(f"    {label}: 调用失败（{_short(e)}）")
            continue
        print(f"    {label}: OK")
        for kw in ("History", "Add", "Save", "Reset", "Brick", "Port"):
            try:
                sigs = cst_api.member_signatures(mws, kw)
            except Exception as e:
                print(f"      mws 含 {kw!r}: 拿不到类型信息（{_short(e)}）")
                continue
            print(f"      mws 含 {kw!r} 的成员（{len(sigs)}）：")
            for s in sigs:
                print(f"        {s['name']}({', '.join(s['params'])})")
        break


def open_new_project(app, attach: bool = False):
    """取得要建模的工程对象。

    默认链：app.NewMWS() → 当前活动工程。都拿不到就抛——**不要**在用户
    已经打开的工程上乱建东西。

    attach=True：只用 GUI 里**当前活动**的工程（不发 NewMWS）。为什么要
    这个开关：`app.NewMWS()` 建出来的工程与 GUI 里 File → New 建的工程
    状态可能不同（实测 `AddToHistory` 一律返回 False 时，需要用它来分辨
    "是新建工程的问题"还是"调用形状的问题"）。
    """
    if attach:
        mws = app.GetActiveProject()
        print("[OK] 工程对象：app.GetActiveProject()（--attach：用 GUI 里"
              "当前打开的工程，请确认它就是你要建模板的那个空工程）")
        return mws
    for label, factory in (
        ("app.NewMWS()", lambda: app.NewMWS()),
        ("app.GetActiveProject()", lambda: app.GetActiveProject()),
    ):
        try:
            mws = factory()
            print(f"[OK] 工程对象：{label}")
            return mws
        except Exception as e:
            print(f"[FAIL] {label}: {_short(e)}")
    raise RuntimeError(
        "拿不到工程对象：请在 CST GUI 里 File → New 新建一个空工程后重试"
        "（或先跑 --probe 看看这台机器上的方法叫什么）")


def build(mws, project: str, portnum: int) -> bool:
    """逐块 AddToHistory：既执行、又写进 History List。返回是否全部成功。"""
    from eaopt.solver.template_builder import template_blocks

    ok_all = True
    n_false = 0
    for i, (header, cmd) in enumerate(template_blocks(project, portnum), 1):
        try:
            ok = mws.AddToHistory(header, cmd)
        except Exception as e:
            print(f"[FAIL] {i:2d} {header:<22} AddToHistory 抛错：{_short(e)}")
            if "AddToHistory" in str(e) or "unknown" in str(e).lower():
                print("       → 这个方法名可能不对：跑 --probe 看真实成员名")
            ok_all = False
            continue
        if not ok:
            # 文档：返回 False = 条目没建成，或 contents 没执行成功
            print(f"[FAIL] {i:2d} {header:<22} AddToHistory 返回 False"
                  f"（条目没建成或命令没执行）")
            ok_all = False
            n_false += 1
            continue
        print(f"[ OK ] {i:2d} {header:<22} 已执行并记入 History List")
    if n_false == i:              # 每一块都 False（i = 块数）
        # 全部失败 = 与命令内容无关（连单行、语法显然没问题的块也 False）：
        # 是条目创建本身被拒，或调用形状不对。别在这儿瞎猜——
        print("  !! 所有块都返回 False：这不是命令内容的问题（单行块也失败）。"
              "\n     跑 `python scripts/cst_probe_history.py`：它会打印"
              "AddToHistory 的真实形参名，\n     并把各种调用形状（含参数"
              "换序、CRLF、另存后再试）逐个试一遍。")
    return ok_all


def save_as(mws, path: Path) -> bool:
    """另存为模板（覆盖写）。两种参数个数都试——CST 2024 实测过 10097。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    target = str(path.resolve())
    for label, args in ((f'SaveAs(..., True)', (target, True)),
                        (f'SaveAs(..., "True")', (target, "True")),
                        (f'SaveAs(...)', (target,))):
        try:
            mws.SaveAs(*args)
            print(f"[OK] 已另存：{target}（{label}）")
            return True
        except Exception as e:
            print(f"[FAIL] {label}: {_short(e)}")
    print("!! 另存全失败——请在 GUI 里 File → Save As → "
          f"{target}")
    return False


def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(
        description="在 CST 里直接生成模板工程（Python + COM + AddToHistory）")
    ap.add_argument("config", nargs="?", default="configs/coupler.yaml")
    ap.add_argument("--which", choices=("fwd", "bwd", "both"), default="both",
                    help="只建其中一个模板（调试用）")
    ap.add_argument("--probe", action="store_true",
                    help="只列相关 COM 成员名，不建工程")
    ap.add_argument("--outdir", default=None,
                    help="输出目录（默认取配置里模板所在的目录）")
    ap.add_argument("--attach", action="store_true",
                    help="用 GUI 里当前打开的工程（不发 NewMWS）：当"
                         "AddToHistory 一律 False、要分辨是新建工程的问题"
                         "还是调用形状的问题时用（先在 GUI 里 File → New）")
    args = ap.parse_args()

    from eaopt.config import CaseConfig

    cfg = CaseConfig.from_yaml(args.config)
    app = cst_api.connect_app()
    if args.probe:
        probe(app)
        return

    targets = []
    if args.which in ("fwd", "both"):
        targets.append(("fwd", 1, Path(cfg.solver.template_fwd)))
    if args.which in ("bwd", "both"):
        targets.append(("bwd", 3, Path(cfg.solver.template_bwd)))
    if args.outdir:
        targets = [(t, p, Path(args.outdir) / p.name) for t, p, p in targets]

    print("=" * 60)
    print("逐块 AddToHistory（每一行 = 一条历史记录，模型靠它重放出来）")
    print("=" * 60)
    results = []
    for tag, portnum, path in targets:
        print(f"--- {tag}（激励端口 {portnum}）→ {path} ---")
        print("!! 会新建一个工程；CST 里当前未保存的东西请先存好")
        mws = open_new_project(app, attach=args.attach)
        ok = build(mws, tag, portnum)
        ok = save_as(mws, path) and ok
        results.append((tag, path, ok))
        print("")

    print("=" * 60)
    for tag, path, ok in results:
        print(f"  {tag}: {'成功' if ok else '**有失败**'}  {path}")
    print("""
接下来（必须做，这是判定成功的唯一标准）：
  1. 在 CST GUI 里看 **History List 是否非空**（应有 Brick/Extrude/Port…）；
  2. 关掉工程再重新打开那个 .cst，确认几何与 4 个端口还在
     （历史表为空的话这里就会变空——那才是问题所在）；
  3. python scripts/cst_inspect_template.py     # 文件层再确认一遍
  4. python scripts/cst_smoke.py configs/coupler.yaml
把上面所有输出贴回开发者。""")


if __name__ == "__main__":
    main()
