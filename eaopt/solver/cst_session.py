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
from pathlib import Path

__all__ = [
    "CstImportError",
    "CST_LIB_SUBPATH",
    "REPO_ROOT",
    "cst_library_candidates",
    "load_cst",
    "describe_import_state",
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
