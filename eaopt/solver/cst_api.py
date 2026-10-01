"""CST COM 结果读取的候选 API 收敛层。

为什么单独一层：CST 各版本的结果读取 API 名称并不稳定，而"读 S 参数 /
列结果树 / 定位场结果条目"这几件事，生产路径（eaopt/solver/cst.py）与
诊断路径（scripts/cst_smoke.py）都要做。把候选调用链集中在这里，服务器
实测收敛后只改这一处——诊断脚本与生产代码不会各写一份、各自漂移。

CST 2024 实测结论（2026-10-01，服务器）：
  - ``ResultTree.GetResultItem(path)`` 不存在 → pywin32 报
    ``<unknown>.GetResultItem``；
  - ``ResultTree.GetAllItems()`` 不存在 → ``<unknown>.GetAllItems``；
  - 可用链（与 Dassault 官方教程 / 公开例程一致）::

        ids = ResultTree.GetResultIDsFromTreeItem(path)   # path 是**条目**，不是文件夹
        res = ResultTree.GetResultFromTreeItem(path, id)
        x   = res.GetArray("x")      # 频率轴
        yre = res.GetArray("yre")    # 实部
        yim = res.GetArray("yim")    # 虚部

  - 结果树遍历：``GetFirstChildName(folder)`` / ``GetNextItemName(item)``，
    结束条件是返回空串；返回的是可以再喂给 SelectTreeItem 的完整路径。
  - 备选链（CST 帮助文档的 Result1D 家族）::

        file = ResultTree.GetFileFromTreeItem(path)
        obj  = Result1DComplex(file)      # 全局函数，COM 里挂在 project 上
        n    = obj.GetClosestIndexFromX(5.0) / obj.GetN() / obj.GetX(i) / obj.GetY(i)

  - 诊断原则：**失败要报原始错误**。这一层的每个函数都可选接收 notes
    列表，把"为什么没读到"原样写进去（pywin32 的异常文本能区分
    "方法不存在"与"参数/路径不对"，是收敛 API 的唯一线索）。
  - 会话与自省：connect_app()（附接 GUI 实例）、member_signatures() /
    member_names()（读 IDispatch 类型库，拿**真实成员名与形参名**，
    调用形状存疑时以它为准）；脚本与 cst.py 共用这一层。
"""

from __future__ import annotations

import numpy as np

__all__ = ["tree_children", "s_param_ids", "find_item", "s_param_at",
           "s_param_via_result1d", "member_names", "member_signatures",
           "connect_app", "call_first", "get_project",
           "ACTIVE_PROJECT_METHODS", "CREATE_PROJECT_METHODS"]

PROGID = "CSTStudio.Application"

# 取"当前活动 3D 工程"的候选方法名。**Active3D 是官方例程的写法**
# （C#: CST.Active3D() → MWS.AddToHistory(...)；MATLAB 同源），
# CST 2024 实测 app.GetActiveProject / app.ActiveProject **都不存在**
# （pywin32 报 AttributeError），所以 Active3D 必须排最前。
ACTIVE_PROJECT_METHODS = ("Active3D", "ActiveMWS", "GetActiveProject",
                          "ActiveProject", "GetActiveDesignEnvironment")
# 新建工程的候选。NewMWS = 明确新建微波工作室（3D）工程；
# FileNew = 走 GUI 的 File → New（官方 C# 例程用它，其后必接 Active3D）。
CREATE_PROJECT_METHODS = ("NewMWS", "FileNew")


def connect_app():
    """连接运行中的 CST，返回 Application 对象。

    候选顺序：
      1. **EnsureDispatch（早绑定）**——用注册的类型库生成包装类，方法签名
         （含返回值/形参类型）都是编译期已知的，pywin32 不用猜。实测晚绑定
         时 `AddToHistory` 返回 **None**（拿不到返回值），早绑定能拿到真正的
         True/False；
      2. Dispatch（晚绑定，装了 pywin32 就能用）；
      3. GetActiveObject（附接已注册的运行实例）。

    三个都失败抛 RuntimeError——提示先打开 CST GUI，避免脚本悄悄起一个
    后台实例、而用户对着另一个窗口找模型。
    """
    import win32com.client

    errs = []
    for label, factory in (
        ("EnsureDispatch（早绑定）", lambda: win32com.client.gencache
         .EnsureDispatch(PROGID)),
        ("Dispatch（晚绑定）", lambda: win32com.client.Dispatch(PROGID)),
        ("GetActiveObject", lambda: win32com.client.GetActiveObject(PROGID)),
    ):
        try:
            app = factory()
        except Exception as e:
            errs.append(f"{label}: {e}")
            continue
        early = "gen_py" in getattr(type(app), "__module__", "")
        print(f"[OK] COM 连接：{label}"
              + ("（早绑定生效）" if early else "（**仍是晚绑定**，返回值可能"
                 "拿不到，见 cst_api.connect_app 的说明）"))
        return app
    raise RuntimeError("无法连接 CST（请先启动 CST Studio 2024 并保持 GUI "
                       "打开）：" + "；".join(str(e) for e in errs))


