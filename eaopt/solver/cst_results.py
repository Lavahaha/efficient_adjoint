"""从 CST 工程里把结果取出来：S 参数（``cst.results``）与场（ASCIIExport）。

为什么单独一层：读取逻辑有**两条独立的链**，且都要能单独定位问题——

1. **S 参数**：官方 ``cst.results``（``ProjectFile(path).get_3d()
   .get_result_item("1D Results\\S-Parameters\\S3,1")`` → ``get_xdata()`` /
   ``get_ydata()``）。它直接读磁盘上的结果文件，**要求工程已经存盘**；
   工程在 CST 里开着时要 ``allow_interactive=True``。
   读不到时的回退链是 VBA 对象模型上的 ``ResultTree``（``cst_api`` 里那套
   纯鸭子类型函数，见其模块头）——官方库与 COM 暴露的是同一套对象模型，
   所以那套"条目名会被加后缀"的经验在这里一样管用。
2. **场**：``cst.results`` **只支持 0D/1D 结果**，3D 场必须导出。走
   ``SelectTreeItem(条目)`` + ``ASCIIExport`` + ``Execute``（官方例程的
   写法，见 vba.ascii_export_params），再把导出的 ASCII 解析成 FieldGrid。

两条链都遵守同一条纪律：**读不到就抛**，绝不把 0 或猜测值塞进伴随法
（伴随梯度对场数据是线性的，一个 0 会静默污染整个速度场）。
"""

from __future__ import annotations

import numpy as np

from eaopt.adjoint.fields import FieldGrid
from eaopt.solver import cst_api
from eaopt.solver import vba as V
from eaopt.solver.ascii_fields import parse_ascii_field
from eaopt.solver.cst_session import load_cst, require_callable, try_calls

__all__ = [
    "S_PARAM_FOLDER", "s_param_item", "read_s_params",
    "s_params_via_result_tree", "export_field_grid", "crop_grid",
]

#: S 参数在结果树里的文件夹（单反斜杠：Python/COM 路径，不是 VBA 源码）。
S_PARAM_FOLDER = "1D Results\\S-Parameters"


def s_param_item(response_port: int, stimulus_port: int) -> str:
    """S 参数条目路径，如 ``S1,3`` = 端口 3 激励、端口 1 响应（CST 记法）。"""
    return f"{S_PARAM_FOLDER}\\S{int(response_port)},{int(stimulus_port)}"


# --------------------------------------------------------------------------- #
# S 参数
# --------------------------------------------------------------------------- #
def read_s_params(project_path, stimulus_port: int, response_ports=(1, 2, 3, 4),
                  freq_ghz: float = 5.0, *, allow_interactive: bool = True,
                  model3d=None, lib_dir=None, install_dir=None,
                  log=print) -> dict:
    """读 5 GHz 处的 ``S_{i,stimulus}``（i ∈ response_ports），线性插值。

    **必须先存盘**：``cst.results`` 读的是磁盘上的结果文件。工程在 CST
    里开着时用 ``allow_interactive=True``；没开（比如事后补读）用 False。

    ``model3d`` 给了就在官方库失败后回退到 ``ResultTree`` 链（工程正开着
    时才可用）。两条路都失败则抛 RuntimeError，把两条链的诊断都列出来
    ——"为什么没读到"的线索全在原始错误里（方法不存在 vs 条目不存在）。

    返回 ``{(响应端口, 激励端口): complex}``，与 ``Solution.s_params`` 同构。
    """
    wanted = [int(p) for p in response_ports]
    notes: list[str] = []
    out: dict = {}
    try:
        rm = _result_module(project_path, allow_interactive=allow_interactive,
                            lib_dir=lib_dir, install_dir=install_dir)
    except Exception as e:
        notes.append(f"cst.results 打开失败: {e}")
        rm = None
    if rm is not None:
        for i in wanted:
            path = s_param_item(i, stimulus_port)
            try:
                out[(i, int(stimulus_port))] = _read_one(rm, path, freq_ghz, notes)
                log(f"    [OK] cst.results: {path.rsplit(chr(92), 1)[-1]} "
                    f"@ {freq_ghz:g} GHz")
            except Exception as e:
                notes.append(f"{path}: {e}")
                break                               # 一条读不到就没必要继续试
        if len(out) == len(wanted):
            return out

    if model3d is not None:
        out2 = s_params_via_result_tree(model3d, stimulus_port, wanted,
                                        freq_ghz, notes, log=log)
        if len(out2) == len(wanted):
            return out2

    raise RuntimeError(
        f"读不到 S 参数（激励端口 {stimulus_port}，响应端口 {wanted}，"
        f"{freq_ghz:g} GHz）：\n  - " + "\n  - ".join(notes or ["无诊断信息"])
        + "\n  排查顺序：(1) 工程存盘了吗（cst.results 读的是磁盘结果）？"
          "(2) 求解真的跑完了吗？(3) 结果树里的条目名是不是带了后缀？"
    )


