"""假 ``cst.interface``：DesignEnvironment / Project / Model3D / ASCIIExport。

形状照 CST 2024（2026-10-02 服务器探针实测过的那套）：

* ``cst.interface.DesignEnvironment.connect_to_any() / new() / new_mws() /
  get_open_projects() / open_project(path)``；
* ``prj.filename()`` / ``prj.save(path[, allow_overwrite])``；
* ``prj.model3d.add_to_history(header, cmd)``——位置参数、成功返回 True、
  坏命令抛 RuntimeError；
* ``model3d.run_solver()``（无参）、``model3d.is_solver_running()``；
* ``model3d.ResultTree``——``GetFirstChildName(父路径)`` /
  ``GetNextItemName(条目)`` 的树遍历（CST VBA 协议），按扁平清单
  ``state.tree_items`` 逐层回答；
* ``DesignEnvironment.set_quiet_mode(True) / in_quiet_mode()``——**会话级**
  静默开关：真机上是"模态框不弹、自动按默认按钮走"；
* ``model3d._execute_vba_code("Sub Main ... End Sub")``——控制宏通道
  （执行 VBA 但**不进历史表**），``DeleteResults`` 走的就是它；
* 模态框的**可测代理**：工程有结果（``has_results``）又在非静默会话里
  ``run_solver()`` 时抛错——真机上是"弹框等人点"，脚本卡死；假库把它变成
  一个能断言的异常，正是本轮修的坑；
* ``model3d.SelectTreeItem(条目)``——**照真库返回布尔**（条目在树上吗），
  并且不在树上时**照真库"静默不生效"**：随后 ``ASCIIExport.Execute()``
  报 "The ASCII export option is not available for the current view."。
  这条静默失败链是生产代码必须自己防住的（先查树、再看返回值）。
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

#: 缺省结果树（扁平全路径清单，层级由 ``\`` 隐含）——照耦合器算例 5 GHz
#: 求解后的样子。场条目**故意不带** ` [AC]` 后缀：真条目名由 CST 决定，
#: 生产代码必须到树上按叶子名找（``field_result_path`` 拼的那份在这里就
#: 选不中——正是服务器上暴露的那个坑）。用 ``configure(tree_items=[...])``
#: 换一棵树。
DEFAULT_TREE_ITEMS = [
    "1D Results",
    "1D Results\\S-Parameters",
    "1D Results\\S-Parameters\\S1,1 [AC]",
    "1D Results\\S-Parameters\\S2,1 [AC]",
    "1D Results\\S-Parameters\\S3,1 [AC]",
    "1D Results\\S-Parameters\\S4,1 [AC]",
    "2D/3D Results",
    "2D/3D Results\\E-Field",
    "2D/3D Results\\E-Field\\e-field (f=5)",
    "2D/3D Results\\H-Field",
    "2D/3D Results\\H-Field\\h-field (f=5)",
]


class State:
    """一次假会话的全部可编程状态。"""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.grid = DEFAULT_GRID
        self.tree_items = list(DEFAULT_TREE_ITEMS)
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
        self.tree_error: Exception | None = None     # ResultTree 遍历抛错
        self.drop_result_tree = False                # Model3D 上没有 ResultTree（建工程前设）
        self.ascii_export_error: Exception | None = None  # Execute 直接抛
        self.quiet_mode = False            # 会话静默（真机上是**跨脚本**的会话级状态）
        self.has_quiet_mode_api = True     # False = 该版本没有 set_quiet_mode()
        self.quiet_mode_error: Exception | None = None    # set_quiet_mode 抛错
        self.initial_has_results = False   # 新打开的工程=已经有结果（重开旧 .cst）
        self.control_vba_error: Exception | None = None   # _execute_vba_code 抛错


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
    """按 CST FixedWidth 导出的**实测格式**写一个场文件（``parse_ascii_field`` 的输入）。

    服务器 2026-10-04 实测的真实布局：表头一行（列名）+ 分隔线一行 +
    **每个点一行 9 列** ``x y z Re1 Im1 Re2 Im2 Re3 Im3``（点序 x 变最快，
    同 CST）。列名用 F1/F2/F3 占位（假库不分 E/H，单位随场类型）。
    """
    nx, ny, nz = len(x), len(y), len(z)
    data = np.zeros((nx, ny, nz, 3), dtype=complex)
    # 值随 x 下标变化且恒非零：裁剪后要么保住它、要么一看就知道裁错了
    data[..., 0] = (np.arange(nx)[:, None, None] + 1.0) * (1.0 + 0.5j)
    data[..., 1] = np.arange(ny)[None, :, None] * 0.25j
    data[..., 2] = np.arange(nz)[None, None, :] * 1.0
    X, Y, Z = np.meshgrid(np.asarray(x, float), np.asarray(y, float),
                          np.asarray(z, float), indexing="ij")
    lines = [
        "      x [mm]      y [mm]      z [mm]   F1Re [V/m]   F1Im [V/m]"
        "   F2Re [V/m]   F2Im [V/m]   F3Re [V/m]   F3Im [V/m]",
        "-" * 120,
    ]
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                v = data[i, j, k]
                lines.append(
                    f"{X[i, j, k]:>12.6g} {Y[i, j, k]:>12.6g} {Z[i, j, k]:>12.6g} "
                    + " ".join(f"{c.real:>12.8g} {c.imag:>12.8g}" for c in v))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# VBA 对象模型
# --------------------------------------------------------------------------- #
class ASCIIExport:
    """假 ``model3d.ASCIIExport``：记下设置，``Execute`` 时落一个真文件。

    属性集照 CST 2024 实测：只有 Reset / FileName / Mode / StepX / StepY /
    StepZ / Execute（**没有** XStart…ZEnd，范围永远是整个包围盒）。
    """

    def __init__(self, st: State, m3d: "Model3D"):
        self._st = st
        self._m3d = m3d
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
        if self._st.ascii_export_error is not None:
            raise self._st.ascii_export_error
        if not self._m3d.selection_valid:
            # 真库的原话：选中的不是可导出的结果视图（多半是 SelectTreeItem
            # 没生效，当前视图还停在 Modeling）——静默失效的下一站。
            raise RuntimeError(
                "(&H8000ffff) The ASCII export option is not available for "
                "the current view.")
        if not self.file:
            raise RuntimeError("ASCIIExport: FileName 还没设")
        write_ascii_field(self.file, *self._st.grid)


class ResultTree:
    """假 ``model3d.ResultTree``：CST VBA 的树遍历协议。

    ``GetFirstChildName(父路径)`` 给第一个子条目、``GetNextItemName(条目)``
    给同层下一个兄弟，**都是全路径**，没有则空串。``state.tree_items`` 是
    扁平的全路径清单，中间文件夹由条目**自动补出**（真树的层级本来就是这样
    隐含的），所以测试里只写叶子条目也行。
    """

    def __init__(self, st: State):
        self._st = st

    def _nodes(self) -> list[str]:
        """清单 + 各级祖先（保持清单里的先后次序）。"""
        out: list[str] = []
        for p in self._st.tree_items:
            parts = str(p).split("\\")
            for k in range(1, len(parts) + 1):
                anc = "\\".join(parts[:k])
                if anc not in out:
                    out.append(anc)
        return out

    def _children(self, parent: str) -> list[str]:
        want = parent + "\\" if parent else ""
        kids: list[str] = []
        for p in self._nodes():
            if not p.startswith(want):
                continue
            full = want + p[len(want):].split("\\", 1)[0]
            if full != parent and full not in kids:
                kids.append(full)
        return kids

    def GetFirstChildName(self, parent):
        if self._st.tree_error is not None:
            raise self._st.tree_error
        kids = self._children(str(parent))
        return kids[0] if kids else ""

    def GetNextItemName(self, item):
        if self._st.tree_error is not None:
            raise self._st.tree_error
        item = str(item)
        parent = item.rsplit("\\", 1)[0] if "\\" in item else ""
        kids = self._children(parent)
        try:
            i = kids.index(item)
        except ValueError:
            return ""
        return kids[i + 1] if i + 1 < len(kids) else ""


class Model3D:
    """假 ``prj.model3d``。"""

    def __init__(self, st: State):
        self._st = st
        self.history: list[tuple] = []
        self.solves = 0
        self.selected: list[str] = []
        #: 控制宏（``_execute_vba_code``）的调用原文——**不进历史表**，单记
        self.vba_calls: list[str] = []
        #: 工程里有没有结果。求解成一次就 True，DeleteResults 清成 False；
        #: 真机上"有结果 + 非静默 + 要重跑"就是那个等人点的确认框。
        #: 从磁盘**打开**的工程按 ``initial_has_results`` 预置（新建的肯定没有）
        self.has_results = False
        #: 最后一次 SelectTreeItem 是否选中了树上真实存在的条目（真库的
        #: 静默失效就是"False 但没人看"）
        self.selection_valid = False
        self.ASCIIExport = ASCIIExport(st, self)
        self.ResultTree = None if st.drop_result_tree else ResultTree(st)

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
        """**无参**——真库实测就是无参；多传参数这里会 TypeError。

        工程里还有上一轮的结果、会话又没静默时，真机是**弹模态框等人点**
        （GUI 下脚本就停在这行）：假库用异常代替"永远卡住"，否则这个坑在
        测试里根本没有出口。静默会话 = 自动点掉，继续跑。
        """
        st = self._st
        if st.solver_running:
            raise RuntimeError("求解器已在运行")
        if self.has_results and not st.quiet_mode:
            raise RuntimeError(
                "Existing MWS result need to be deleted and re-simulated to "
                "perform the simulation. Do you want to proceed?"
                "（模态框没人点：要么 set_quiet_mode(True)，要么先 "
                "DeleteResults）")
        st.events.append(("run_solver",))
        self.solves += 1
        if st.solve_error is not None:
            raise st.solve_error
        self.has_results = True

    def is_solver_running(self):
        return self._st.solver_running

    def _execute_vba_code(self, vba):
        """控制宏通道：执行 VBA 但**不进历史表**（真库的签名是代码字符串）。

        生产代码只用它跑 ``DeleteResults``；这里只认这一条，免得假库比真库
        宽容（真库不认的命令是**报错**，不是静默无效）。
        """
        st = self._st
        if st.control_vba_error is not None:
            raise st.control_vba_error
        code = str(vba)
        self.vba_calls.append(code)
        if "DeleteResults" not in code:
            raise RuntimeError(f"假库只实现了 DeleteResults，收到：{code!r}")
        self.has_results = False
        return True

    def SelectTreeItem(self, item):
        """选中结果树条目，**返回布尔**（条目在树上吗）——照真库的签名。

        条目不在树上时"静默不生效"（返回值是调用方唯一能看到的信号）。
        """
        if self._st.select_error is not None:
            raise self._st.select_error
        self.selected.append(item)
        self.selection_valid = item in self._st.tree_items
        return self.selection_valid


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
    def set_quiet_mode(flag=True):
        """会话级静默开关：模态框不弹、自动按默认按钮走。

        真库上**这个方法是会话状态**（附接 GUI 时会静默用户自己的窗口，
        只有重开 CST 能恢复）——假库把它存在 ``state.quiet_mode``，测试之间
        随 ``reset()`` 复位。老版本没有这个方法，用
        ``configure(has_quiet_mode_api=False)`` 模拟（生产代码必须容忍）。
        """
        if not _state.has_quiet_mode_api:
            raise AttributeError(
                "type object 'DesignEnvironment' has no attribute "
                "'set_quiet_mode'")
        if _state.quiet_mode_error is not None:
            raise _state.quiet_mode_error
        _state.events.append(("set_quiet_mode", bool(flag)))
        _state.quiet_mode = bool(flag)
        return True

    @staticmethod
    def in_quiet_mode():
        return _state.quiet_mode

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
        # 磁盘上的工程多半带着上一轮的结果——这正是确认框的触发条件
        prj.model3d.has_results = bool(_state.initial_has_results)
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
