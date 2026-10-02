"""CST 官方 Python API（``cst.interface`` / ``cst.results``）的唯一入口。

为什么有这个模块：以前我们用 COM + pywin32 手工模拟 CST 的脚本宿主
（``AddToHistory`` 往历史表里写模型、COM 属性求解/导出），调用形状全靠猜，
在服务器上反复失败。CST 2024 自带官方 Python 库，把"起实例/建工程/建模/
求解/读结果"都封成了稳定接口，本模块就是它在我们代码里的**唯一**接触面。

**本模块是唯一 import ``cst`` 的地方，而且是延迟导入**——没装 CST 的机器上
``import eaopt.solver.cst_session`` 必须照常成功，否则本地测试与 mock 路径全
跑不了。所有真机调用都发生在函数体内部。

遮蔽陷阱（本仓库特有，已实测复现）
----------------------------------
仓库根有个 ``cst/`` 目录（我们放 CST 宏的），``.gitignore`` 忽略、无
``__init__.py``。当仓库根被放进 ``sys.path``（editable 安装的 ``.pth``、
``python -c``、``python -m pytest`` 的 CWD……）时，``import cst`` 会命中
**PEP 420 命名空间包**：:

    ModuleSpec(name='cst', loader=None, origin=None,
               submodule_search_locations=NamespacePath(['<repo>\\cst']))

导入"成功"却没有任何代码，随后 ``import cst.interface`` 报
ModuleNotFoundError，而错误信息**完全不指向真因**。所以 ``load_cst()``
导入前会把 ``sys.modules`` 里这种空壳清掉，导入后再校验 ``cst.__file__``。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from eaopt.solver import vba as V

__all__ = [
    "CstImportError",
    "CST_LIB_SUBPATH",
    "REPO_ROOT",
    "cst_library_candidates",
    "load_cst",
    "describe_import_state",
    # 会话 / 工程 / 建模 / 求解
    "try_calls",
    "require_callable",
    "running_design_environments",
    "ensure_environment",
    "close_environment",
    "new_project",
    "open_projects",
    "project_filename",
    "open_or_reuse_project",
    "save_project",
    "apply_blocks",
    "update_design_region",
    "run_solver",
    "describe_environment",
]

#: 仓库根（``eaopt/solver/cst_session.py`` 往上三层）。
#: 用来识别"import 到的 cst 其实是我们仓库里的宏目录"这种遮蔽。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: CST 安装目录下 Python 库的相对路径（含 ``cst`` 子包与 ``.pyd``）。
CST_LIB_SUBPATH = Path("AMD64") / "python_cst_libraries"

#: 常见安装根（自动探测用；找不到就靠用户显式给）。
_INSTALL_GLOBS = (
    "C:/Program Files/CST Studio Suite *",
    "C:/Program Files (x86)/CST Studio Suite *",
    "C:/Program Files/Dassault Systemes/CST Studio Suite *",
    "D:/Program Files/CST Studio Suite *",
    # 服务器上的安装是 `D:\CST 2024`（既不在 Program Files 下、名字里也没有
    # "Studio Suite"），所以还要扫各盘根目录的 `CST *`。
    "C:/CST *", "D:/CST *", "E:/CST *",
    "C:/CST Studio Suite *", "D:/CST Studio Suite *",
)

_ENV_VARS = ("CST_PYTHON_LIBS", "CST_INSTALL_DIR")


class CstImportError(RuntimeError):
    """``import cst`` 失败——区分"没装/路径不对/被仓库 ``cst/`` 目录遮蔽"。"""


def cst_library_candidates(install_dir=None, extra=()) -> list[Path]:
    """候选的 CST Python 库目录（``<CST>/AMD64/python_cst_libraries``）。

    顺序 = 优先级：显式给的（配置/命令行）→ 环境变量 → 常见安装根扫描。
    **只返回真实存在的目录**（调用方拿到空表就知道"得让用户指路"）。
    """
    import glob

    out: list[Path] = []

    def _add(p):
        if p is None:
            return
        p = Path(p)
        # 允许直接给安装根、或直接给库目录，两种都认。**优先**安装根下的
        # 库子目录——把安装根本身也塞进 sys.path 是没用的（它里面没有
        # cst 包），还会把 sys.path 搞脏。
        sub = p / CST_LIB_SUBPATH
        cand = sub if sub.is_dir() else (p if p.is_dir() else None)
        if cand is not None and cand not in out:
            out.append(cand)

    _add(install_dir)
    for var in _ENV_VARS:
        _add(os.environ.get(var))
    # CST 安装时会往解释器里放一个 .pth，把库目录直接加进 sys.path——服务器上
    # 就是这样（`sys.path[1] = D:\CST 2024\AMD64\python_cst_libraries`），
    # 而我们的安装根扫描一个都没命中。回头看一眼 sys.path 最省事。
    for p in list(sys.path):
        if p.endswith(("python_cst_libraries", "python_cst_libraries\\",
                       "python_cst_libraries/")):
            _add(p)
    roots: list[str] = []
    for pattern in _INSTALL_GLOBS:
        roots.extend(sorted(glob.glob(pattern), reverse=True))  # 新版本优先
    for root in roots:
        _add(root)
    for p in extra:
        _add(p)
    return out


def _drop_namespace_shadow(name: str) -> str | None:
    """清掉 ``sys.modules`` 里同名的**空壳命名空间包**，返回被清掉对象的 ``__file__``。

    仓库根的 ``cst/`` 目录会被 PEP 420 当成命名空间包（``__file__`` 为 None）：
    ``import cst`` 不报错、后面 ``cst.interface`` 才炸，且错误信息不指向真因。
    导入前清掉它，官方库才有机会被找到。
    """
    pkg = sys.modules.get(name)
    if pkg is None:
        return None
    where = getattr(pkg, "__file__", None)
    if where is None:
        del sys.modules[name]
    return where


def load_cst(install_dir=None, lib_dir=None):
    """导入 CST 官方库，返回 ``(cst, cst.interface, cst.results)``。

    参数
    ----
    install_dir : CST 安装根（如 ``C:/Program Files/CST Studio Suite 2024``）
    lib_dir     : 直接给 ``.../AMD64/python_cst_libraries``（优先于 install_dir）

    做法：把库目录插到 ``sys.path[0]``（**先插再导**，保证胜过仓库根的
    ``cst/`` 目录），清掉可能已存在的空壳命名空间包，然后导入并校验。

    **硬约束**：CST 2024 官方库只支持 Python 3.6–3.11，3.12+ 会导入失败
    ——这不是我们能绕的，只能换解释器。
    """
    explicit = [p for p in (lib_dir, install_dir) if p]
    candidates = cst_library_candidates(install_dir=install_dir, extra=[lib_dir] if lib_dir else ())

    # 已经在别处成功导入过（比如服务器上装了 cst-studio-suite-link）就直接用，
    # 但仍然要过一遍遮蔽校验——sys.modules 里的壳包会一直骗人。
    _drop_namespace_shadow("cst")
    for sub in ("cst.interface", "cst.results"):
        _drop_namespace_shadow(sub)

    if candidates:
        # 插到最前：胜过 CWD、胜过仓库根、胜过其它 .pth 带来的路径
        for cand in reversed([str(c) for c in candidates]):
            if cand in sys.path:
                sys.path.remove(cand)
            sys.path.insert(0, cand)

    try:
        import cst
        import cst.interface
        import cst.results
    except Exception as e:                       # pragma: no cover - 无 CST 环境
        hint = _import_failure_hint(explicit, candidates)
        raise CstImportError(f"{e}\n{hint}") from e

    where = getattr(cst, "__file__", None)
    if where is None:
        raise CstImportError(
            "`import cst` 拿到的是**空壳命名空间包**（__file__ 为 None）——"
            "几乎可以肯定是仓库根的 `cst/` 目录把它遮蔽了。\n"
            f"  sys.path 前几项：{sys.path[:5]}\n"
            "  对策：确认 load_cst() 里库目录已插到 sys.path[0]；"
            "或把仓库根的 cst/ 改成别的名字（如 cst_macros/）。"
        )
    if _is_under(where, REPO_ROOT):
        raise CstImportError(
            f"import 到的 `cst` 在仓库目录里：{where}\n"
            "  这是仓库根的 cst/ 目录（宏目录）被当成包了，不是 CST 官方库。\n"
            "  对策：把 cst/ 改名（如 cst_macros/），或显式指定 "
            "install_dir/lib_dir 让官方库排在前面。"
        )

    _check_python_version()
    return cst, cst.interface, cst.results


def _import_failure_hint(explicit, candidates) -> str:
    """导入失败时给一段能直接照做的说明（比裸 ModuleNotFoundError 有用得多）。"""
    lines = ["--- 为什么 import cst 失败 ---"]
    if not candidates and not explicit:
        lines.append("没有找到 CST 的 python_cst_libraries 目录。请显式指定：")
        lines.append("  脚本加 --cst-dir \"C:/Program Files/CST Studio Suite 2024\"，")
        lines.append("  或配置 solver.cst_install_dir / 环境变量 CST_INSTALL_DIR。")
    elif not explicit:
        lines.append(f"试过这些候选目录：{[str(c) for c in candidates]}")
        lines.append("若都不对，请显式指定 install_dir/lib_dir。")
    else:
        lines.append(f"显式给的路径没找到有效库目录：{explicit}")
        lines.append("期望的布局是 <CST>/AMD64/python_cst_libraries/cst/__init__.py")
    lines.append("另一条路（装了就不用给路径）：")
    lines.append("  pip install --no-index --find-links \"<CST>/Library/Python/repo/simple\" "
                 "cst-studio-suite-link")
    major, minor = sys.version_info[0], sys.version_info[1]
    if (major, minor) >= (3, 12):
        lines.append(f"!! 当前解释器是 Python {major}.{minor}：CST 2024 官方库"
                     "只支持 3.6–3.11，请换 3.10（本项目 requires-python>=3.10）。")
    return "\n".join(lines)


def _check_python_version() -> None:
    major, minor = sys.version_info[0], sys.version_info[1]
    if (major, minor) >= (3, 12):                # pragma: no cover - 有 CST 才走到
        print(f"[ !! ] Python {major}.{minor}：CST 2024 官方库官方只支持 "
              f"3.6–3.11，若后面出现诡异错误，先换 3.10 复现。")


def _is_under(path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def describe_import_state() -> list[str]:
    """给探针用：把"现在 import cst 会拿到什么"讲清楚（不抛异常）。"""
    out: list[str] = []
    out.append(f"python      : {sys.version.split()[0]} ({sys.executable})")
    out.append(f"仓库根      : {REPO_ROOT}")
    for name in ("cst", "cst.interface", "cst.results"):
        mod = sys.modules.get(name)
        if mod is None:
            out.append(f"sys.modules : {name:<14} 未导入")
        else:
            where = getattr(mod, "__file__", None)
            out.append(f"sys.modules : {name:<14} {where or '<空壳命名空间包！>'}")
    for i, p in enumerate(sys.path[:6]):
        out.append(f"sys.path[{i}] : {p}")
    return out


# =========================================================================== #
# 会话 / 工程 / 建模 / 求解
#
# 这一层是 init/update/CstSolver **唯一**能碰 CST 的地方：上面三个脚本
# 共享同一份实现，不允许各自写一套（否则"哪个脚本先跑过什么"就会变成
# 隐形状态，出问题时无从复现）。
#
# 设计前提（更换方案的原因，见模块头部）：
#   * 建模一律走 ``model3d.add_to_history(标题, 命令文本)``——它**既执行
#     又写进 History List**，而直接调 VBA 对象模型只改当前模型、重放历史
#     时会被打回原形；
#   * 失败是**可见**的：``add_to_history`` 命令有错时抛 RuntimeError，
#     原始诊断（形如 ``(&H8000ffff) The specified component does not
#     exist``）就在消息里。所以本层只管加"第几块、哪一块"的上下文，
#     然后把原错原样抛出，绝不吞掉。
# =========================================================================== #
def try_calls(obj, calls, what: str):
    """按顺序试几种调用写法，返回第一个不抛异常的结果。

    用于"同一件事在不同 CST 版本里形参名/个数不同"的场合（例如
    ``Project.save`` 收不收路径、有没有 ``allow_overwrite``）。
    **不吞错**：全部失败时抛 RuntimeError，把每种写法的原始错误都列出来
    ——照着报错改代码，比继续猜 API 快得多。
    """
    errs: list[str] = []
    for label, fn in calls:
        try:
            return fn()
        except Exception as e:                      # noqa: BLE001 - 逐条汇报
            errs.append(f"{label}\n      {type(e).__name__}: {e}")
    raise RuntimeError(
        f"{what} 全部失败（试过 {len(calls)} 种写法）：\n    "
        + "\n    ".join(errs)
        + f"\n  该对象的成员：{_member_summary(obj)}"
    )


def require_callable(obj, name: str, what: str):
    """取一个**必须存在**的方法，缺失时给出带成员清单的中文错误。

    比裸 ``AttributeError`` 有用：本机 CST 版本与预期不符时，成员清单
    能一眼看出该换成哪个名字（或确认这个版本根本没有该能力）。
    """
    fn = getattr(obj, name, None)
    if not callable(fn):
        raise RuntimeError(
            f"{what} 需要 `{name}`，但当前对象没有这个方法——本机 CST 的 "
            f"API 与预期不符。\n  它有的成员：{_member_summary(obj)}"
        )
    return fn


def _member_summary(obj, limit: int = 60) -> str:
    try:
        names = sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception:                               # pragma: no cover - 兜底
        return "<取不到成员表>"
    head = ", ".join(names[:limit])
    return head + (f" …（共 {len(names)} 个）" if len(names) > limit else "")


def running_design_environments() -> list:
    """当前机器上运行中的 CST 实例列表（取不到就返回空表，仅供日志）。"""
    try:
        _cst, iface, _res = load_cst()
        return list(iface.running_design_environments())
    except Exception:                               # pragma: no cover - 有 CST 才走到
        return []


def ensure_environment(*, attach: bool = False, force_new: bool = False,
                       options=None, install_dir=None, lib_dir=None,
                       quiet: bool = False, log=print):
    """拿到一个可用的 ``DesignEnvironment``（CST 实例）。

    三种意图，语义钉死，不允许含糊：

    ``attach=True``
        只附接**已经运行**的实例（通常就是用户开着的 GUI），连不上直接
        报错退出——绝不偷偷起新实例，免得用户对着另一个窗口找模型。
        服务器无 GUI 许可时的退路，也是调试时想"看着模型被改"的用法。
    ``force_new=True``
        只新建一个无 GUI 的静态实例。
    默认（两个都不给）
        优先附接已运行的实例（复用上一次会话与已打开的工程），没有才新建。
        **注意**：GUI 里开着 CST 时这会附到那个界面实例上，自动化将在
        用户的 GUI 会话里建工程——调试时正合适，但也别以为在跑无头模式。
        日志里会打印附到了哪个实例。

    附接是"抢"的：``connect_to_any()`` 在有多个实例时**随机挑一个**。
    真要多实例并行，得用 ``connect(pid=...)`` 显式指定，本层暂不支持
    （两个工程共用一个实例就够，见 cst_driver）。
    """
    _cst, iface, _res = load_cst(install_dir=install_dir, lib_dir=lib_dir)
    de_cls = iface.DesignEnvironment

    if attach:
        try:
            de = de_cls.connect_to_any()
        except Exception as e:
            raise RuntimeError(
                "attach 模式要求 CST 已经在运行（GUI 或静态实例），"
                f"但 connect_to_any() 失败：{e}\n"
                "  对策：先打开 CST Studio 2024（或去掉 attach，让脚本"
                "自己起一个静态实例）。"
            ) from e
        log(f"[OK] 附接到已运行的 CST 实例（运行的实例："
            f"{running_design_environments() or '未知'}）")
        return de

    if not force_new:
        try:
            de = de_cls.connect_to_any()
            log("[OK] 复用已运行的 CST 实例（若这不是本脚本启动的，注意它里面"
                "可能还有别的工程；本脚本只会碰自己那两个 .cst 文件）")
            return de
        except Exception:
            pass                                    # 没有实例 -> 新建，属正常路径

    kwargs = {}
    if options:
        kwargs["options"] = list(options)
    de = de_cls.new(**kwargs)
    log(f"[OK] 新建 CST 实例（静态、无 GUI；options={options or '默认'}）")
    if quiet:
        set_quiet_mode(True, log=log)
    return de


def set_quiet_mode(enabled: bool = True, log=print) -> bool:
    """尽力打开/关闭 CST 的静默模式；不同版本挂在不同位置，失败就算了。

    只影响 CST 自己往控制台刷的进度，不影响结果，所以这里不把失败当错误。
    """
    try:
        _cst, iface, _res = load_cst()
    except Exception:                               # pragma: no cover
        return False
    for label, fn in (
        ("cst.interface.set_quiet_mode", lambda: iface.set_quiet_mode(enabled)),
        ("DesignEnvironment.set_quiet_mode",
         lambda: iface.DesignEnvironment.set_quiet_mode(enabled)),
    ):
        try:
            fn()
            log(f"[OK] 静默模式：{label}({enabled})")
            return True
        except Exception:
            continue
    return False


def close_environment(de, *, save: bool = False, log=print) -> None:
    """关闭实例（尽力而为；脚本退出前调一次，避免留下孤儿进程）。"""
    if de is None:
        return
    try:
        if save:
            for prj in open_projects(de):
                try:
                    prj.save()
                except Exception:
                    pass
        de.close()
        log("[OK] 已关闭 CST 实例")
    except Exception as e:                          # pragma: no cover - 兜底
        log(f"[ -- ] 关闭 CST 实例失败（忽略）：{e}")


# --------------------------------------------------------------------------- #
# 工程
# --------------------------------------------------------------------------- #
def new_project(de, log=print):
    """新建一个 Microwave Studio（3D）工程。"""
    fn = require_callable(de, "new_mws", "新建工程")
    prj = fn()
    log("[OK] 新建 MWS 工程")
    return prj


def open_projects(de) -> list:
    """当前实例里已打开的全部工程（API 名字随版本变，逐个试）。"""
    for name in ("get_open_projects", "list_open_projects"):
        fn = getattr(de, name, None)
        if not callable(fn):
            continue
        try:
            return list(fn())
        except Exception:
            continue
    return []


def project_filename(prj) -> str | None:
    """工程对应的磁盘路径（没存过盘时可能是 None/空串）。"""
    fn = getattr(prj, "filename", None)
    if not callable(fn):
        return None
    try:
        v = fn()
    except Exception:
        return None
    return str(v) if v else None


def open_or_reuse_project(de, path, log=print):
    """打开工程；**已经开着就直接复用**，不要重复打开同一个文件。

    重复打开同一个 .cst 会得到第二个工程对象，两边各自改模型、各自存盘，
    后存的覆盖先存的——排查起来非常费劲。所以先按路径比对已打开的列表。
    """
    want = _norm(path)
    for prj in open_projects(de):
        got = project_filename(prj)
        if got and _norm(got) == want:
            log(f"[OK] 复用已打开的工程：{want}")
            return prj
    fn = require_callable(de, "open_project", "打开工程")
    prj = fn(str(path))
    log(f"[OK] 打开工程：{want}")
    return prj


def _norm(p) -> str:
    try:
        return str(Path(p).resolve()).lower()
    except (OSError, ValueError):
        return str(p).lower()


def save_project(prj, path, log=print):
    """把工程另存/保存到 ``path``（父目录自动创建）。

    官方文档的写法是 ``Project.save(path, allow_overwrite=True)``；形参在
    各版本略有出入，故按"带覆盖 → 不带 → model3d.SaveAs"依次试，全失败
    时把每种写法的原始错误都报出来（见 try_calls）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try_calls(prj, [
        (f"prj.save({path}, allow_overwrite=True)",
         lambda: prj.save(str(path), allow_overwrite=True)),
        (f"prj.save({path})", lambda: prj.save(str(path))),
        (f"prj.model3d.SaveAs({path}, True)",
         lambda: prj.model3d.SaveAs(str(path), True)),
    ], what=f"保存工程到 {path}")
    log(f"[OK] 保存工程：{path}")


