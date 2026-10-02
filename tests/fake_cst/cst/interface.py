"""假 ``cst.interface``：DesignEnvironment / Project / Model3D / ASCIIExport。

形状照 CST 2024（2026-10-02 服务器探针实测过的那套）：

* ``cst.interface.DesignEnvironment.connect_to_any() / new() / new_mws() /
  get_open_projects() / open_project(path)``；
* ``prj.filename()`` / ``prj.save(path[, allow_overwrite])``；
* ``prj.model3d.add_to_history(header, cmd)``——位置参数、成功返回 True、
  坏命令抛 RuntimeError；
* ``model3d.run_solver()``（无参）、``model3d.is_solver_running()``、
  ``model3d.SelectTreeItem(path)``、``model3d.ASCIIExport``。

故意**不提供** ``model3d.ResultTree``：那条活工程回退链已被删除，生产代码
若残留旧路会立刻 AttributeError，而不是悄悄走另一条路。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

__all__ = [
    "DesignEnvironment",
    "running_design_environments",
    "configure",
    "reset",
    "state",
    "grid_for",
    "write_ascii_field",
]

#: 假场文件的采样步长（mm）——比真导出粗，够用且快。
GRID_STEP_MM = 0.5

#: 不指定网格时的缺省采样（覆盖毫米级器件的常见设计区）。
DEFAULT_GRID = (np.arange(-20.0, 20.0 + 1e-9, 2.0),
                np.arange(-20.0, 20.0 + 1e-9, 2.0),
                np.array([0.0, 0.5]))


class State:
    """一次假会话的全部可编程状态。"""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.grid = DEFAULT_GRID
        #: 调用时序（"connect_to_any"/"new"/"new_mws"/"open_project"/"save"…）
        self.events: list[tuple] = []
        #: 工程对象的常驻清单——"实例跨进程存活"的假实现
        self.projects: list[FakeProject] = []
        self.env_closed = False

        # ---- 可编程的失败/行为开关 ----
        self.fail_connect = False          # connect_to_any 抛错
        self.solver_running = False        # is_solver_running 返回 True
        self.solve_error: Exception | None = None
        self.history_error: tuple[str, Exception] | None = None   # (header|"*", exc)
        self.history_false_on: str | None = None                  # 该块返回 False
        self.save_accepts_overwrite = True  # False = 只认 save(path) 一种形参
        self.save_error: Exception | None = None
        self.select_error: Exception | None = None


_state = State()


def state() -> State:
    """当前状态（测试断言用）。"""
    return _state


def configure(**kw) -> State:
    """按关键字设定状态（等价于逐个赋值，读起来更短）。"""
    for k, v in kw.items():
        if not hasattr(_state, k):
            raise AttributeError(f"假 cst.interface 没有这个开关：{k}")
        setattr(_state, k, v)
    return _state


def reset() -> State:
    """复位（conftest 的 autouse fixture 每个测试都会调）。"""
    _state.reset()
    return _state


def grid_for(x, y, margin: float = 0.0, step: float = GRID_STEP_MM,
             z=(0.0, 0.5)):
    """按设计区 ``x``/``y``（含 margin）造一个够用的采样网格。

    网格**比裁剪窗口大一圈**，这样"裁到设计区 ± margin"才有东西可裁；
    步长取 GRID_STEP_MM，够快又能在数值上区分位置。
    """
    xs = np.arange(x[0] - margin - 1.0, x[1] + margin + 1.0 + 1e-9, step)
    ys = np.arange(y[0] - margin - 1.0, y[1] + margin + 1.0 + 1e-9, step)
    return (xs, ys, np.array(list(z), dtype=float))


def write_ascii_field(path, x, y, z) -> None:
    """按 CST FixedWidth 导出的格式写一个场文件（``parse_ascii_field`` 的输入）。

    三行头 ``x0 x1 nx`` + 6 个分量块（实部 3 块、虚部 3 块），列优先。
    """
    nx, ny, nz = len(x), len(y), len(z)
    data = np.zeros((nx, ny, nz, 3), dtype=complex)
    # 值随 x 下标变化且恒非零：裁剪后要么保住它、要么一看就知道裁错了
    data[..., 0] = (np.arange(nx)[:, None, None] + 1.0) * (1.0 + 0.5j)
    data[..., 1] = np.arange(ny)[None, :, None] * 0.25j
    data[..., 2] = np.arange(nz)[None, None, :] * 1.0
    lines = [f"{x[0]} {x[-1]} {nx}", f"{y[0]} {y[-1]} {ny}", f"{z[0]} {z[-1]} {nz}"]
    for c in range(3):
        lines += [f"{v:.12g}" for v in data[..., c].reshape(-1, order="F").real]
    for c in range(3):
        lines += [f"{v:.12g}" for v in data[..., c].reshape(-1, order="F").imag]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# VBA 对象模型
# --------------------------------------------------------------------------- #
class ASCIIExport:
    """假 ``model3d.ASCIIExport``：记下设置，``Execute`` 时落一个真文件。

    属性集照 CST 2024 实测：只有 Reset / FileName / Mode / StepX / StepY /
    StepZ / Execute（**没有** XStart…ZEnd，范围永远是整个包围盒）。
    """

    def __init__(self, st: State):
        self._st = st
        self.calls: list[tuple] = []
        self.file = None

    def Reset(self):
        self.calls.append(("Reset",))
        self.file = None

    def FileName(self, v):
        self.file = str(v)
        self.calls.append(("FileName", str(v)))

    def Mode(self, v):
        self.calls.append(("Mode", str(v)))

    def StepX(self, v):
        self.calls.append(("StepX", str(v)))

    def StepY(self, v):
        self.calls.append(("StepY", str(v)))

    def StepZ(self, v):
        self.calls.append(("StepZ", str(v)))

    def Execute(self):
        self.calls.append(("Execute",))
        if not self.file:
            raise RuntimeError("ASCIIExport: FileName 还没设")
        write_ascii_field(self.file, *self._st.grid)


class Model3D:
    """假 ``prj.model3d``。"""

    def __init__(self, st: State):
        self._st = st
        self.history: list[tuple] = []
        self.solves = 0
        self.selected: list[str] = []
        self.ASCIIExport = ASCIIExport(st)

    def add_to_history(self, header, cmd):
        st = self._st
        self.history.append((header, cmd))
        if st.history_error is not None:
            hdr, exc = st.history_error
            if hdr == "*" or hdr == header:
                raise exc
        if st.history_false_on == header:
            return False
        return True

    def run_solver(self):
        """**无参**——真库实测就是无参；多传参数这里会 TypeError。"""
        st = self._st
        if st.solver_running:
            raise RuntimeError("求解器已在运行")
        st.events.append(("run_solver",))
        self.solves += 1
        if st.solve_error is not None:
            raise st.solve_error

    def is_solver_running(self):
        return self._st.solver_running

    def SelectTreeItem(self, item):
        if self._st.select_error is not None:
            raise self._st.select_error
        self.selected.append(item)


class Project:
    """假 ``prj``。"""

    def __init__(self, st: State, path=None):
        self._st = st
        self.model3d = Model3D(st)
        self._path = str(path) if path else None
        self.saves: list[dict] = []

    def filename(self):
        return self._path

    def save(self, path=None, allow_overwrite=None, **kw):
        st = self._st
        if allow_overwrite is not None and not st.save_accepts_overwrite:
            raise TypeError("save() got an unexpected keyword argument "
                            "'allow_overwrite'")
        self.saves.append({"path": path, "allow_overwrite": allow_overwrite, **kw})
        st.events.append(("save", str(path)))
        if st.save_error is not None:
            raise st.save_error
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fake-cst-project", encoding="utf-8")
        self._path = str(path)
        return True

    def close(self):
        self._st.events.append(("close", self._path))
        if self in self._st.projects:
            self._st.projects.remove(self)


class DesignEnvironment:
    """假 ``DesignEnvironment``——**方法全是静态的**（真库的用法就是类级调用：
    ``cst.interface.DesignEnvironment.new()`` 是"起一个实例"，调用时还没有
    任何实例可依附）。

    假库里只有一个"常驻"实例（模块单例 ``_ENV``）：``new()`` 返回它，所以
    两次脚本调用看到的是同一批工程——这正是"常驻会话"要的语义。
    """

    @staticmethod
    def connect_to_any():
        _state.events.append(("connect_to_any",))
        if _state.fail_connect:
            raise RuntimeError("没有正在运行的 CST 实例")
        return _ENV

    @staticmethod
    def new(**kw):
        _state.events.append(("new", tuple(sorted(kw))))
        return _ENV

    @staticmethod
    def new_mws():
        _state.events.append(("new_mws",))
        prj = Project(_state)
        _state.projects.append(prj)
        return prj

    @staticmethod
    def get_open_projects():
        return list(_state.projects)

    @staticmethod
    def open_project(path):
        want = str(Path(path).resolve()).lower()
        for prj in _state.projects:
            got = prj.filename()
            if got and str(Path(got).resolve()).lower() == want:
                # 真库对已打开的工程会报错；这里照做，锁住"同一工程不重复打开"
                raise RuntimeError(f"工程已经打开：{path}")
        _state.events.append(("open_project", str(path)))
        prj = Project(_state, path)
        _state.projects.append(prj)
        return prj

    @staticmethod
    def close():
        _state.events.append(("close_env",))
        _state.env_closed = True


_ENV = DesignEnvironment()


def running_design_environments():
    """模块级函数（真库有）：返回运行中的实例。假库里只维护一个常驻实例。"""
    return [] if _state.env_closed else [_ENV]
