"""查清 AddToHistory 为什么不生效（一次性诊断，不保存任何工程文件）。

背景：`scripts/cst_build_template.py` 逐块调用
``mws.AddToHistory(标题, 命令文本)``，实测每一块都"失败"。但探针显示返回的
其实是 **None**——晚绑定（Dispatch）下 pywin32 拿不到返回值，所以
"返回 None/False" **不能**当成功判据；真正要看的是 CST 里的模型树与
History List。

**当前的主要假设：问题在"工程对象从哪儿来"。** 官方例程（C#/MATLAB）建完
工程后用 `app.Active3D()` 取回**活动工程**再操作，而我们一直用
`app.NewMWS()` 的返回值。CST 2024 实测 `GetActiveProject` /
`ActiveProject` **都不存在**（`AttributeError ... Did you mean:
'CloseProject'?`）。

本脚本做五件事：

1. 列 Application 上名字含 Active/Project/New/Open/Save 的成员**签名**
   （带形参名，读 IDispatch 类型库）——"取工程对象该用哪个方法"的权威
   答案；
2. 依次取**三个**对象：建工程**之前**的活动工程 → `app.NewMWS()` 的
   返回值 → 建工程**之后**的活动工程，各自打印成员签名并**各打一遍调用
   形状矩阵**；
3. 矩阵用**可见**命令（建一个名叫 ``probeN-brick-lf`` 之类的 Brick）
   ——跑完请到 CST 模型树里看**哪几个 Brick 真的出现了**，这是唯一可靠
   的判据；同时保留 LF/CRLF/末尾换行/参数换序等字符串形状的对照；
4. 读回历史表（无参且名字含 history 的成员全试一遍）；
5. 顺带看官方 `cst` 包在不在（备选路线）。

用法（服务器，CST GUI 已启动；最好先在 GUI 里 File → New 建个空工程）：
    python scripts/cst_probe_history.py

**最佳做法**：先在 CST 里手工画一个 Brick，确认 GUI 自己的操作**能**进
History List（对照组），再跑本脚本，把完整输出 + 模型树截图贴回开发者。
"""

import argparse
import tempfile
from pathlib import Path

from eaopt.solver import cst_api


def _short(exc: BaseException, limit: int = 300) -> str:
    s = str(exc).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _short_repr(value, limit: int = 70) -> str:
    """参数预览：多行命令压成一行，免得刷屏。"""
    s = repr(value)
    s = s.replace("\\n", "\\n").replace("\\r", "\\r")
    return s if len(s) <= limit else s[:limit] + "…"


# ---- 矩阵：调用形状假设 ----

def brick_cmd(name: str, eol: str = "\n") -> str:
    """一段**看得见**的建模命令：建一个名叫 name 的 Brick。

    为什么非要用可见命令：返回值不可靠（晚绑定恒为 None），唯一可信的
    判据是**模型树里真的多出这个 Brick**。名字带前缀，好在树里对号入座。
    """
    lines = [
        "With Brick",
        "     .Reset",
        f'     .Name "{name}"',
        '     .Component "probe"',
        '     .Material "PEC"',
        '     .Xrange "-1", "1"',
        '     .Yrange "-1", "1"',
        '     .Zrange "-1", "1"',
        "     .Create",
        "End With",
    ]
    return eol.join(lines) + eol


def cases(prefix: str = "probe"):
    """(标签, 传给 AddToHistory 的位置参数) —— 除 A/B/C/H 外都**可见**。

    prefix 让不同对象建出的 Brick 名字不撞车（跑完在模型树里数一数就知道
    是哪个对象在起作用）。
    """
    return (
        ("A 空内容（只建条目，不执行任何东西）", (f"{prefix}-empty", "")),
        ("B 单行语句（不可见，控制组）", (f"{prefix}-one", "Brick.Reset")),
        ("C 单行 + 分号（不可见，控制组）", (f"{prefix}-semi", "Brick.Reset;")),
        (f"D 多行 LF → 模型树里应有 {prefix}-brick-lf",
         (f"{prefix}-lf", brick_cmd(f"{prefix}-brick-lf", "\n"))),
        (f"E 多行 CRLF → 模型树里应有 {prefix}-brick-crlf",
         (f"{prefix}-crlf", brick_cmd(f"{prefix}-brick-crlf", "\r\n"))),
        ("F 多行 CRLF + 末尾换行 → 应有 "
         f"{prefix}-brick-crlf2",
         (f"{prefix}-crlf2", brick_cmd(f"{prefix}-brick-crlf2", "\r\n"))),
        (f"G 参数换序（命令在前）→ 应有 {prefix}-brick-swap",
         (brick_cmd(f"{prefix}-brick-swap", "\n"), f"{prefix}-swap")),
        ("H 只给一个参数（标题）", (f"{prefix}-onearg",)),
    )