def _result_module(project_path, *, allow_interactive: bool,
                   lib_dir=None, install_dir=None):
    """``cst.results.ProjectFile(path, allow_interactive=...)`` → 3D 结果模块。

    ``allow_interactive`` 是官方文档里的形参；个别版本没有，故按有无依次试。
    """
    _cst, _iface, results = load_cst(install_dir=install_dir, lib_dir=lib_dir)
    pf = try_calls(results, [
        (f"ProjectFile({project_path}, allow_interactive={allow_interactive})",
         lambda: results.ProjectFile(str(project_path),
                                     allow_interactive=allow_interactive)),
        (f"ProjectFile({project_path})",
         lambda: results.ProjectFile(str(project_path))),
    ], what=f"打开结果文件 {project_path}")
    fn = getattr(pf, "get_3d", None)
    if not callable(fn):
        raise RuntimeError(
            f"ProjectFile 返回的对象没有 get_3d()（本机 API 与预期不符）："
            f"{_members(pf)}")
    return fn()


def _read_one(rm, path: str, freq_ghz: float, notes: list[str]) -> complex:
    """取单条曲线在 freq_ghz 的值（复数）。"""
    item = _open_item(rm, path, notes)
    if item is None:
        raise RuntimeError("结果条目不存在（见 notes）")
    x = np.asarray(item.get_xdata(), dtype=float).ravel()
    y = np.asarray(item.get_ydata()).ravel()
    if x.size < 2 or y.size != x.size:
        raise RuntimeError(f"曲线数据长度不对：x={x.size}, y={y.size}")
    if not (x.min() <= freq_ghz <= x.max()):
        notes.append(f"警告: {freq_ghz:g} GHz 超出数据范围 "
                     f"[{x.min():g}, {x.max():g}]，插值会被端点截断")
    y = y.astype(complex)
    v = complex(float(np.interp(freq_ghz, x, y.real)),
                float(np.interp(freq_ghz, x, y.imag)))
    if abs(v) > 1.5:
        notes.append(f"可疑: |S|={abs(v):.3g} 远大于 1，可能读错了条目")
    return v


def _open_item(rm, path: str, notes: list[str]):
    """按路径取结果条目；取不到就按**叶子名前缀**在结果树里找真实条目。

    保留 COM 时代的经验：CST 会给结果条目自动加后缀（``S1,1`` 在结果树
    里可能是 ``S1,1 [run 1]``、场监视器则是 ``e-field (f=5) [AC]``），
    所以**不要拿名字硬拼路径**。
    """
    get = getattr(rm, "get_result_item", None)
    if not callable(get):
        notes.append(f"ResultModule 没有 get_result_item：{_members(rm)}")
        return None
    try:
        return get(path)
    except Exception as e:
        notes.append(f"get_result_item({path!r}): {e}")
    folder, _, leaf = path.rpartition("\\")
    for cand in _tree_items(rm, folder, notes):
        if cand.rsplit("\\", 1)[-1].startswith(leaf):
            try:
                item = get(cand)
                notes.append(f"按前缀找到真实条目 {cand!r}")
                return item
            except Exception as e:
                notes.append(f"get_result_item({cand!r}): {e}")
    return None


def _tree_items(rm, folder: str, notes: list[str]) -> list[str]:
    """``ResultModule.get_tree_items`` 的两种调用形态（有/无 folder）。"""
    fn = getattr(rm, "get_tree_items", None)
    if not callable(fn):
        notes.append("ResultModule 没有 get_tree_items")
        return []
    for label, call in ((f"get_tree_items({folder!r})", lambda: fn(folder)),
                        ("get_tree_items()", lambda: fn())):
        try:
            items = [str(x) for x in call()]
        except Exception as e:
            notes.append(f"{label}: {e}")
            continue
        if folder:
            sub = [x for x in items if x.startswith(folder)]
            if sub:
                return sub
        return items
    return []


