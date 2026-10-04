"""结果提取：S 参数与 E/H 场（顶层直接 ``import cst.results``）。

这里**不是** CST API 的包装层——CST 官方库只在该用的地方直接 import
（三个 ``scripts/cst_*.py`` 与 ``eaopt/solver/cst.py``）。本模块只把"取数的
规则"集中一处，免得每个程序各写一份，规则有两条：

1. **S 参数**：``cst.results.ProjectFile(path, allow_interactive=True)
   .get_3d().get_result_item(条目).get_xdata()/get_ydata()``（服务器实测的
   直路）。它读的是**磁盘上的结果文件**——所以纪律是"求解 → 存盘 → 读"，
   没存盘时读到的要么是旧结果要么直接报错。
2. **场**：``cst.results`` 只支持 0D/1D 结果，3D 场必须导出——
   ``SelectTreeItem(条目)`` + ``ASCIIExport`` + ``Execute``（属性集见
   ``cst_model.ascii_export_params``），再把 ASCII 解析成 ``FieldGrid``。
   ASCIIExport 没有区域范围属性，导出的是整个包围盒（Volume 监视器 =
   整个计算域），所以要用 :func:`crop_grid` 裁到设计区 ± 余量。
   条目名由 CST 自己起（可能带 `` [AC]`` 这类后缀），**导出前先到活结果树
   上按叶子名找真实路径**（:func:`resolve_field_item`）——``SelectTreeItem``
   对不存在的路径既不抛错也不生效，拼错路径的代价是后面那句指不到真因的
   "The ASCII export option is not available for the current view."。

贯穿全篇的纪律：**读不到就抛**，绝不把 0 或猜测值塞进伴随法（伴随梯度对
场数据是线性的，一个 0 会静默污染整个速度场）。
"""

from __future__ import annotations

import numpy as np

import cst.results as cstr

from eaopt.adjoint.fields import FieldGrid
from eaopt.solver import cst_model as M

__all__ = [
    "S_PARAM_FOLDER", "s_param_item", "read_s_params",
    "resolve_field_item",
    "export_field_grid", "export_field_cropped", "crop_grid",
    "parse_ascii_field",
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
                  log=print) -> dict:
    """读 ``freq_ghz`` 处的 ``S_{i,激励端口}``（i ∈ response_ports），线性插值。

    **必须先存盘**：``cst.results`` 读的是磁盘上的结果文件。工程在 CST
    里开着时用 ``allow_interactive=True``；没开（比如事后补读）用 False。

    返回 ``{(响应端口, 激励端口): complex}``，与 ``Solution.s_params`` 同构。
    任何一条读不到都抛 RuntimeError，且把原始错误完整带出来——"为什么没
    读到"的线索全在里面。
    """
    wanted = [int(p) for p in response_ports]
    stim = int(stimulus_port)
    try:
        rm = _result_module(project_path, allow_interactive)
    except Exception as e:
        raise RuntimeError(_why_not(project_path, stim, wanted, freq_ghz,
                                    [f"打开结果文件失败: {e}"])) from e

    out: dict = {}
    notes: list[str] = []
    for i in wanted:
        path = s_param_item(i, stim)
        try:
            out[(i, stim)] = _read_curve(rm, path, freq_ghz, notes, log)
        except Exception as e:
            notes.append(f"{path}: {e}")
            break                       # 一条读不到就没必要继续试
        leaf = path.rsplit("\\", 1)[-1]
        log(f"    [OK] cst.results: {leaf} @ {freq_ghz:g} GHz")
    if len(out) != len(wanted):
        raise RuntimeError(_why_not(project_path, stim, wanted, freq_ghz, notes))
    return out


def _why_not(project_path, stimulus_port: int, response_ports, freq_ghz: float,
             notes: list[str]) -> str:
    return (
        f"读不到 S 参数（工程 {project_path}，激励端口 {stimulus_port}，"
        f"响应端口 {response_ports}，{freq_ghz:g} GHz）：\n  - "
        + "\n  - ".join(notes or ["无诊断信息"])
        + "\n  排查顺序：(1) 工程存盘了吗（cst.results 读的是磁盘结果）？"
          "(2) 求解真的跑完了吗？(3) 结果条目名对得上吗（带后缀时已按叶子名"
          "重试过一次）？"
    )


