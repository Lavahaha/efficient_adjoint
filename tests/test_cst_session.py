"""CST 官方 API 层（cst_session）——**不装 CST 也能全绿**。

这里的重点是导入守卫：本仓库根有个 ``cst/`` 目录（宏目录），它会被 PEP 420
当成命名空间包，让 ``import cst`` "成功"、``cst.interface`` 才炸，而错误信息
完全不指向真因——本机实测复现过。守卫必须把这种空壳清掉、并在导入失败时给出
能照做的中文提示，而不是裸的 ModuleNotFoundError。
"""

import subprocess
import sys
import types
from pathlib import Path

import pytest

from eaopt.solver import cst_session as S

def _make_fake_lib(root: Path) -> Path:
    """造一个假的 CST 库目录 ``<root>/cst/{__init__,interface,results}.py``。"""
    pkg = root / "cst"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('__version__ = "fake-2024"\n', "utf-8")
    (pkg / "interface.py").write_text("MARK = 'interface'\n", "utf-8")
    (pkg / "results.py").write_text("MARK = 'results'\n", "utf-8")
    return root


@pytest.fixture
def clean_modules():
    """备份/还原 ``sys.modules`` 与 ``sys.path``（cst 相关键 + 列表本身）。"""
    mods = {k: v for k, v in sys.modules.items() if k == "cst" or k.startswith("cst.")}
    path = list(sys.path)
    yield
    for k in [k for k in sys.modules if k == "cst" or k.startswith("cst.")]:
        del sys.modules[k]
    sys.modules.update(mods)
    sys.path[:] = path


# --------------------------------------------------------------------------- #
# 导入守卫
# --------------------------------------------------------------------------- #
def test_load_cst_purges_a_namespace_shadow(tmp_path, clean_modules):
    """``sys.modules`` 里的空壳命名空间包必须先清掉，官方库才有机会被找到。

    不这么做的话：``import cst`` 命中壳包（不报错）→ 后面所有调用都对着一个
    没有代码的对象，错误信息完全不指向真因。
    """
    shell = types.ModuleType("cst")
    shell.__file__ = None                       # 命名空间包的特征
    shell.__path__ = [str(tmp_path / "repo_cst")]
    sys.modules["cst"] = shell

    lib = _make_fake_lib(tmp_path / "lib")
    cst, iface, results = S.load_cst(lib_dir=lib)

    assert cst.__file__ is not None
    assert cst.__version__ == "fake-2024"
    assert iface.MARK == "interface"
    assert results.MARK == "results"
    assert sys.modules["cst"] is cst            # 壳包确实被换掉了


def test_load_cst_rejects_a_package_inside_the_repo(tmp_path, clean_modules, monkeypatch):
    """import 到的 cst 若在仓库目录里（宏目录被当成包），必须报错而不是放行。"""
    repo = tmp_path / "repo"
    (repo / "eaopt" / "solver").mkdir(parents=True)
    monkeypatch.setattr(S, "REPO_ROOT", repo)   # 假装仓库根是这个
    lib = _make_fake_lib(repo / "cst")          # 库就在"仓库"里
    with pytest.raises(S.CstImportError) as ei:
        S.load_cst(lib_dir=lib)
    assert "仓库" in str(ei.value)


def test_load_cst_failure_gives_actionable_hints(clean_modules, monkeypatch):
    """没有任何库目录时：报错要含"怎么指定路径"与 install 命令，不能是裸异常。"""
    monkeypatch.setenv("CST_PYTHON_LIBS", "")
    monkeypatch.setenv("CST_INSTALL_DIR", "")
    monkeypatch.setattr(S, "cst_library_candidates", lambda **kw: [])
    with pytest.raises(S.CstImportError) as ei:
        S.load_cst()
    msg = str(ei.value)
    assert "--cst-dir" in msg
    assert "cst-studio-suite-link" in msg
    assert "CST_INSTALL_DIR" in msg


def test_import_failure_mentions_python_version(clean_modules, monkeypatch):
    """Python ≥3.12 上要明确指出官方库不支持（免得去查别的原因）。"""
    monkeypatch.setattr(S, "cst_library_candidates", lambda **kw: [])
    monkeypatch.setattr(S.sys, "version_info", (3, 12, 0))
    with pytest.raises(S.CstImportError) as ei:
        S.load_cst()
    assert "3.6–3.11" in str(ei.value) or "3.6-3.11" in str(ei.value)


def test_library_candidates_prefers_explicit_and_accepts_both_shapes(tmp_path, monkeypatch):
    """既认安装根、也直接认库目录；显式参数排在环境变量/扫描前面。"""
    install = tmp_path / "CST Studio Suite 2024"
    (install / S.CST_LIB_SUBPATH).mkdir(parents=True)
    lib = tmp_path / "pylibs"
    lib.mkdir()
    monkeypatch.setenv("CST_INSTALL_DIR", str(lib))
    out = S.cst_library_candidates(install_dir=install)
    assert out[0] == install / S.CST_LIB_SUBPATH      # 显式给的最优先
    assert lib in out                                  # 环境变量也认
    # 直接给库目录本身（而不是安装根）也要被认出来
    assert S.cst_library_candidates(install_dir=lib)[0] == lib


def test_library_candidates_sees_a_lib_dir_already_on_sys_path(tmp_path, monkeypatch):
    """CST 安装时会往解释器塞 .pth，库目录可能已经在 sys.path 上。

    服务器实测就是这样（`sys.path[1] = D:\\CST 2024\\AMD64\\python_cst_libraries`），
    而安装根扫描一个都没命中——回头看一眼 sys.path 最省事。
    """
    lib = tmp_path / "CST 2024" / "AMD64" / "python_cst_libraries"
    lib.mkdir(parents=True)
    monkeypatch.setattr(S.sys, "path", [str(lib)] + list(sys.path))
    assert lib in S.cst_library_candidates()


def test_module_import_does_not_import_cst():
    """导入本模块**不得**触发 ``import cst``（本地无 CST 时其它模块要能导入）。"""
    code = ("import eaopt.solver.cst_session, sys; "
            "print('cst' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=str(S.REPO_ROOT))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("False")


def test_describe_import_state_flags_a_namespace_shell(tmp_path, clean_modules):
    """探针的 §1 诊断要能一眼看出"这个 cst 是空壳"。"""
    shell = types.ModuleType("cst")
    shell.__file__ = None
    sys.modules["cst"] = shell
    text = "\n".join(S.describe_import_state())
    assert "空壳命名空间包" in text
    assert str(S.REPO_ROOT) in text