def s_params_via_result_tree(model3d, stimulus_port: int, response_ports,
                             freq_ghz: float, notes: list[str] | None = None,
                             log=print) -> dict:
    """回退链：直接问 CST 活工程的 ``ResultTree``（``cst_api`` 那套）。

    只在工程**正开在 CST 里**时可用（``model3d`` 是活对象）。与
    ``read_s_params`` 返回同构的字典；任一条读不到就返回已读到的部分，
    由调用方判断是否够用。
    """
    notes = [] if notes is None else notes
    rt = getattr(model3d, "ResultTree", None)
    if rt is None:
        notes.append("model3d 上没有 ResultTree")
        return {}
    out: dict = {}
    for i in response_ports:
        local: list[str] = []
        path = (cst_api.find_item(rt, S_PARAM_FOLDER, f"S{i},{stimulus_port}",
                                  local)
                or s_param_item(i, stimulus_port))
        v = cst_api.s_param_at(rt, path, freq_ghz, local, project=model3d)
        if v is None:
            notes.append(f"ResultTree 读 S{i},{stimulus_port}（{path}）失败：" +
                         "；".join(local or ["无诊断"]))
            return {}
        log(f"    [OK] ResultTree: S{i},{stimulus_port} @ {freq_ghz:g} GHz")
        out[(int(i), int(stimulus_port))] = complex(v)
    return out


# --------------------------------------------------------------------------- #
# 场
# --------------------------------------------------------------------------- #
def export_field_grid(model3d, field_type: str, freq_ghz: float, step_mm: float,
                      out_path, *, notes: list[str] | None = None,
                      log=print) -> FieldGrid:
    """导出场监视器的结果并解析成 FieldGrid（单位 mm / V·m⁻¹ 或 A·m⁻¹）。

    流程 = 选中结果条目 → ``ASCIIExport`` 设步长 → ``Execute`` → 解析。
    全程**即时执行、不进历史表**（后处理命令本来就不该进历史表）。

    step_mm 是各轴的采样步长（mm）：取 ``sampling.point_spacing_mm``
    （0.2）量级即可，别用网格步长（0.05）——导出点数会放大 64 倍。
    导出范围是整个监视器（Volume => 整个计算域），所以调用方拿到后要用
    :func:`crop_grid` 裁到设计区。
    """
    notes = [] if notes is None else notes
    item = _field_item_path(model3d, field_type, freq_ghz, notes)
    select = require_callable(model3d, "SelectTreeItem", f"选中 {field_type} 结果")
    try:
        select(item)
    except Exception as e:
        raise RuntimeError(
            f"SelectTreeItem({item!r}) 失败：{e}\n  "
            + "；".join(notes or ["无诊断"])
            + "\n  排查：条目名对不上（监视器名 → 结果条目的映射见 "
              "vba.field_result_path 与 vba.field_monitor_name）。") from e

    exporter = getattr(model3d, "ASCIIExport", None)
    if exporter is None:
        raise RuntimeError(f"model3d 上没有 ASCIIExport：{_members(model3d)}")
    _configure_and_run(exporter, out_path, step_mm, field_type)

    data, axes = parse_ascii_field(str(out_path))
    grid = FieldGrid(origin=tuple(float(a[0]) for a in axes),
                     spacing=_spacing(axes, out_path), data=data)
    log(f"    [OK] {field_type} 场导出：{item} → {data.shape} "
        f"@ ({grid.origin[0]:g}, {grid.origin[1]:g}, {grid.origin[2]:g}) mm，"
        f"步长 {grid.spacing[0]:g} mm")
    return grid


def _spacing(axes, out_path) -> tuple:
    """由坐标轴推步长；**某个轴只有一层时必须报错**。

    单层轴上"步长"没有定义，硬塞 0 会让 FieldGrid 的插值器退化（以后每个
    采样点都取到同一个值，而且看不出错）。正常不会出现——场监视器是 Volume
    监视器，三个方向都覆盖整个计算域——所以这基本只在"导出设置/文件格式与
    预期不符"时触发，那就该把话说清楚。
    """
    out = []
    for name, a in zip("xyz", axes):
        if np.size(a) < 2:
            raise RuntimeError(
                f"场导出文件 {out_path} 的 {name} 轴只有 {np.size(a)} 个采样点，"
                f"步长无定义。文件头应为 'x0 x1 nx' 三行、Mode 应为 FixedWidth；"
                f"当前解析出 {name} 轴 = {np.asarray(a).tolist()}。")
        out.append(float(a[1] - a[0]))
    return tuple(out)