def _result_module(project_path, allow_interactive: bool):
    """``ProjectFile(path, allow_interactive=...)`` → 3D 结果模块。

    官方库直路，不做"形参有没有"的候选链：探针（服务器 2026-10-02）实测
    ``allow_interactive`` 存在；真装不了就该在 import 处报错，而不是在这里
    悄悄换一条读法。
    """
    pf = cstr.ProjectFile(str(project_path), allow_interactive=allow_interactive)
    get3d = getattr(pf, "get_3d", None)
    if not callable(get3d):
        raise RuntimeError(f"ProjectFile 返回的对象没有 get_3d()：{_members(pf)}")
    return get3d()


def _read_curve(rm, path: str, freq_ghz: float, notes: list[str],
                log=print) -> complex:
    """取单条曲线在 freq_ghz 的值（复数）。"""
    item = _get_item(rm, path, notes)
    x = np.asarray(item.get_xdata(), dtype=float).ravel()
    y = np.asarray(item.get_ydata()).ravel()
    if x.size < 2 or y.size != x.size:
        raise RuntimeError(f"曲线数据长度不对：x={x.size}, y={y.size}")
    if not (x.min() <= freq_ghz <= x.max()):
        log(f"    [警告] {freq_ghz:g} GHz 超出数据范围 "
            f"[{x.min():g}, {x.max():g}]，插值会被端点截断")
    y = y.astype(complex)
    v = complex(float(np.interp(freq_ghz, x, y.real)),
                float(np.interp(freq_ghz, x, y.imag)))
    if abs(v) > 1.5:
        log(f"    [警告] |S{path.rsplit(chr(92), 1)[-1]}|={abs(v):.3g} 远大于 1，"
            "可能读错了条目")
    return v


def _get_item(rm, path: str, notes: list[str]):
    """取结果条目：先按全路径，失败后用**叶子名**再试一次。

    CST 会给结果条目自动加后缀（``S1,1`` 在结果树里可能是 ``S1,1 [run 1]``），
    这时全路径匹配会失败。只重试这一次、只换名字不换条目（叶子名交给 CST
    自己解析）——不再枚举结果树："找一条能读的"那种候选链只会掩盖主路的失败。
    """
    get = getattr(rm, "get_result_item", None)
    if not callable(get):
        raise RuntimeError(f"ResultModule 没有 get_result_item：{_members(rm)}")
    try:
        return get(path)
    except Exception as e:
        notes.append(f"get_result_item({path!r}): {e}")
    leaf = path.rsplit("\\", 1)[-1]
    try:
        item = get(leaf)
    except Exception as e:
        notes.append(f"get_result_item({leaf!r}): {e}")
        raise RuntimeError(f"结果条目读不到（{path!r} / {leaf!r}）") from e
    notes.append(f"全路径读不到，改用叶子名 {leaf!r} 命中（条目名带后缀？）")
    return item


# --------------------------------------------------------------------------- #
# 场条目定位（活结果树）
# --------------------------------------------------------------------------- #
class _TreeUnavailable(Exception):
    """结果树枚举不可用（拿不到 ResultTree / 这个版本没有遍历方法）。"""


