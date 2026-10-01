"""CST COM 结果读取的候选 API 收敛层。

为什么单独一层：CST 各版本的结果读取 API 名称并不稳定，而"读 S 参数 /
列结果树"这两件事，生产路径（eaopt/solver/cst.py）与诊断路径
（scripts/cst_smoke.py）都要做。把候选调用链集中在这里，服务器实测
收敛后只改这一处——诊断脚本与生产代码不会各写一份、各自漂移。

CST 2024 实测结论（2026-10-01，服务器）：
  - ``ResultTree.GetResultItem(path)`` 不存在 → pywin32 报
    ``<unknown>.GetResultItem``；
  - ``ResultTree.GetAllItems()`` 不存在 → ``<unknown>.GetAllItems``；
  - 可用链（与 Dassault 官方教程 / MATLAB-ActiveX 公开例程一致）::

        ids = ResultTree.GetResultIDsFromTreeItem(path)
        res = ResultTree.GetResultFromTreeItem(path, id)
        x   = res.GetArray("x")      # 频率轴
        yre = res.GetArray("yre")    # 实部
        yim = res.GetArray("yim")    # 虚部

  - 结果树遍历：``GetFirstChildName(folder)`` / ``GetNextItemName(item)``，
    结束条件是返回空串；返回的是可以再喂给 SelectTreeItem 的完整路径。
"""

from __future__ import annotations

import numpy as np

__all__ = ["tree_children", "s_param_ids", "s_param_at"]


def tree_children(result_tree, folder: str) -> list[str]:
    """列出结果树 folder 下的直接子条目（完整路径列表）。

    API 名称不符时返回空列表（诊断脚本会原样打印，便于按实际输出修正），
    不抛出——列表本身是"锦上添花"的诊断信息，不该让调用方挂掉。
    """
    out: list[str] = []
    try:
        item = result_tree.GetFirstChildName(folder)
    except Exception:
        return out
    while item:
        out.append(item)
        try:
            nxt = result_tree.GetNextItemName(item)
        except Exception:
            break
        if nxt == item:          # 防御：万一 API 返回自身，别死循环
            break
        item = nxt
    return out


def s_param_ids(result_tree, tree_path: str) -> list:
    """tree_path 处的结果 ID 列表（取不到返回空列表）。"""
    try:
        ids = result_tree.GetResultIDsFromTreeItem(tree_path)
    except Exception:
        return []
    if isinstance(ids, (list, tuple)):
        return list(ids)
    return [ids] if ids is not None else []


def _get_array(result, key: str):
    """``result.GetArray(key)`` → 一维 float 数组；取不到返回 None。"""
    try:
        arr = np.asarray(result.GetArray(key), dtype=float)
    except Exception:
        return None
    if arr.size == 0:
        return None
    return arr.ravel()


def s_param_at(result_tree, tree_path: str, freq_ghz: float,
               notes: list[str] | None = None):
    """读 tree_path 处的（复数）结果在 freq_ghz 的值，线性插值。

    候选链：GetResultIDsFromTreeItem → 逐个 ID GetResultFromTreeItem →
    优先 ("yre", "yim") 复数对；退而求 "y"（幅度，返回实数值）。
    全部失败返回 None；诊断细节追加到 notes（可选，供 smoke 打印）。
    """
    def note(msg: str) -> None:
        if notes is not None:
            notes.append(msg)

    ids = s_param_ids(result_tree, tree_path)
    note(f"ids={ids}")
    if not ids:
        return None
    for rid in ids:
        try:
            res = result_tree.GetResultFromTreeItem(tree_path, rid)
        except Exception as e:
            note(f"GetResultFromTreeItem({rid!r}) 失败: {e}")
            continue
        x = _get_array(res, "x")
        if x is None or x.size < 2:
            note(f"id={rid!r}: 取不到频率轴 x")
            continue
        if not (x.min() <= freq_ghz <= x.max()):
            note(f"警告: {freq_ghz:g} GHz 超出数据范围 "
                 f"[{x.min():g}, {x.max():g}]，插值会被端点截断")
        re = _get_array(res, "yre")
        im = _get_array(res, "yim")
        if re is not None and im is not None and re.size == im.size == x.size:
            note(f"id={rid!r}: 复数 (x, yre, yim), n={x.size}")
            return complex(float(np.interp(freq_ghz, x, re)),
                           float(np.interp(freq_ghz, x, im)))
        mag = _get_array(res, "y")
        if mag is not None and mag.size == x.size:
            note(f"id={rid!r}: 只有幅度 y（无 yre/yim），按实数值返回")
            return complex(float(np.interp(freq_ghz, x, mag)))
        note(f"id={rid!r}: 数组布局不认识 "
             f"(x={x.size}, yre={None if re is None else re.size}, "
             f"yim={None if im is None else im.size}, "
             f"y={None if mag is None else mag.size})")
    return None