def call_first(obj, names, notes: list[str] | None = None):
    """按顺序试 names 里的方法，返回 ``(名字, 返回值)``；全失败抛最后一个错。

    不在 hasattr 上做判断：pywin32 的晚绑定对象对**不存在的成员**也会在
    调用时才报错，用 getattr + 调用最直接（错误原文写进 notes）。
    """
    last: Exception | None = None
    for name in names:
        try:
            method = getattr(obj, name)
        except Exception as e:          # 名字不存在：换下一个
            last = e
            _note(notes, f"{name}: {e}")
            continue
        try:
            return name, method()
        except Exception as e:
            last = e
            _note(notes, f"{name}(): {e}")
    if last is None:
        raise RuntimeError(f"没有可用的方法名：{names}")
    raise last


def get_project(app, attach: bool = False):
    """取得要操作的工程对象（建模板/改模型都用它）。

    attach=True：只取 GUI 里**当前活动的 3D 工程**（不发新建）。
    否则：先新建（NewMWS/FileNew），**再统一用 Active3D 之类的活动工程
    取回对象**——这是 CST 官方例程（C#/MATLAB）的写法，实测差别很大：
    `app.NewMWS()` 返回的那个对象上调 `AddToHistory` 一律没有效果
    （返回 None、历史表与模型树都不动），而从 `Active3D()` 拿到的活动工程
    才是可写的那个。取不到活动工程时退回新建函数的返回值（聊胜于无，
    并打印警告）。
    """
    if attach:
        name, proj = call_first(app, ACTIVE_PROJECT_METHODS)
        print(f"[OK] 工程对象：app.{name}()（--attach：用 GUI 里当前打开的"
              f"工程，请确认它就是你要建模板的那个空工程）")
        return proj
    created = None
    try:
        cname, created = call_first(app, CREATE_PROJECT_METHODS)
        print(f"[OK] 新建工程：app.{cname}()")
    except Exception as e:
        print(f"[ -- ] 新建失败（继续试取活动工程）：{e}")
    notes: list[str] = []
    try:
        name, proj = call_first(app, ACTIVE_PROJECT_METHODS, notes)
        print(f"[OK] 工程对象：app.{name}()"
              + ("" if created is None else "（活动工程，用它建模）"))
        return proj
    except Exception as e:
        for n in notes:
            print(f"     [ -- ] {n}")
        if created is None:
            raise
        print(f"!! 取不到活动工程（{e}），退回用新建函数返回的对象——"
              "实测那个对象上 AddToHistory 常常无效，出现"
              "「历史表空/模型没建上」时优先怀疑这里")
        return created


def member_signatures(obj, keyword: str | None = None) -> list[dict]:
    """COM 对象成员**签名**：名字 + 参数名 + 参数个数 + 帮助串。

    比 member_names 多给参数名——调用形状存疑时（比如 AddToHistory 到底
    是 (标题, 命令) 还是 (命令, 标题)、要 1 个还是 2 个参数），类型库里的
    形参名就是权威答案，不必猜、也不必反复试。

    **不要靠猜 CST 的 API 名字**：各版本方法名有出入，而 pywin32 的
    "dynamic dispatch" 让 `hasattr` 对不存在的成员也返回 True。
    keyword 非空时只返回名字含该串的成员（工程对象有几百个成员）。
    取不到类型信息时抛原始异常——调用方决定怎么报。
    """
    ole = getattr(obj, "_oleobj_", obj)      # 已包成 PyIDispatch 的也能用
    ti = ole.GetTypeInfo()
    ta = ti.GetTypeAttr()
    out: dict[str, dict] = {}
    for i in range(ta.cFuncs):
        try:
            fd = ti.GetFuncDesc(i)
            names = list(ti.GetNames(fd.memid))
        except Exception:
            continue
        if not names:
            continue
        name = names[0]
        if keyword and keyword.lower() not in name.lower():
            continue
        try:
            doc = ti.GetDocumentation(fd.memid)
            help_text = (doc[1] or "").strip()
        except Exception:
            help_text = ""
        sig = out.setdefault(name, {
            "name": name, "params": names[1:],
            "n_params": getattr(fd, "cParams", len(names) - 1),
            "help": help_text, "id": fd.memid})
        if not sig["params"] and len(names) > 1:
            sig["params"] = names[1:]        # 同名重载：补上带形参名的那条
    return sorted(out.values(), key=lambda d: d["name"])