def resolve_field_item(model3d, field_type: str, freq_ghz: float, *,
                       log=print) -> str:
    """求出该场在结果树里**真实存在**的条目路径。

    条目名是 CST 自己起的：同一个监视器可以叫 ``e-field (f=5)``，也可能带
    `` [AC]`` / ``[run 1]`` 之类后缀（``cst_model.field_result_path`` 拼的
    那份后缀只是**猜测**，服务器 2026-10-04 就折在这上面）。所以这里走
    CST VBA 的结果树协议（``ResultTree.GetFirstChildName`` /
    ``GetNextItemName``，刀口在 **2D/3D Results** 子树）按叶子名认领：
    先找完全同名的，再找同名前缀的（容忍后缀），命中多个取叶子名最短的
    （后缀最少 = 最可能是主结果）。

    树上确实没有 → 抛 RuntimeError，并把树里的条目**全部列出来**（对拍用）；
    枚举不可用 → 退回 ``cst_model.field_result_path`` 的惯例路径并告警，
    选不中会由 :func:`_select_item` 当场拦下（不会静默导出错东西）。
    """
    want = M.field_monitor_name(field_type, freq_ghz)
    fallback = M.field_result_path(field_type, freq_ghz)
    try:
        items = _tree_items(model3d)
    except _TreeUnavailable as e:
        log(f"    [警告] 结果树枚举不可用（{e}），按惯例路径选中；条目名若带"
            "后缀会立刻报错，不会静默导出")
        return fallback
    cands = [p for p in items if _leaf(p).startswith(want)]
    if not cands:
        raise RuntimeError(
            f"结果树里没有 {field_type} 的条目（期望叶子名 {want!r}；"
            f"惯例路径 {fallback!r}）。\n"
            + _tree_note(model3d)
            + "\n  排查：监视器建了吗（Modeling 树 → Field Monitors，名字由"
              " cst_model.field_monitor_name 决定）、求解跑完了吗、频点对得"
              f"上吗（{freq_ghz:g} GHz）。")
    exact = [p for p in cands if _leaf(p) == want]
    pick = sorted(exact or cands, key=lambda p: (len(_leaf(p)), p))[0]
    if pick != fallback:
        others = [p for p in cands if p != pick]
        log(f"    [ .. ] 场条目按实际树路径选中：{pick}"
            + (f"（同名前缀另有 {others}）" if others else ""))
    return pick


def _tree_items(model3d, root: str = "2D/3D Results") -> list[str]:
    """枚举 ``root`` 子树下的全部条目（深度优先，返回全路径）。

    用 CST 的 VBA 协议：``GetFirstChildName(父路径)`` 给第一个子条目
    （没有则空串），``GetNextItemName(条目)`` 给下一个同层条目（空串收尾），
    两者给的都是**全路径**。root 下为空时再枚举整棵树（有些状态只给部分
    层级）。任何版本差异/异常都收成 :class:`_TreeUnavailable`，由调用方
    决定退路——这里不做"多试几种写法"的候选链。
    """
    tree = getattr(model3d, "ResultTree", None)
    if tree is None:
        raise _TreeUnavailable("model3d 上没有 ResultTree")
    first = getattr(tree, "GetFirstChildName", None)
    nxt = getattr(tree, "GetNextItemName", None)
    if not callable(first) or not callable(nxt):
        raise _TreeUnavailable(
            f"ResultTree 没有 GetFirstChildName/GetNextItemName：{_members(tree)}")
    out: list[str] = []
    try:
        _walk(first, nxt, root, out, set())
        if not out and root:
            _walk(first, nxt, "", out, set())
    except Exception as e:
        raise _TreeUnavailable(f"枚举结果树失败：{e}") from e
    return out


def _walk(first, nxt, parent: str, out: list[str], seen: set) -> None:
    """把 parent 的子条目递归收进 out（seen 防环、限长防呆）。"""
    child = first(parent)
    while isinstance(child, str) and child:
        if child in seen or len(out) >= 5000:
            return
        seen.add(child)
        out.append(child)
        _walk(first, nxt, child, out, seen)
        child = nxt(child)


def _leaf(path: str) -> str:
    """条目路径的叶子名（``A\\B\\C`` → ``C``）。"""
    return path.rsplit("\\", 1)[-1]


def _tree_note(model3d) -> str:
    """出错时附上"结果树里到底有什么"（整棵树；枚举不了就说明原因）。"""
    try:
        items = _tree_items(model3d, root="")
    except _TreeUnavailable as e:
        return f"  （结果树枚举不可用：{e}）"
    if not items:
        return "  结果树是空的——求解还没跑完，或结果没落盘。"
    return "  结果树里的条目：\n" + "\n".join(f"    - {p}" for p in items)