def _configure_and_run(exporter, out_path, step_mm: float,
                       field_type: str) -> None:
    """逐条设置 ASCIIExport 并执行（属性集来自 vba.ascii_export_params）。"""
    reset = getattr(exporter, "Reset", None)
    if callable(reset):
        reset()                                     # 清掉上一次的残留设置
    setter = {"FileName": str(out_path)}
    setter.update({prop: val for prop, val in V.ascii_export_params(step_mm)})
    for prop, val in setter.items():
        fn = getattr(exporter, prop, None)
        if not callable(fn):
            raise RuntimeError(
                f"ASCIIExport 没有 {prop}（CST 2024 实测可用的只有 "
                f"Reset/FileName/Mode/StepX/StepY/StepZ/Execute）："
                f"{_members(exporter)}")
        try:
            fn(val)
        except Exception as e:
            raise RuntimeError(f"ASCIIExport.{prop}({val!r}) 失败：{e}") from e
    execute = getattr(exporter, V.ASCII_EXPORT_EXECUTE, None)
    if not callable(execute):
        raise RuntimeError(f"ASCIIExport 没有 {V.ASCII_EXPORT_EXECUTE}："
                           f"{_members(exporter)}")
    try:
        execute()
    except Exception as e:
        raise RuntimeError(
            f"{field_type} 场导出 Execute 失败：{e}\n"
            "  CST 报 'not available for the current view' 表示选中的条目"
            "不是可导出的场结果（条目名对不上）。") from e


def _field_item_path(model3d, field_type: str, freq_ghz: float,
                     notes: list[str]) -> str:
    """结果树里该监视器的真实条目路径。

    先在结果树里按监视器名找（CST 会加 ``[AC]`` 之类的后缀，监视器名与
    条目名不是一回事），找不到才退回按惯例拼的路径。
    """
    folder = f"2D/3D Results\\{V.FIELD_TYPES[field_type][1]}"
    name = V.field_monitor_name(field_type, freq_ghz)
    rt = getattr(model3d, "ResultTree", None)
    if rt is not None:
        found = cst_api.find_item(rt, folder, name, notes)
        if found:
            return found
    fallback = V.field_result_path(field_type, freq_ghz)
    notes.append(f"结果树里没找到以 {name!r} 开头的条目，按惯例拼：{fallback}")
    return fallback


def crop_grid(grid: FieldGrid, box, margin_mm: float = 0.0) -> FieldGrid:
    """把场网格裁到 ``box``（``.x``/``.y`` 各一对 mm 边界）外扩 margin_mm。

    导出的是监视器的整个包围盒（Volume 监视器 = 整个计算域），而伴随法
    只在设计区内部及其近旁用得到场——裁剪把内存与后续插值开销按面积比例
    降下来（全计算域 → 设计区通常小一个数量级）。

    规则网格上落在范围内的坐标是连续的，所以直接切片，不做插值。
    z 方向不裁：监视器是 Volume 监视器，但 z 只有几十层（xy 才是大头），
    而且采样平面 field_z_mm 未必在 z 的中心。
    """
    axis = grid.axes()
    ix = _index_range(axis[0], box.x[0] - margin_mm, box.x[1] + margin_mm)
    iy = _index_range(axis[1], box.y[0] - margin_mm, box.y[1] + margin_mm)
    if ix is None or iy is None:
        raise ValueError(
            f"设计区 {box.x}×{box.y} mm（余量 {margin_mm} mm）与场网格不相交："
            f"网格 x∈[{axis[0][0]:g},{axis[0][-1]:g}] "
            f"y∈[{axis[1][0]:g},{axis[1][-1]:g}] ——"
            " 多半是导出范围/坐标系与配置对不上。")
    data = grid.data[ix[0]: ix[-1] + 1, iy[0]: iy[-1] + 1, :]
    return FieldGrid(
        origin=(float(axis[0][ix[0]]), float(axis[1][iy[0]]), float(axis[2][0])),
        spacing=grid.spacing,
        data=data,
    )


def _index_range(coord: np.ndarray, lo: float, hi: float):
    """coord 中落在 [lo, hi] 的下标范围；一个都没有返回 None。"""
    sel = np.nonzero((coord >= lo - 1e-9) & (coord <= hi + 1e-9))[0]
    return sel if sel.size else None


def _members(obj, limit: int = 60) -> str:
    try:
        names = sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception:                               # pragma: no cover - 兜底
        return "<取不到成员表>"
    head = ", ".join(names[:limit])
    return head + (f" …（共 {len(names)} 个）" if len(names) > limit else "")