def member_names(obj, keyword: str | None = None) -> list[str]:
    """COM 对象真实成员名，按名字排序去重（= member_signatures 的名字列）。"""
    return [s["name"] for s in member_signatures(obj, keyword)]


def _note(notes: list[str] | None, msg: str) -> None:
    if notes is not None:
        notes.append(msg)


def _extend(notes: list[str] | None, msgs: list[str]) -> None:
    if notes is not None:
        notes.extend(msgs)


def _leaf(path: str) -> str:
    return path.rsplit("\\", 1)[-1]


def tree_children(result_tree, folder: str,
                  notes: list[str] | None = None) -> list[str]:
    """列出结果树 folder 下的直接子条目（完整路径列表）。

    取不到时返回空列表并**把原始错误写进 notes**——"列举为空"既可能是
    "真没有结果"，也可能是"API 名字不对"，只有原始错误能区分。
    """
    out: list[str] = []
    try:
        item = result_tree.GetFirstChildName(folder)
    except Exception as e:
        _note(notes, f"GetFirstChildName({folder!r}) 失败: {e}")
        return out
    while item:
        out.append(item)
        try:
            nxt = result_tree.GetNextItemName(item)
        except Exception as e:
            _note(notes, f"GetNextItemName({item!r}) 失败: {e}")
            break
        if nxt == item:          # 防御：万一 API 返回自身，别死循环
            _note(notes, f"GetNextItemName({item!r}) 返回自身，提前结束")
            break
        item = nxt
    return out


def find_item(result_tree, folder: str, prefix: str,
              notes: list[str] | None = None) -> str | None:
    """在 folder 下找叶子名等于/以 prefix 开头的条目，返回完整路径。

    用途：CST 给结果条目自动加后缀（`e-field (f=5)` → 结果树里是
    `e-field (f=5) [AC]` 之类，后缀随求解器/监视器类型变），所以**不要
    拿名字硬拼路径**——拿监视器名去结果树里对前缀，拿到真路径再选中/读取。
    找不到返回 None（诊断信息写进 notes）。
    """
    kids = tree_children(result_tree, folder, notes)
    for child in kids:
        leaf = _leaf(child)
        if leaf == prefix or leaf.startswith(prefix):
            return child
    _note(notes, f"{folder!r} 下没有以 {prefix!r} 开头的条目"
                 f"（现有 {len(kids)} 条：{kids[:8]}）")
    return None


def s_param_ids(result_tree, tree_path: str,
                notes: list[str] | None = None) -> list:
    """tree_path 处的结果 ID 列表（取不到返回空列表）。

    注意 tree_path 是**结果条目**路径（如 ``1D Results\\S-Parameters\\S1,1``），
    不是文件夹——对文件夹调用通常返回空列表。
    """
    try:
        ids = result_tree.GetResultIDsFromTreeItem(tree_path)
    except Exception as e:
        _note(notes, f"GetResultIDsFromTreeItem({tree_path!r}) 失败: {e}")
        return []
    if isinstance(ids, (list, tuple)):
        out = list(ids)
    elif ids is None:
        out = []
    else:
        out = [ids]
    if not out:
        _note(notes, f"GetResultIDsFromTreeItem({tree_path!r}) 返回空")
    return out


def _get_array(result, key: str):
    """``result.GetArray(key)`` → (一维 float 数组, 原始错误)。

    取不到时把**原始错误**一并返回——"这个 key 不存在"与"整个 GetArray
    不可用"是两种病，只有原文能区分（pywin32 会报 ``<unknown>.xxx``）。
    """
    try:
        arr = np.asarray(result.GetArray(key), dtype=float)
    except Exception as e:
        return None, str(e)
    if arr.size == 0:
        return None, "空数组"
    return arr.ravel(), None


def _pick_array(result, keys, notes, rid) -> np.ndarray | None:
    """按 keys 顺序试取值；全失败时把原始错误写进 notes。"""
    errs = []
    for key in keys:
        arr, err = _get_array(result, key)
        if arr is not None:
            return arr
        errs.append(f"{key}: {err}")
    _note(notes, f"id={rid!r}: 取不到 {keys} —— " + "；".join(errs))
    return None


def _order_ids(ids, tree_path: str) -> list:
    """把叶子名对应的 ID 排到最前。

    公开例程提示：同一树条目的 ID 列表里可能混着"坐标轴"之类的辅助项，
    先试与条目同名的那个（S1,1 的 ID 就叫 "S1,1"），能少走弯路。
    """
    leaf = _leaf(tree_path)
    return sorted(ids, key=lambda i: 0 if str(i) == leaf else 1)