def _mark(ok) -> str:
    """返回值标记：None 单独一档（晚绑定拿不到返回值，不等于失败）。"""
    if ok is None:
        return "[ ?? ]"
    return "[ OK ]" if ok else "[FAIL]"


def show_signatures(obj, label: str) -> None:
    print("-" * 64)
    print(f"签名：{label}")
    print("-" * 64)
    try:
        sigs = cst_api.member_signatures(obj, "history")
    except Exception as e:
        print(f"  [FAIL] 拿不到类型信息：{_short(e)}")
        return
    if not sigs:
        print("  [!!] 这个对象上没有任何名字含 'history' 的成员"
              "——AddToHistory 不在这个对象上")
        return
    for s in sigs:
        print(f"  {s['name']}  参数 {s['n_params']} 个：{list(s['params'])}")
        if s["help"]:
            print(f"      帮助：{s['help']}")


def show_modeler_members(mws) -> None:
    """有没有独立的"建模器 / 3D 模型"对象？

    官方 CST Python 包里写历史用的是 ``project.model3d.add_to_history(...)``
    ——即 add_to_history 挂在 **Model3D/建模器**对象上，不一定是工程对象。
    这里把候选成员列出来；若真拿到对象，就顺手在那个对象上也打一遍矩阵。
    """
    print("-" * 64)
    print("名字含 model/design 的成员（找独立建模器对象）")
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
            run_matrix(obj, f"建模器对象 {s['name']}()", "probeM")


def run_matrix(obj, title: str, prefix: str = "probe") -> list[str]:
    """把 cases 逐个试一遍，返回明确返回 True 的标签（可能为空）。"""
    print("-" * 64)
    print(f"调用形状矩阵：{title}（Brick 前缀 {prefix}）")
    print("-" * 64)
    good: list[str] = []
    for label, args in cases(prefix):
        try:
            ok = obj.AddToHistory(*args)
        except Exception as e:
            print(f"  [ EXC ] {label}：抛异常 {_short(e)}")
            continue
        shown = " / ".join(_short_repr(a) for a in args)
        print(f"  {_mark(ok)} {label}：AddToHistory({shown}) -> {ok!r}")
        if ok:
            good.append(label)
    if good:
        print(f"  => 明确返回 True 的形状：{good}")
    else:
        print("  => 没有一条明确返回 True。**别据此下结论**：晚绑定下返回值"
              "恒为 None。\n     真正的判据是 CST 模型树里有没有 "
              f"{prefix}-brick-* 这几个 Brick。")
    return good


def read_back(mws) -> None:
    """调用所有无参且名字含 history 的成员，尝试读出历史表内容。"""
    print("-" * 64)
    print("读回历史表（无参且名字含 history 的成员全试一遍）")
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


def state_matrix(mws, label: str) -> None:
    """换个工程状态再打矩阵：先另存到临时文件，再试前几条。

    怀疑过"未存盘的工程不许写历史"，这一步把它排除掉。
    """
    tmp = Path(tempfile.gettempdir()) / "cst_probe_history"
    tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "probe.cst"
    print("-" * 64)
    print(f"另存之后再试：{label} -> {target}")
    print("-" * 64)
    try:
        mws.SaveAs(str(target), True)
        print("[ OK ] SaveAs 成功")
    except Exception as e:
        print(f"  [FAIL] SaveAs: {_short(e)}")
        return
    for label_, args in cases("probeS")[:3]:
        try:
            ok = mws.AddToHistory(*args)
        except Exception as e:
            print(f"  [ EXC ] {label_}：抛异常 {_short(e)}")
            continue
        print(f"  {_mark(ok)} {label_} -> {ok!r}")


def official_api_check() -> None:
    """顺带看一眼官方 CST Python API（cst 包）是否可用——那条路是
    Dassault 自己维护的（model3d.add_to_history），若可用可直接换过去。"""
    print("-" * 64)
    print("官方 cst Python 包是否可用（备选路线）")
    print("-" * 64)
    try:
        import cst                                    # noqa: F401
        print(f"  [ OK ] import cst 成功：{cst.__file__}")
    except Exception as e:
        print(f"  [ -- ] import cst 失败：{_short(e)}")
        print("         （不走这条路也没关系，只是少一个备选）")