# --------------------------------------------------------------------------- #
# 建模（历史表）
# --------------------------------------------------------------------------- #
def apply_blocks(model3d, blocks, *, what: str = "写入模型", log=print) -> int:
    """把 ``[(标题, VBA 命令文本)]`` 逐块交给 ``model3d.add_to_history``。

    成功的返回值是 ``True``；命令有错时官方库抛 RuntimeError 并把 CST 的
    原始诊断带在消息里——所以这里只在外面补"第几块 / 哪一块"的上下文
    （以及失败那一块的命令原文，方便直接对照），然后原样抛出。

    ``add_to_history`` 是**同步执行**的：返回即模型已改好。
    """
    add = require_callable(model3d, "add_to_history", what)
    blocks = list(blocks)
    for i, (header, cmd) in enumerate(blocks, 1):
        try:
            ret = add(header, cmd)
        except Exception as e:
            raise RuntimeError(
                f"{what}：第 {i}/{len(blocks)} 块 {header!r} 失败。\n"
                f"CST 原始诊断：{e}\n"
                f"--- 该块命令 ---\n{cmd}"
            ) from e
        if ret is False:
            # 官方库正常是"成功返回 True、失败抛异常"。返回 False 说明
            # CST 既没执行也没报错——这种最危险，必须当场停下。
            raise RuntimeError(
                f"{what}：第 {i}/{len(blocks)} 块 {header!r} 返回 False"
                f"（CST 没有报错，但命令也没生效）。\n"
                f"--- 该块命令 ---\n{cmd}"
            )
        log(f"    [{i:2d}/{len(blocks)}] {header}")
    log(f"[OK] {what}：{len(blocks)} 块已写入历史表")
    return len(blocks)


