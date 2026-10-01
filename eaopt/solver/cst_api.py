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
"""

from __future__ import annotations

import numpy as np

__all__ = ["tree_children", "s_param_ids", "find_item", "s_param_at",
           "s_param_via_result1d"]


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
