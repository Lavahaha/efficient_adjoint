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

关键一步是**工程对象从哪儿来**（`cst_api.get_project`）：CST 官方例程
（C#/MATLAB）建完工程后一律用 `app.Active3D()` 取回活动工程再操作，而不是
用新建函数的返回值。CST 2024 实测：`GetActiveProject` / `ActiveProject`
**都不存在**（pywin32: `AttributeError ... Did you mean: 'CloseProject'?`），
`Active3D` 才是名字；且 `AddToHistory` 在晚绑定下返回 **None**（拿不到
返回值），所以"返回值"不能当成功判据。

`--probe` 不建任何东西，只挨个试每个取工程对象的候选方法名、把 app / 工程
对象上名字含 Add/History/Save 的成员**连形参名**一起列出来
（`cst_api.member_signatures` 读 IDispatch 类型库）——万一方法在这个版本
里叫别的名字、或参数顺序不同，那张表就是答案。

若所有块都返回 False（与命令内容无关的信号），先跑
`python scripts/cst_probe_history.py`：它专门查这件事（签名 + 调用形状
矩阵 + 读回历史表）。

产物：按 cfg.solver.template_fwd / template_bwd 的路径写出两个 .cst
（连同名文件夹一起，见 cst_project），**存完立刻用 cst_project.describe
做文件层检查**——两个"成功"（返回不报错 + SaveAs 不报错）仍可能存出空
工程，只有文件层面搜到对象名才算数。
"""

import argparse
from pathlib import Path

from eaopt.solver import cst_api


def _short(exc: BaseException, limit: int = 200) -> str:
    s = str(exc).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _dump_members(obj, label: str, keywords) -> None:
    """打印 obj 上名字含 keywords 的成员**签名**（带形参名）。

    读的是 IDispatch 类型库——调用形状存疑时以它为准，不用反复试。
    拿不到类型信息（晚绑定对象常见）就打印原始错误，好与"成员真的不存在"
    区分开。
    """
    for kw in keywords:
        try:
            sigs = cst_api.member_signatures(obj, kw)
        except Exception as e:
            print(f"    {label} 含 {kw!r}: 拿不到类型信息（{_short(e)}）")
            continue
        print(f"    {label} 含 {kw!r} 的成员（{len(sigs)}）：")
        for s in sigs:
            print(f"      {s['name']}({', '.join(s['params'])})")


def probe(app) -> None:
    """列出与"建工程 / 拿活动工程 / 写历史表 / 保存"有关的成员签名。

    **不建任何东西**：只挨个试每个候选方法名、打印成功与否（`repr` 原始
    异常），再对拿到手的工程对象打一遍成员签名。目的就是把这台机器上
    "取工程对象该用哪个方法"钉死——CST 2024 实测 `GetActiveProject` /
    `ActiveProject` 都不存在，官方例程用的是 `Active3D`。
    """
    print("=" * 60)
    print("COM 成员探针（不建任何东西）")
    print("=" * 60)
    _dump_members(app, "app", ("New", "Open", "Save", "Active", "Project",
                               "Studio"))
    print("  --- 逐个试「取工程对象」的候选方法名 ---")
    for group, names in (("活动工程", cst_api.ACTIVE_PROJECT_METHODS),
                         ("新建工程", cst_api.CREATE_PROJECT_METHODS)):
        for name in names:
            try:
                method = getattr(app, name)
            except Exception as e:
                print(f"    app.{name}(): **不存在**（{_short(e)}）")
                continue
            try:
                mws = method()
            except Exception as e:
                print(f"    app.{name}(): 调用报错（{_short(e)}）")
                continue
            print(f"    app.{name}(): OK → {mws!r}   （{group}）")
            _dump_members(mws, f"app.{name}()", ("History", "Add", "Save",
                                                 "Reset", "Brick", "Port"))
            try:
                cst_api.member_signatures(mws, "AddToHistory")
            except Exception as e:
                print(f"      注意：这个对象读不到类型信息（{_short(e)}）"
                      f"——早绑定/晚绑定可能给了不同类型库")


def build(mws, project: str, portnum: int) -> bool:
    """逐块 AddToHistory：既执行、又写进 History List。返回是否全部成功。

    **不要只看返回值**：晚绑定时 `AddToHistory` 返回 None（拿不到返回值），
    返回值与"命令有没有真的执行"并不等价。所以每一块都打印原始返回值
    （`repr`），最终判定以**模型树 / History List / 存盘文件的文件层检查**
    为准（main 会在另存后自动跑一遍文件层检查）。
    """
    from eaopt.solver.template_builder import template_blocks

    ok_all = True
    values = []
    for i, (header, cmd) in enumerate(template_blocks(project, portnum), 1):
        try:
            ok = mws.AddToHistory(header, cmd)
        except Exception as e:
            print(f"[FAIL] {i:2d} {header:<22} AddToHistory 抛错：{_short(e)}")
            if "AddToHistory" in str(e) or "unknown" in str(e).lower():
                print("       → 这个方法名可能不对：跑 --probe 看真实成员名")
            ok_all = False
            continue
        values.append(ok)
        if ok is None:
            print(f"[ ?? ] {i:2d} {header:<22} AddToHistory 返回 None"
                  f"（晚绑定拿不到返回值，不代表失败——看模型树/History List）")
        elif ok:
            print(f"[ OK ] {i:2d} {header:<22} 已执行并记入 History List")
        else:
            print(f"[FAIL] {i:2d} {header:<22} AddToHistory 返回 False"
                  f"（条目没建成或命令没执行）")
            ok_all = False
    if values and all(v is False for v in values):
        # 每一块都明确 False = 与命令内容无关（连单行块也 False）
        print("  !! 每一块都返回 False：不是命令内容的问题（单行块也失败）。"
              "\n     跑 `python scripts/cst_probe_history.py`。")
    elif values and all(v is None for v in values):
        print("  !! 每一块都返回 None：晚绑定拿不到返回值。**真正要看的是**"
              "\n     CST 里的模型树 / History List，以及本脚本结尾的文件层"
              "检查结果。")
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


def check_written(path: Path) -> bool:
    """存完立刻在**文件层面**查一遍：这个 .cst 里到底有没有模型。

    为什么必须查：`AddToHistory` 在晚绑定下返回 None，`SaveAs` 也不报错
    ——**两个"成功"加起来仍可能存出一个空工程**（我们的老毛病）。只有
    cst_project.describe（不用 CST，直接读文件/同名文件夹）能证明模型
    真的写进去了。判定行以 "=>" 开头。
    """
    from eaopt.solver import cst_project

    print(f"--- 文件层检查：{path} ---")
    lines = cst_project.describe(path)
    for ln in lines:
        print("   " + ln)
    verdict = next((ln for ln in lines if "=>" in ln), "")
    ok = "模型数据在" in verdict or "模型在同名文件夹里" in verdict
    print("   " + ("[ OK ] 文件里有模型" if ok else
                   "[FAIL] 文件里找不到模型——**这个模板是空的**，"
                   "别急着往下走"))
    return ok


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
                    help="不新建工程，直接往 GUI 里当前打开的那个活动工程里"
                         "建（默认是 NewMWS/FileNew 之后从活动工程取对象）")
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
        if not args.attach:
            print("!! 会新建一个工程；CST 里当前未保存的东西请先存好")
        mws = cst_api.get_project(app, attach=args.attach)
        ok = build(mws, tag, portnum)
        ok = save_as(mws, path) and ok
        # 判定成功的唯一标准是**文件层面**有没有模型：返回值靠不住
        # （晚绑定给 None），模型树要人眼看。存完立刻自动查一遍。
        ok = check_written(path) and ok
        results.append((tag, path, ok))
        print("")

    print("=" * 60)
    for tag, path, ok in results:
        print(f"  {tag}: {'文件层检查通过' if ok else '**有失败**'}  {path}")
    print("""
接下来（必须做）：
  1. 在 CST GUI 里看 **History List 是否非空**（应有 Brick/Extrude/Port…），
     并确认模型树里有 substrate / design_region / 4 个端口；
  2. 关掉工程再重新打开那个 .cst，确认几何与 4 个端口还在
     （历史表为空的话这里就会变空——那才是问题所在）；
  3. python scripts/cst_inspect_template.py     # 文件层再确认一遍
  4. python scripts/cst_smoke.py configs/coupler.yaml
把上面所有输出贴回开发者。""")


if __name__ == "__main__":
    main()
