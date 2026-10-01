"""查清 AddToHistory 为什么返回 False（一次性诊断，不改任何工程文件）。

背景：`scripts/cst_build_template.py` 逐块调用
``mws.AddToHistory(标题, 命令文本)``，实测**18 块全部返回 False**
（包括只有一行、语法显然没问题的 `Solver.FrequencyRange "0", "10"`）。
按 CST 文档，False = "条目没建成 **或** contents 没执行成功"，光看返回值
分不清是哪一种——这个脚本把两种可能一次问清楚。

它做四件事（**都不写盘、不保存**）：

1. **要签名**：从 IDispatch 类型库读 `AddToHistory` 的**形参名与个数**
   （`cst_api.member_signatures`）——到底是 (header, contents) 还是
   (contents, header)、要 2 个还是 1 个参数，这是权威答案，不用猜；
   顺带列出工程对象上所有名字含 history 的成员（读回历史表要用）。
2. **打矩阵**：在新建工程上把各种调用形状逐个试一遍（空内容 / 单条语句 /
   LF 多行 / CRLF 多行 / 参数换序 / 带分号 / 只给一个参数），看**哪一种
   返回 True**——这一步直接给出可用的调用形状。
3. **换状态再试**：同一次会话里，先在 GUI 里手工建一个空工程（File →
   New）再跑本脚本的话，会对**当前活动工程**也打一遍矩阵（新建工程可能
   与 GUI 工程状态不同）；另外在 SaveAs 到临时文件**之后**再试关键几条
   （怀疑"未存盘的工程不允许写历史"）。
4. **读回**：调用工程对象上所有无参且名字含 history 的成员，把结果打出来
   （历史表条目数 / 内容），与矩阵结果对照。

用法（服务器，CST GUI 已启动）：
    python scripts/cst_probe_history.py
    python scripts/cst_probe_history.py --config configs/coupler.yaml  # 只用来定位临时目录

**最佳做法**：先在 CST 里手工画一个 Brick（或改一下单位）并保存，确认
GUI 自己的操作**能**进 History List（这是"记录功能本身是好的"的对照），
再跑本脚本，把完整输出贴回开发者。
"""

import argparse
import tempfile
from pathlib import Path

from eaopt.solver import cst_api


def _short(exc: BaseException, limit: int = 300) -> str:
    s = str(exc).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


# ---- 矩阵：调用形状假设 ----
# (标签, 传给 AddToHistory 的位置参数)
CASES = (
    ("A 空内容（只建条目，不执行任何东西）", ("probe-empty", "")),
    ("B 单条语句", ("probe-one", "Brick.Reset")),
    ("C 单条带分号", ("probe-semi", 'Brick.Reset;')),
    ("D 多行 LF", ("probe-lf", 'With Brick\n    .Reset\nEnd With')),
    ("E 多行 CRLF", ("probe-crlf", 'With Brick\r\n    .Reset\r\nEnd With')),
    ("F 多行 CRLF + 末尾换行",
     ("probe-crlf2", 'With Brick\r\n    .Reset\r\nEnd With\r\n')),
    ("G 参数换序（命令在前）", ("Brick.Reset", "probe-swap")),
    ("H 只给一个参数（标题）", ("probe-onearg",)),
)


def show_signatures(mws) -> None:
    print("=" * 64)
    print("1. AddToHistory 的真实签名（形参名来自 IDispatch 类型库）")
    print("=" * 64)
    try:
        sigs = cst_api.member_signatures(mws, "history")
    except Exception as e:
        print(f"  [FAIL] 拿不到类型信息：{_short(e)}")
        return
    if not sigs:
        print("  [!!] 工程对象上没有任何名字含 'history' 的成员"
              "——AddToHistory 不是这个对象的方法，需要改用别的对象/写法")
        return
    for s in sigs:
        print(f"  {s['name']}  参数 {s['n_params']} 个：{list(s['params'])}")
        if s["help"]:
            print(f"      帮助：{s['help']}")


def show_modeler_members(mws) -> None:
    """有没有独立的"建模器/3D 模型"对象？

    官方 CST Python 包里写历史用的是 ``project.model3d.add_to_history(...)``
    ——即 add_to_history 挂在 **Model3D/建模器**对象上，不一定是工程对象。
    这里把候选成员列出来；若真拿到对象，就顺手在那个对象上也打一遍矩阵。
    """
    print("-" * 64)
    print("1b. 名字含 model/design 的成员（找独立建模器对象）")
    print("-" * 64)
    try:
        sigs = cst_api.member_signatures(mws, "model")
    except Exception as e:
        print(f"  [FAIL] 拿不到类型信息：{_short(e)}")
        return
    if not sigs:
        print("  （没有名字含 model 的成员）")
    for s in sigs:
        print(f"  {s['name']}  参数 {s['n_params']} 个：{list(s['params'])}")
    # 无参、且名字像"取建模器"的，直接调用看返回什么
    for s in sigs:
        if s["n_params"] != 0:
            continue
        try:
            obj = getattr(mws, s["name"])()
        except Exception as e:
            print(f"    [ -- ] {s['name']}() 调用失败：{_short(e)}")
            continue
        print(f"    [ OK ] {s['name']}() -> {obj!r}")
        try:
            hist = cst_api.member_signatures(obj, "history")
        except Exception as e:
            print(f"           该对象拿不到类型信息：{_short(e)}")
            continue
        if hist:
            print(f"           **该对象上有 history 成员**："
                  f"{[h['name'] for h in hist]}")
            run_matrix(obj, f"建模器对象 {s['name']}()")


