"""假 ``cst.results``：``ProjectFile(path).get_3d().get_result_item(路径)``。

照 CST 2024 实测的直路写（服务器 2026-10-02 验过）：**读的是磁盘上的工程
文件**——所以这里的 ``ProjectFile`` 会先查文件在不在，工程没存盘就读会直接
报错（真库同样读不到）。这一条把"求解 → 存盘 → 读结果"的顺序钉死。

默认曲线：每条 S 参数两点（4/6 GHz），值由**条目名**决定（``S3,1`` →
``0.10*3 + 0.05*1j``）——读错端口从数值上就能看出来。测试可以用
``configure(curves={路径: (x, y)})`` 覆盖任意一条，或用 ``missing`` 让某条
读不到（锁"读不到一律抛"）。
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

__all__ = ["ProjectFile", "ResultModule", "Curve", "configure", "reset",
           "state", "values_of", "DEFAULT_FREQS_GHZ"]

#: 缺省曲线的频点（GHz）：5 GHz 正好在中间，线性插值结果 = 端点值。
DEFAULT_FREQS_GHZ = (4.0, 6.0)

_ITEM_RE = re.compile(r"^S(\d+),(\d+)$")


class State:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        #: 条目路径 → (x, y)，覆盖默认曲线
        self.curves: dict[str, tuple] = {}
        #: 这些条目读不到（模拟条目名对不上/结果没生成）
        self.missing: set[str] = set()
        #: 读任何条目都抛这个错（模拟 ResultModule 级别的失败）
        self.item_error: Exception | None = None
        #: 被问过的条目路径（按顺序）
        self.asked: list[str] = []
        #: 被打开过的工程文件 (path, allow_interactive)
        self.opened: list[tuple] = []
        #: True = 工程文件不存在就报错（真库读磁盘，先存盘再读）
        self.require_file = True


_state = State()


def state() -> State:
    return _state


def configure(**kw) -> State:
    for k, v in kw.items():
        if not hasattr(_state, k):
            raise AttributeError(f"假 cst.results 没有这个开关：{k}")
        setattr(_state, k, v)
    return _state


def reset() -> State:
    _state.reset()
    return _state


def values_of(path: str) -> np.ndarray:
    """这条条目在缺省曲线下的值（测试用来算期望值）。"""
    if path in _state.curves:
        return np.asarray(_state.curves[path][1], dtype=complex)
    leaf = path.rsplit("\\", 1)[-1]
    m = _ITEM_RE.match(leaf)
    if not m:
        raise ValueError(f"不是 S 参数条目：{path!r}")
    i, j = int(m.group(1)), int(m.group(2))
    return np.array([complex(0.10 * i, 0.05 * j)] * 2)


class Curve:
    """假 ``Result1D``。"""

    def __init__(self, x, y):
        self._x = np.asarray(x, dtype=float)
        self._y = np.asarray(y, dtype=complex)

    def get_xdata(self):
        return self._x

    def get_ydata(self):
        return self._y


class ResultModule:
    def __init__(self, path):
        self._path = path

    def get_result_item(self, path, run_id=0, load_impedances=True):
        _state.asked.append(path)
        if _state.item_error is not None:
            raise _state.item_error
        if path in _state.missing:
            raise RuntimeError(f"假 cst.results：条目不存在 {path!r}")
        if path in _state.curves:
            x, y = _state.curves[path]
            return Curve(x, y)
        return Curve(DEFAULT_FREQS_GHZ, values_of(path))


class ProjectFile:
    def __init__(self, path, allow_interactive=False):
        _state.opened.append((str(path), bool(allow_interactive)))
        if _state.require_file and not Path(path).exists():
            raise FileNotFoundError(
                f"假 cst.results：工程文件不存在 {path}——真库读磁盘，"
                "必须先 prj.save() 再读结果")
        self._path = str(path)

    def get_3d(self):
        return ResultModule(self._path)

    def get_all_result_ids(self):
        return []