def _try_project_methods(app, names, tag: str):
    """按 names 顺序取工程对象，成功即返回（失败原因逐个打印）。"""
    notes: list[str] = []
    try:
        name, obj = cst_api.call_first(app, names, notes)
    except Exception as e:
        for n in notes:
            print(f"  [ -- ] {n}")
        print(f"  [FAIL] {tag}：全部候选都失败（{_short(e)}）")
        return None
    print(f"  [ OK ] {tag}：app.{name}() -> {obj!r}")
    return obj


def main() -> None:
    from eaopt.cli import safe_console

    safe_console()
    ap = argparse.ArgumentParser(description="AddToHistory 不生效的诊断")
    ap.add_argument("--config", default=None,
                    help="算例配置（本脚本不读它，只用于提示）")
    ap.parse_args()

    app = cst_api.connect_app()

    print("=" * 64)
    print("0. Application 上「取工程对象 / 建工程 / 保存」相关的成员签名")
    print("=" * 64)
    for kw in ("Active", "Project", "New", "Open", "Save", "Design", "Model"):
        try:
            sigs = cst_api.member_signatures(app, kw)
        except Exception as e:
            print(f"  app 含 {kw!r}：拿不到类型信息（{_short(e)}）")
            continue
        if not sigs:
            continue
        print(f"  app 含 {kw!r}（{len(sigs)}）：")
        for s in sigs:
            print(f"    {s['name']}({', '.join(s['params'])})")

    # ---- 三个候选对象：建之前的活动工程 / NewMWS 返回值 / 建之后的活动工程 ----
    print("=" * 64)
    print("1. 取三个候选工程对象")
    print("=" * 64)
    print("  （建工程**之前**的活动工程 = GUI 里打开的那个）")
    before = _try_project_methods(app, cst_api.ACTIVE_PROJECT_METHODS,
                                  "建之前的活动工程")
    print()
    print("  （新建工程）")
    new = _try_project_methods(app, cst_api.CREATE_PROJECT_METHODS,
                               "app.NewMWS()/FileNew() 的返回值")
    print()
    print("  （建工程**之后**的活动工程 —— 官方例程在这里取对象）")
    after = _try_project_methods(app, cst_api.ACTIVE_PROJECT_METHODS,
                                 "建之后的活动工程")

    # ---- 对每个对象：签名 + 矩阵 ----
    groups = [("建之前的活动工程", before, "probeA"),
              ("NewMWS() 的返回值", new, "probeB"),
              ("建之后的活动工程", after, "probeC")]
    live = [(label, obj, pre) for label, obj, pre in groups if obj is not None]
    for label, obj, pre in live:
        print("=" * 64)
        print(f"2. 对象：{label}")
        print("=" * 64)
        show_signatures(obj, label)
        show_modeler_members(obj)
        run_matrix(obj, label, pre)
        read_back(obj)
        print()

    if live:
        print("=" * 64)
        print("3. 换个状态再试（另存之后）")
        print("=" * 64)
        state_matrix(live[-1][1], live[-1][0])

    official_api_check()

    print("=" * 64)
    print(f"""
怎么读这份输出：
  - **先看模型树**（不是看返回值）：CST 里 component "probe" 下有没有
    probeA-brick-* / probeB-brick-* / probeC-brick-*？
      * 有 probeC-*（或 probeA-*）而没有 probeB-* => **要用活动工程对象**
        （`app.Active3D()`），`NewMWS()` 的返回值不能拿来建模——
        cst_api.get_project 已经按这个改了；
      * 一个都没有 => 连可见命令都没执行：把这份输出（含签名表）贴回来，
        并请在 GUI 里手工画一个 Brick 看它进不进 History List（对照组）；
      * 只有 CRLF 那几个出现了、LF 没出现（或反之）=> 是**换行符**的
        问题，按结果改 vba.py 的行尾；
      * 只有 G（参数换序）出现 => 这台机器上形参顺序与文档相反。
  - **返回值一列**：None = 晚绑定拿不到返回值（不是失败）；早绑定
    （gencache.EnsureDispatch）下才可能看到 True/False。开头的
    "[OK] COM 连接" 那行会写明这次是早绑定还是晚绑定。
把上面**完整输出**（含 0 节签名表、三个对象的矩阵、读回、以及模型树里
probe 组件的截图）贴回开发者。
""")

if __name__ == "__main__":
    main()