def run_matrix(mws, title: str) -> list[str]:
    """把 CASES 逐个试一遍，返回返回 True 的标签列表。"""
    print("-" * 64)
    print(f"2. 调用形状矩阵：{title}")
    print("-" * 64)
    good: list[str] = []
    for label, args in CASES:
        try:
            ok = mws.AddToHistory(*args)
        except Exception as e:
            print(f"  [FAIL] {label}: 抛异常 {_short(e)}")
            continue
        mark = "[ OK ]" if ok else "[FAIL]"
        shown = " / ".join(repr(a) for a in args)
        print(f"  {mark} {label}: AddToHistory({shown}) -> {ok}")
        if ok:
            good.append(label)
    if good:
        print(f"  => 返回 True 的形状：{good}")
    else:
        print("  => 全部返回 False：**条目创建本身被拒**（与命令内容无关）")
    return good


def read_back(mws) -> None:
    """调用所有无参且名字含 history 的成员，尝试读出历史表内容。"""
    print("-" * 64)
    print("3. 读回历史表（无参且名字含 history 的成员全试一遍）")
    print("-" * 64)
    try:
        sigs = cst_api.member_signatures(mws, "history")
    except Exception as e:
        print(f"  [FAIL] 拿不到类型信息：{_short(e)}")
        return
    tried = 0
    for s in sigs:
        if s["n_params"] != 0 or s["name"].startswith(("Add", "Delete",
                                                       "Reset", "Set")):
            continue
        tried += 1
        try:
            val = getattr(mws, s["name"])()      # 属性也可能是无参的
        except Exception:
            try:
                val = getattr(mws, s["name"])    # 真·属性（不带括号）
            except Exception as e:
                print(f"  [FAIL] {s['name']}(): {_short(e)}")
                continue
        text = repr(val)
        print(f"  [ OK ] {s['name']}() = "
              f"{text if len(text) <= 400 else text[:400] + '…'}")
    if not tried:
        print("  （没有可无参调用的 history 成员）")


def state_matrix(mws, app, cfg_path: str | None) -> None:
    """换个工程状态再打矩阵：先另存到临时文件，再试关键几条。"""
    tmp = Path(tempfile.gettempdir()) / "cst_probe_history"
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "probe.cst"
    print("-" * 64)
    print(f"4. 另存之后再试（怀疑'未存盘的工程不许写历史'）-> {target}")
    print("-" * 64)
    try:
        mws.SaveAs(str(target), True)
        print("[ OK ] SaveAs 成功")
    except Exception as e:
        print(f"  [FAIL] SaveAs: {_short(e)}")
        return
    for label, args in CASES[:3]:
        try:
            ok = mws.AddToHistory(*args)
        except Exception as e:
            print(f"  [FAIL] {label}: 抛异常 {_short(e)}")
            continue
        print(f"  {'[ OK ]' if ok else '[FAIL]'} {label} -> {ok}")


def official_api_check() -> None:
    """顺带看一眼官方 CST Python API（cst 包）是否可用——那条路是
    Dassault 自己维护的（model3d.add_to_history），若可用可直接换过去。"""
    print("-" * 64)
    print("5. 官方 cst Python 包是否可用（备选路线）")
    print("-" * 64)
    try:
        import cst                                    # noqa: F401
        print(f"  [ OK ] import cst 成功：{cst.__file__}")
    except Exception as e:
        print(f"  [ -- ] import cst 失败：{_short(e)}")
        print("         （不走这条路也没关系，只是少一个备选）")


def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(description="AddToHistory 返回 False 的诊断")
    ap.add_argument("--config", default=None,
                    help="算例配置（本脚本不读它，只用于提示）")
    args = ap.parse_args()

    app = cst_api.connect_app()

    print("=" * 64)
    print("A. 新建工程（app.NewMWS()）")
    print("=" * 64)
    mws = app.NewMWS()
    show_signatures(mws)
    show_modeler_members(mws)
    run_matrix(mws, "新建工程")
    read_back(mws)
    state_matrix(mws, app, args.config)

    print("=" * 64)
    print("B. 当前活动工程（app.GetActiveProject()）")
    print("=" * 64)
    try:
        active = app.GetActiveProject()
        print(f"[ OK ] 活动工程对象：{active}")
    except Exception as e:
        print(f"  [FAIL] GetActiveProject: {_short(e)}")
        active = None
    if active is not None:
        run_matrix(active, "活动工程（GUI 里那个）")

    official_api_check()

    print("=" * 64)
    print("""
怎么读这份输出：
  - **矩阵里有没有 True**：有 -> 记下那个形状（尤其"参数换序"那条），
    按它改 scripts/cst_build_template.py 就能建模板；
  - **全 False** -> 条目创建被整体拒绝，与命令内容无关：这时请回到 CST
    GUI 手工画一个 Brick 并保存，看 History List 是否出现那一条——
      * 手工操作也不进历史表 => 这个 CST 会话/安装的历史记录功能有问题
        （贴回截图，走官方 cst 包或重装/修复的思路）；
      * 手工操作进历史表 => 把矩阵输出与签名表贴回，按真实签名改调用。
把上面**完整输出**（含签名表、矩阵、读回、官方包那一节）贴回开发者。
""")


if __name__ == "__main__":
    main()
