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


# --------------------------------------------------------------------------- #
# 假的 CST 库（形状照官方 API，行为可编程）
# --------------------------------------------------------------------------- #
class _Calls(list):
    """调用记录 + 一个 fail_connect 开关（list 上挂不了属性，故派生子类）。"""

    fail_connect = False


class FakeDesignEnvironment:
    """`DesignEnvironment` 的替身：记录 new/connect 的调用，供分支断言。"""

    def __init__(self, kids):
        self._kids = kids

    def connect_to_any(self):
        self._kids.append("connect_to_any")
        if self._kids.fail_connect:
            raise RuntimeError("没有正在运行的 CST 实例")
        return "DE(已存在)"

    def new(self, **kwargs):
        self._kids.append(("new", kwargs))
        return "DE(新建)"


class FakeIface:
    def __init__(self, de, running=()):
        self.DesignEnvironment = de
        self._running = running

    def running_design_environments(self):
        return list(self._running)


@pytest.fixture
def fake_env(monkeypatch):
    """把 S.load_cst 换成返回假的 cst 库，并给一份可编程的 DesignEnvironment。"""
    def install(*, fail_connect=False, running=()):
        calls = _Calls()
        calls.fail_connect = fail_connect
        de = FakeDesignEnvironment(calls)
        iface = FakeIface(de, running)
        monkeypatch.setattr(S, "load_cst", lambda **kw: (object(), iface, object()))
        return calls
    return install


def test_ensure_environment_reuses_then_falls_back_to_new(fake_env):
    """默认语义：先附接（复用上一次会话），没有实例才新建。"""
    calls = fake_env(fail_connect=True)
    assert S.ensure_environment(log=lambda *a: None) == "DE(新建)"
    assert calls == ["connect_to_any", ("new", {})]

    calls2 = fake_env()                      # 有实例 -> 只附接，不新建
    assert S.ensure_environment(log=lambda *a: None) == "DE(已存在)"
    assert calls2 == ["connect_to_any"]


def test_ensure_environment_attach_never_silently_starts_a_new_instance(fake_env):
    """attach=True 连不上必须**报错退出**，绝不偷偷起新实例。

    否则用户对着自己开着的 GUI 找模型，而脚本在另一个实例里改得好好的。
    """
    fake_env(fail_connect=True)
    with pytest.raises(RuntimeError, match="attach"):
        S.ensure_environment(attach=True, log=lambda *a: None)

    calls = fake_env()
    S.ensure_environment(attach=True, log=lambda *a: None)
    assert calls == ["connect_to_any"]


def test_ensure_environment_force_new_passes_options(fake_env):
    calls = fake_env()
    S.ensure_environment(force_new=True, options=["-m"], log=lambda *a: None)
    assert calls == [("new", {"options": ["-m"]})]


# --------------------------------------------------------------------------- #
# 历史表
# --------------------------------------------------------------------------- #
class FakeModel3D:
    """只有 add_to_history 的假 model3d：可指定"第几块失败/返回 False"。"""

    def __init__(self, ret=True, error=None, error_on=None):
        self.records: list[tuple] = []
        self._ret, self._error = ret, error
        self._error_on = error_on                # 块标题；None = 第一块就炸

    def add_to_history(self, header, cmd):
        self.records.append((header, cmd))
        hit = self._error_on is None or header == self._error_on
        if self._error is not None and hit:
            raise self._error
        if self._ret is False and hit:
            return False
        return True


def test_apply_blocks_writes_header_and_command_in_order():
    """每块的原样交给 add_to_history：标题/命令顺序写反，历史表就成了一团乱码。"""
    m3d = FakeModel3D()
    blocks = [("Units", "cmd-A"), ("Brick substrate", "cmd-B")]
    n = S.apply_blocks(m3d, blocks, log=lambda *a: None)
    assert n == 2
    assert m3d.records == blocks


def test_apply_blocks_reports_which_block_failed():
    """失败要能直接定位到"第几块、哪一块"，并原样保留 CST 的诊断。"""
    m3d = FakeModel3D(error=RuntimeError("(&H8000ffff) 组件不存在"),
                      error_on="Brick substrate")
    blocks = [("Units", "ok"), ("Brick substrate", "boom"), ("Port p1", "never")]
    with pytest.raises(RuntimeError) as ei:
        S.apply_blocks(m3d, blocks, log=lambda *a: None)
    msg = str(ei.value)
    assert "2/3" in msg and "'Brick substrate'" in msg
    assert "(&H8000ffff) 组件不存在" in msg     # 原始诊断不能丢
    assert "boom" in msg                        # 出错的那块命令原文
    assert m3d.records == blocks[:2]            # 失败即停，不再往下发


def test_apply_blocks_stops_on_a_false_return():
    """返回 False = CST 既没执行也没报错：这种最危险，必须当场停。"""
    m3d = FakeModel3D(ret=False)
    with pytest.raises(RuntimeError, match="返回 False"):
        S.apply_blocks(m3d, [("Units", "cmd")], log=lambda *a: None)