def s_param_at(result_tree, tree_path: str, freq_ghz: float,
               notes: list[str] | None = None, project=None):
    """读 tree_path 处的（复数）结果在 freq_ghz 的值，线性插值。

    候选链 1：GetResultIDsFromTreeItem → 逐个 ID GetResultFromTreeItem →
    优先 ("yre", "yim") 复数对；退而求 "y"（幅度，返回实数值）。
    候选链 2（给了 project 才试）：GetFileFromTreeItem + Result1DComplex。
    全部失败返回 None；诊断细节追加到 notes（可选，供 smoke 打印）。
    """
    ids = s_param_ids(result_tree, tree_path, notes)
    _note(notes, f"ids={ids}")
    for rid in _order_ids(ids, tree_path):
        # 每个 id 的诊断先攒在 local，读成功就不污染 notes（幅度回退那条
        # 路本来就会缺 yre/yim，不算错误）。
        local: list[str] = []
        try:
            res = result_tree.GetResultFromTreeItem(tree_path, rid)
        except Exception as e:
            _note(notes, f"GetResultFromTreeItem({rid!r}) 失败: {e}")
            continue
        x = _pick_array(res, ("x",), local, rid)
        if x is None or x.size < 2:
            _extend(notes, local)
            _note(notes, f"id={rid!r}: 频率轴不可用")
            continue
        if not (x.min() <= freq_ghz <= x.max()):
            _note(notes, f"警告: {freq_ghz:g} GHz 超出数据范围 "
                         f"[{x.min():g}, {x.max():g}]，插值会被端点截断")
        re = _pick_array(res, ("yre", "re"), local, rid)
        im = _pick_array(res, ("yim", "im"), local, rid)
        if re is not None and im is not None and re.size == im.size == x.size:
            v = complex(float(np.interp(freq_ghz, x, re)),
                        float(np.interp(freq_ghz, x, im)))
            _note(notes, f"id={rid!r}: 复数 (x, yre, yim), n={x.size}")
            _check_magnitude(v, notes)
            return v
        mag = _pick_array(res, ("y",), local, rid)
        if mag is not None and mag.size == x.size:
            v = complex(float(np.interp(freq_ghz, x, mag)))
            _note(notes, f"id={rid!r}: 只有幅度 y（无 yre/yim），按实数值返回")
            return v
        _extend(notes, local)
        _note(notes, f"id={rid!r}: 数组布局不认识 "
                     f"(x={x.size}, yre={_size(re)}, yim={_size(im)}, "
                     f"y={_size(mag)})")
    if ids:
        _note(notes, "候选链 1 全部失败，改试 Result1DComplex")
    if project is not None:
        v = s_param_via_result1d(project, result_tree, tree_path, freq_ghz, notes)
        if v is not None:
            return v
    return None


def _size(arr) -> str:
    return "None" if arr is None else str(arr.size)


def _check_magnitude(v: complex, notes: list[str] | None) -> None:
    """|S| 明显 >1 时提醒——多半是读到了坐标轴之类的辅助条目。"""
    if abs(v) > 1.5:
        _note(notes, f"可疑: |S|={abs(v):.3g} 远大于 1，可能读错了条目")


def s_param_via_result1d(project, result_tree, tree_path: str, freq_ghz: float,
                         notes: list[str] | None = None):
    """备选链：GetFileFromTreeItem + Result1DComplex（CST 帮助的 Result1D 家族）。

    VBA 里是全局函数 ``Result1DComplex(file)``；COM 自动化下挂在工程对象上
    （``project.Result1DComplex``）。复数取值用哪对访问器各版本不一，故按
    ("Y","YImag") → ("YReal","YImag") → ("Y","YPhase") 依次试，全部失败
    返回 None 并把原始错误写进 notes。
    """
    try:
        f = result_tree.GetFileFromTreeItem(tree_path)
    except Exception as e:
        _note(notes, f"GetFileFromTreeItem({tree_path!r}) 失败: {e}")
        return None
    try:
        obj = project.Result1DComplex(f)
    except Exception as e:
        _note(notes, f"Result1DComplex({f!r}) 失败: {e}")
        return None
    try:
        i = int(obj.GetClosestIndexFromX(freq_ghz))
    except Exception as e:
        _note(notes, f"GetClosestIndexFromX({freq_ghz:g}) 失败: {e}")
        return None
    for keys in (("Y", "YImag"), ("YReal", "YImag"), ("Y", "YPhase")):
        vals = []
        for key in keys:
            try:
                vals.append(float(getattr(obj, "Get" + key)(i)))
            except Exception as e:
                _note(notes, f"Result1DComplex.Get{key}({i}) 失败: {e}")
                vals = None
                break
        if vals is None:
            continue
        re, im = vals
        v = complex(re, im) if keys != ("Y", "YPhase") else \
            complex(re * np.cos(np.deg2rad(im)), re * np.sin(np.deg2rad(im)))
        _note(notes, f"Result1DComplex: idx={i} 用 {keys} 取值")
        _check_magnitude(v, notes)
        return v
    return None