def _select_item(model3d, item: str) -> None:
    """选中结果树条目；**返回 False（条目不存在）与抛异常一样是硬错误**。

    ``SelectTreeItem`` 的返回值就是"这条条目在树上吗"——不看它，路径写错
    会一路滑到 ``ASCIIExport.Execute`` 才炸，而那句 "not available for the
    current view" 指不到真因。
    """
    select = getattr(model3d, "SelectTreeItem", None)
    if not callable(select):
        raise RuntimeError(f"model3d 上没有 SelectTreeItem：{_members(model3d)}")
    try:
        ok = select(item)
    except Exception as e:
        raise RuntimeError(
            f"SelectTreeItem({item!r}) 失败：{e}\n" + _tree_note(model3d)) from e
    if ok is False:
        raise RuntimeError(
            f"SelectTreeItem({item!r}) 返回 False：结果树里没有这条条目。\n"
            + _tree_note(model3d))


# --------------------------------------------------------------------------- #
# 场
# --------------------------------------------------------------------------- #
def export_field_grid(model3d, field_type: str, freq_ghz: float, step_mm: float,
                      out_path, *, log=print) -> FieldGrid:
    """导出场监视器的结果并解析成 FieldGrid（单位 mm / V·m⁻¹ 或 A·m⁻¹）。

    流程 = 选中结果条目 → ``ASCIIExport`` 设步长 → ``Execute`` → 解析。
    全程**即时执行、不进历史表**（后处理命令本来就不该进历史表）。

    step_mm 是各轴的采样步长（mm）：取 ``sampling.point_spacing_mm``
    （0.2）量级即可，别用网格步长（0.05）——导出点数会放大 64 倍。
    导出范围是整个监视器（Volume => 整个计算域），所以调用方拿到后要用
    :func:`crop_grid` 裁到设计区。
    """
    item = resolve_field_item(model3d, field_type, freq_ghz, log=log)
    _select_item(model3d, item)

    exporter = getattr(model3d, "ASCIIExport", None)
    if exporter is None:
        raise RuntimeError(f"model3d 上没有 ASCIIExport：{_members(model3d)}")
    _configure_and_run(exporter, out_path, step_mm, field_type, item)

    data, axes = parse_ascii_field(str(out_path))
    grid = FieldGrid(origin=tuple(float(a[0]) for a in axes),
                     spacing=_spacing(axes, out_path), data=data)
    log(f"    [OK] {field_type} 场导出：{item} → {data.shape} "
        f"@ ({grid.origin[0]:g}, {grid.origin[1]:g}, {grid.origin[2]:g}) mm，"
        f"步长 {grid.spacing[0]:g} mm")
    return grid


def export_field_cropped(model3d, field_type: str, freq_ghz: float, step_mm: float,
                         out_path, box, margin_mm: float = 0.0, *,
                         log=print) -> FieldGrid:
    """导出该场 → 裁到设计区 ``box``（``.x``/``.y``）± ``margin_mm``。

    三个 CST 程序都要做这个动作：整域导出的网格没有保留价值（点数按面积
    放大一个量级，而伴随法只在设计区近旁用得到场），所以导出即裁剪，只把
    裁剪后的 ``FieldGrid`` 交出去。
    """
    grid = export_field_grid(model3d, field_type, freq_ghz, step_mm, out_path,
                             log=log)
    cut = crop_grid(grid, box, margin_mm)
    log(f"    [OK] {field_type} 裁剪：{grid.data.shape} → {cut.data.shape}"
        f"（设计区 ±{margin_mm:g} mm）")
    return cut


def _configure_and_run(exporter, out_path, step_mm: float,
                       field_type: str, item: str) -> None:
    """逐条设置 ASCIIExport 并执行（属性集来自 cst_model.ascii_export_params）。"""
    reset = getattr(exporter, "Reset", None)
    if callable(reset):
        reset()                                     # 清掉上一次的残留设置
    for prop, val in [("FileName", str(out_path))] + list(
            M.ascii_export_params(step_mm)):
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
    execute = getattr(exporter, M.ASCII_EXPORT_EXECUTE, None)
    if not callable(execute):
        raise RuntimeError(f"ASCIIExport 没有 {M.ASCII_EXPORT_EXECUTE}："
                           f"{_members(exporter)}")
    try:
        execute()
    except Exception as e:
        raise RuntimeError(
            f"{field_type} 场导出 Execute 失败：{e}\n"
            f"  已选中的条目：{item}（在结果树上已确认存在）\n"
            "  CST 报 'not available for the current view' 表示当前视图不是"
            "可导出的场结果，按可能性排查：\n"
            "  ① 该监视器还没有结果数据（求解没跑完/没存盘）；\n"
            "  ② CST 处于网格视图或 2D 标量图视图（切回 3D 结果视图再试）；\n"
            "  ③ 工程窗口未激活（被别的工程/对话框挡住）。") from e


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