def update_design_region(model3d, polys, *, thickness_mm: float,
                         material: str = "PEC",
                         component: str = V.DESIGN_COMPONENT, z0: float = 0.0,
                         what: str = "更新设计区形状", log=print) -> int:
    """一轮形状更新：**一条**历史记录 = 删整个组件 + 按多边形重建。

    返回写入的历史记录条数（恒为 1，保留返回值是为了与 apply_blocks 同形）。
    """
    polys = list(polys)
    cmd = V.design_region_update(polys, thickness_mm, component=component,
                                 material=material, z0=z0)
    n_pts = sum(len(p) for p in polys)
    log(f"[ .. ] {what}：{len(polys)} 个多边形 / {n_pts} 个点，"
        f"历史记录 {len(cmd)} 字符")
    apply_blocks(model3d, [("design region", cmd)], what=what, log=log)
    return 1


# --------------------------------------------------------------------------- #
# 求解
# --------------------------------------------------------------------------- #
def run_solver(model3d, *, timeout: float | None = None, log=print) -> float:
    """运行求解器并等它结束，返回耗时（秒）。

    ``run_solver()`` 是**同步**的：返回即求解完成（这与 COM 时代
    ``Solver.Start()`` 后要自己轮询完全不同）。timeout 按官方文档是秒，
    ``None`` = 不限；超时抛异常，由调用方决定怎么报。
    """
    if is_solver_running(model3d):
        raise RuntimeError(
            "求解器已经在运行：可能上一轮还没结束，或上一次崩溃留下了"
            "跑飞的求解进程。先在 CST 里确认（必要时 abort），再重试。")
    run = require_callable(model3d, "run_solver", "求解")
    log("[ .. ] 求解中（同步等待，可能要几分钟）…")
    t0 = time.perf_counter()
    try:
        if timeout is None:
            run()
        else:
            try:
                run(timeout)
            except TypeError:                       # 该版本不收 timeout
                log(f"[ -- ] run_solver 不接受 timeout={timeout}，改为不限时")
                run()
    except Exception as e:
        raise RuntimeError(f"求解失败：{e}") from e
    dt = time.perf_counter() - t0
    info = _solver_info(model3d)
    log(f"[OK] 求解完成，耗时 {dt:.1f} s" + (f"（状态：{info}）" if info else ""))
    return dt


def is_solver_running(model3d) -> bool:
    fn = getattr(model3d, "is_solver_running", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def _solver_info(model3d) -> str:
    fn = getattr(model3d, "get_solver_run_info", None)
    if not callable(fn):
        return ""
    try:
        return str(fn())
    except Exception:
        return ""


def describe_environment(de) -> list[str]:
    """把当前会话/工程状态讲清楚（报告用，不抛异常）。"""
    out = [f"CST 实例      : {de}"]
    try:
        running = running_design_environments()
        out.append(f"运行中的实例  : {running or '未知'}")
    except Exception:
        pass
    prjs = open_projects(de)
    out.append(f"已打开的工程  : {len(prjs)} 个")
    for prj in prjs:
        out.append(f"    - {project_filename(prj) or '<未存盘>'}")
    return out