def test_update_design_region_sends_exactly_one_record():
    """每轮一条历史记录：删组件 + 重建全部多边形（20 轮只增长 20 条）。"""
    m3d = FakeModel3D()
    polys = [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]]
    n = S.update_design_region(m3d, polys, thickness_mm=0.035,
                               log=lambda *a: None)
    assert n == 1 and len(m3d.records) == 1
    header, cmd = m3d.records[0]
    assert header == "design region"
    assert 'Component.Delete "design_region"' in cmd
    assert 'With Extrude' in cmd


def test_update_design_region_refuses_an_empty_shape():
    """空形状会把设计区金属删光——在发命令之前就该拒绝。"""
    with pytest.raises(ValueError, match="polys 为空"):
        S.update_design_region(FakeModel3D(), [], thickness_mm=0.035,
                               log=lambda *a: None)


def test_require_callable_lists_members_when_missing():
    """API 名字对不上时，错误里要有成员清单（否则只能靠猜下一个名字）。"""
    class Odd:
        def whatever(self):
            pass

    with pytest.raises(RuntimeError) as ei:
        S.require_callable(Odd(), "add_to_history", "写入模型")
    assert "add_to_history" in str(ei.value)
    assert "whatever" in str(ei.value)


# --------------------------------------------------------------------------- #
# 工程
# --------------------------------------------------------------------------- #
class FakeProject:
    def __init__(self, filename=None, save_error=None):
        self._filename = filename
        self._save_error = save_error
        self.saves: list[dict] = []

    def filename(self):
        return self._filename

    def save(self, path=None, **kw):
        self.saves.append({"path": path, **kw})
        if self._save_error is not None:
            raise self._save_error
        return True


def test_open_or_reuse_project_does_not_open_the_same_file_twice(tmp_path):
    """同一个 .cst 重复打开会得到两个工程对象，各自改模型、互相覆盖存盘。"""
    path = tmp_path / "a.cst"
    path.write_text("x", "utf-8")
    same = FakeProject(str(path))

    class DE:
        def __init__(self):
            self.opened: list[str] = []

        def get_open_projects(self):
            return [same]

        def open_project(self, p):
            self.opened.append(p)
            return FakeProject(p)

    de = DE()
    assert S.open_or_reuse_project(de, path, log=lambda *a: None) is same
    assert de.opened == []

    other = tmp_path / "b.cst"
    other.write_text("x", "utf-8")
    assert S.open_or_reuse_project(de, other, log=lambda *a: None) is not same
    assert de.opened == [str(other)]


def test_save_project_tries_the_signature_chain_and_reports_all_errors(tmp_path):
    """save() 的形参随版本变：带覆盖 → 不带 → model3d.SaveAs，全失败要全报。"""
    out = tmp_path / "sub" / "p.cst"                 # 父目录不存在
    prj = FakeProject()
    S.save_project(prj, out, log=lambda *a: None)
    assert prj.saves[0] == {"path": str(out), "allow_overwrite": True}
    assert out.parent.is_dir()                       # 父目录自动建

    class Broken:
        @property
        def model3d(self):
            raise RuntimeError("没有 model3d")

        def save(self, *a, **kw):
            raise TypeError("wrong number of parameters")

    with pytest.raises(RuntimeError) as ei:
        S.save_project(Broken(), out, log=lambda *a: None)
    msg = str(ei.value)
    assert "allow_overwrite=True" in msg and "SaveAs" in msg


def test_close_environment_is_best_effort():
    """关实例失败不该把脚本拽崩（它已经在收尾了）。"""
    class DE:
        def close(self):
            raise RuntimeError("已经没了")

    S.close_environment(DE(), log=lambda *a: None)   # 不抛


# --------------------------------------------------------------------------- #
# 求解
# --------------------------------------------------------------------------- #
class FakeSolver:
    def __init__(self, running=False, error=None):
        self._running, self._error = running, error
        self.calls: list = []

    def is_solver_running(self):
        return self._running

    def run_solver(self, *args):
        self.calls.append(("run_solver", args))
        if self._error is not None:
            raise self._error

    def get_solver_run_info(self):
        return "solved"


def test_run_solver_is_synchronous_and_reports_seconds():
    m3d = FakeSolver()
    dt = S.run_solver(m3d, log=lambda *a: None)
    assert dt >= 0.0 and m3d.calls == [("run_solver", ())]


def test_run_solver_refuses_to_start_on_top_of_a_running_solver():
    """上一轮没结束就再来一发，只会拿到状态不明的结果（或挂死）。"""
    with pytest.raises(RuntimeError, match="已经在运行"):
        S.run_solver(FakeSolver(running=True), log=lambda *a: None)


def test_run_solver_falls_back_when_timeout_is_not_supported():
    """timeout 是可选形参：不接受就退回不限时，而不是当作求解失败。"""
    class NoTimeout(FakeSolver):
        def run_solver(self, *args):
            self.calls.append(("run_solver", args))
            if args:
                raise TypeError("run_solver() takes no arguments")

    m3d = NoTimeout()
    S.run_solver(m3d, timeout=60.0, log=lambda *a: None)
    assert m3d.calls == [("run_solver", (60.0,)), ("run_solver", ())]


def test_run_solver_wraps_the_original_error():
    """求解失败的原始信息必须留着（CST 的诊断只在异常文本里）。"""
    with pytest.raises(RuntimeError, match="网格发散"):
        S.run_solver(FakeSolver(error=RuntimeError("网格发散")),
                     log=lambda *a: None)