# --------------------------------------------------------------------------- #
# ASCII 场文件解析（CST ASCIIExport 的输出格式）
# --------------------------------------------------------------------------- #
def parse_ascii_field(path: str) -> tuple[np.ndarray, tuple]:
    """解析 CST ASCII 场文件（容错）。

    格式：``%`` 开头的注释行跳过；网格三行 ``x0 x1 nx`` / ``y0 y1 ny`` /
    ``z0 z1 nz``；其余数值按分量块排列（实部/虚部成对，见 :func:`_parse_blocks`）。

    返回 ``(data, axes)``：data 为 (nx, ny, nz, 3) complex（E: V/m 或
    H: A/m），axes 为各轴坐标（mm）。文件格式与预期不符时抛 ValueError
    ——宁可不解析，也不给出一份错坐标的场。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = [ln.strip() for ln in f
                 if ln.strip() and not ln.lstrip().startswith("%")]

    def _grid_line(i: int):
        parts = lines[i].replace(",", " ").split()
        return float(parts[0]), float(parts[1]), int(parts[2])

    x0, x1, nx = _grid_line(0)
    y0, y1, ny = _grid_line(1)
    z0, z1, nz = _grid_line(2)
    vals: list[float] = []
    for ln in lines[3:]:
        vals.extend(float(v) for v in ln.replace(",", " ").split())
    arr = np.asarray(vals, dtype=float)

    per_comp = nx * ny * nz
    if arr.size < per_comp * 3:
        raise ValueError(
            f"数值不足: {arr.size} < 3×{per_comp}（文件与 CST ASCIIExport 的"
            "格式不符：应为百分比注释 + 'x0 x1 nx' 三行 + 3/6 个分量块）")
    if arr.size >= per_comp * 6:                    # 复数导出：6 个块
        data = _parse_blocks(arr[: per_comp * 6], per_comp, nx, ny, nz)
    else:                                           # 实数导出：3 个块
        data = _parse_blocks(arr[: per_comp * 3], per_comp, nx, ny, nz).real \
            .astype(complex)
    axes = (
        np.linspace(x0, x1, nx),
        np.linspace(y0, y1, ny),
        np.linspace(z0, z1, nz),
    )
    return data, axes


def _parse_blocks(vals: np.ndarray, per_comp: int,
                  nx: int, ny: int, nz: int) -> np.ndarray:
    """把数值块装配成 (nx,ny,nz,3) complex。

    布局 A：每分量先全 Re 后全 Im（块顺序 Re_x,Re_y,Re_z,Im_x,Im_y,Im_z）
    ——CST 2024 实测的布局。列优先（``order="F"``，x 变的最快）。
    """
    n = len(vals)
    data = np.zeros((nx, ny, nz, 3), dtype=complex)
    if n == per_comp * 6:
        re = [vals[k * per_comp: (k + 1) * per_comp] for k in range(3)]
        im = [vals[(3 + k) * per_comp: (4 + k) * per_comp] for k in range(3)]
        for c in range(3):
            data[..., c] = (re[c] + 1j * im[c]).reshape(nx, ny, nz, order="F")
    elif n == per_comp * 3:
        for c in range(3):
            data[..., c] = vals[c * per_comp: (c + 1) * per_comp].reshape(
                nx, ny, nz, order="F")
    else:
        raise ValueError(f"无法识别的块数 {n}/{per_comp}")
    return data


# --------------------------------------------------------------------------- #
# 裁剪
# --------------------------------------------------------------------------- #
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
