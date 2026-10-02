"""三个脚本共用的编排层（cst_driver）——用一个**假 CST 库**端到端跑通。

这是全链路里最值得锁的一层：三个脚本 + ``CstSolver`` 都走它，而它错了不会
报错、只会产出**看着正常**的错数据（写错轮次、读错端口、场没裁、工程没存盘）。
各层单测只证明了"每一块按契约工作"，这里证明"串起来还是对的"。

假 CST 库照官方 API 的形状搭：``DesignEnvironment.new()/new_mws()`` →
``prj.model3d.add_to_history()/run_solver()`` → ``cst.results.ProjectFile()
.get_3d().get_result_item()``。真机上的行为差异靠 ``scripts/cst_probe_api.py``
收敛；这里锁的是**我们的编排逻辑**。
"""

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from eaopt import artifacts as A
from eaopt.config import CaseConfig
from eaopt.solver import cst_driver as D
from eaopt.solver import cst_results as R
from eaopt.solver import cst_session as S
from eaopt.solver import template_builder as TB
from eaopt.solver import vba as V

REPO = Path(__file__).resolve().parents[1]

#: 假场的采样步长（mm）——比导出步长粗，够用且快。
GRID_STEP = 0.5


def _quiet(*a):
    pass


# --------------------------------------------------------------------------- #
# 假 CST 库（形状照官方 API，行为可编程）
# --------------------------------------------------------------------------- #
def _write_ascii_field(path, x, y, z) -> None:
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


class FakeItem:
    """一条 S 参数曲线：两点（4/6 GHz），值由**条目名**决定。

    值随端口号变，"读的是哪一条"就能从数值上验出来（读错端口 = 数值对不上）。
    """

    def __init__(self, path):
        leaf = path.rsplit("\\", 1)[-1]                  # "S1,3"
        i, j = (int(t) for t in leaf[1:].split(","))
        self._x = np.array([4.0, 6.0])
        self._y = np.array([complex(0.10 * i, 0.05 * j)] * 2)

    def get_xdata(self):
        return self._x

    def get_ydata(self):
        return self._y


class FakeResultModule:
    def __init__(self, asked):
        self._asked = asked

    def get_result_item(self, path, run_id=0, load_impedances=True):
        self._asked.append(path)
        return FakeItem(path)


class FakeResultFile:
    def __init__(self, asked):
        self._asked = asked

    def get_3d(self):
        return FakeResultModule(self._asked)


class FakeResults:
    """假 ``cst.results``：记录被问到的条目路径。"""

    def __init__(self, asked):
        self._asked = asked

    def ProjectFile(self, path, allow_interactive=False):
        return FakeResultFile(self._asked)


class FakeExport:
    """假 ``model3d.ASCIIExport``：记下设置，Execute 时落一个真文件。"""

    def __init__(self, grid):
        self._grid = grid
        self.calls: list[tuple] = []
        self.file = None

    def _set(self, prop, v):
        self.calls.append((prop, str(v)))

    def Reset(self):
        self.calls.append(("Reset",))

    def FileName(self, v):
        self.file = str(v)
        self._set("FileName", v)

    def Mode(self, v):
        self._set("Mode", v)

    def StepX(self, v):
        self._set("StepX", v)

    def StepY(self, v):
        self._set("StepY", v)

    def StepZ(self, v):
        self._set("StepZ", v)

    def Execute(self):
        self.calls.append(("Execute",))
        _write_ascii_field(self.file, *self._grid)


class FakeTree:
    """空结果树：``cst_api.find_item`` 找不到条目 → 退回按惯例拼的路径。"""

    def GetFirstChildName(self, folder):
        return ""


class FakeModel3D:
    def __init__(self, grid):
        self.history: list[tuple] = []
        self.solves = 0
        self.selected: list[str] = []
        self.ResultTree = FakeTree()
        self.ASCIIExport = FakeExport(grid)

    def add_to_history(self, header, cmd):
        self.history.append((header, cmd))
        return True

    def run_solver(self, *args):
        self.solves += 1

    def is_solver_running(self):
        return False

    def SelectTreeItem(self, item):
        self.selected.append(item)


class FakeProject:
    def __init__(self, grid, path=None):
        self.model3d = FakeModel3D(grid)
        self._path = path

    def filename(self):
        return self._path

    def save(self, path=None, **kw):
        self._path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("fake-cst-project", encoding="utf-8")
        return True


class FakeDesignEnvironment:
    """一个"常驻"实例：``new()`` 返回自己，所以两次脚本调用看到同一批工程。

    这正是 ``cst_driver`` 依赖的语义——``connect_to_any()`` 失败后走
    ``new()``，而"常驻会话"要求第二次进来还能看到上次建的工程。
    """

    def __init__(self, grid):
        self._grid = grid
        self.created: list[FakeProject] = []
        self.opened: list[str] = []

    def connect_to_any(self):
        raise RuntimeError("没有运行中的实例")

    def new(self, **kw):
        return self

    def new_mws(self):
        prj = FakeProject(self._grid)
        self.created.append(prj)
        return prj

    def get_open_projects(self):
        return list(self.created)

    def open_project(self, path):
        prj = FakeProject(self._grid, path)
        self.created.append(prj)
        self.opened.append(str(path))
        return prj


class FakeIface:
    def __init__(self, de):
        self.DesignEnvironment = de

    def running_design_environments(self):
        return []


class CstFake:
    """一次"假 CST 会话"：装好 monkeypatch，并留下可断言的记录。"""

    def __init__(self, cfg):
        box = cfg.design_region.box
        m = float(cfg.design_region.field_margin_mm)
        self.grid = (np.arange(box.x[0] - m - 1.0, box.x[1] + m + 1.0, GRID_STEP),
                     np.arange(box.y[0] - m - 1.0, box.y[1] + m + 1.0, GRID_STEP),
                     np.array([0.0, 0.5]))
        self.de = FakeDesignEnvironment(self.grid)
        self.iface = FakeIface(self.de)
        self.s_params_asked: list[str] = []

    def install(self, monkeypatch):
        fakes = (object(), self.iface, FakeResults(self.s_params_asked))
        monkeypatch.setattr(S, "load_cst", lambda **kw: fakes)
        monkeypatch.setattr(R, "load_cst", lambda **kw: fakes)
        return self

    @property
    def projects(self):
        return self.de.created

    def project_of(self, path) -> FakeProject:
        want = str(Path(path).resolve()).lower()
        for prj in self.de.created:
            got = prj.filename()
            if got and str(Path(got).resolve()).lower() == want:
                return prj
        raise AssertionError(f"假 CST 里没有这个工程：{path}")


@pytest.fixture
def cst_cfg(tmp_path):
    """小尺寸的 CST 配置（复用 compact 测试算例，只把求解器换成 cst）。"""
    from conftest import make_compact_cfg

    cfg = make_compact_cfg(tmp_path)
    cfg.solver.type = "cst"
    return cfg


@pytest.fixture
def fake_cst(cst_cfg, monkeypatch):
    return CstFake(cst_cfg).install(monkeypatch)


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def test_initialize_builds_the_project_and_writes_iteration_zero(cst_cfg, fake_cst):
    rep = D.initialize(cst_cfg, "fwd", log=_quiet)

    path = cst_cfg.project_path("fwd")
    assert path.is_file() and rep["project"] == str(path)
    prj = fake_cst.project_of(path)

    # 模板的**全部**命令块按原顺序进了历史表（这正是"历史表非空、重开工程
    # 还有模型"的保证；直接调 VBA 对象模型做不到这一点）
    headers = [h for h, _ in prj.model3d.history]
    assert headers == [h for h, _ in TB.template_blocks(f"{cst_cfg.name}_fwd", 1)]
    assert prj.model3d.solves == 1                     # 第一次仿真跑过了

    # 激励端口写死在工程里（fwd = from_port），之后 pipeline 不再碰激励 API
    solver_block = next(c for h, c in prj.model3d.history if h == "Solver")
    assert '.StimulationPort "1"' in solver_block
    assert '.StimulationMode "1"' in solver_block

    assert A.list_iterations(cst_cfg.output.dir, tags=("fwd",)) == [0]
    assert A.load_s_params(cst_cfg.output.dir, 0, "fwd")[(1, 1)] == pytest.approx(
        complex(0.10, 0.05))


def test_the_two_init_scripts_share_one_implementation(cst_cfg, fake_cst):
    """fwd/bwd 只差激励端口：bwd 读的是 S{i,3}，fwd 读 S{i,1}。"""
    D.initialize(cst_cfg, "fwd", log=_quiet)
    D.initialize(cst_cfg, "bwd", log=_quiet)

    bwd = fake_cst.project_of(cst_cfg.project_path("bwd"))
    solver_block = next(c for h, c in bwd.model3d.history if h == "Solver")
    assert '.StimulationPort "3"' in solver_block
    assert '.StimulationMode "1"' in solver_block       # 端口与模式必须成对

    asked = fake_cst.s_params_asked
    assert "1D Results\\S-Parameters\\S1,1" in asked
    assert "1D Results\\S-Parameters\\S1,3" in asked

    # 两个 init 都跑完，iter_000 才算完成（半截轮次不算，见 artifacts 的判据）
    assert A.list_iterations(cst_cfg.output.dir) == [0]
    meta = json.loads((A.iteration_dir(cst_cfg.output.dir, 0) / "meta.json")
                      .read_text("utf-8"))
    assert meta["tags_present"] == ["bwd", "fwd"]


def test_initialize_refuses_to_overwrite_an_existing_project(cst_cfg, fake_cst):
    """重建会丢掉已跑的轮次——宁可报错让人显式删，也不要静默覆盖。"""
    D.initialize(cst_cfg, "fwd", log=_quiet)
    with pytest.raises(FileExistsError, match="已经存在"):
        D.initialize(cst_cfg, "fwd", log=_quiet)


def test_update_without_init_tells_you_to_init_first(cst_cfg, fake_cst):
    with pytest.raises(FileNotFoundError, match="cst_init_fwd.py"):
        D.update_design(cst_cfg, _polys(), log=_quiet)


# --------------------------------------------------------------------------- #
# 形状更新
# --------------------------------------------------------------------------- #
def _polys():
    return [np.array([[0.0, -0.6], [4.0, -0.6], [4.0, -0.1], [0.0, -0.1]])]


def test_update_design_changes_both_projects_in_one_record_each(cst_cfg, fake_cst):
    """两个工程各发**一条**历史记录（删组件 + 重建），并各求解一次。"""
    D.initialize(cst_cfg, "fwd", log=_quiet)
    D.initialize(cst_cfg, "bwd", log=_quiet)
    before = {tag: len(fake_cst.project_of(cst_cfg.project_path(tag))
                       .model3d.history) for tag in ("fwd", "bwd")}

    rep = D.update_design(cst_cfg, _polys(), log=_quiet)
    assert rep["iteration"] == 1                        # 0 已被 init 占了

    for tag in ("fwd", "bwd"):
        prj = fake_cst.project_of(cst_cfg.project_path(tag))
        assert len(prj.model3d.history) == before[tag] + 1
        header, cmd = prj.model3d.history[-1]
        assert header == "design region"
        assert 'Component.Delete "design_region"' in cmd  # 幂等：不必知道上一轮有几个实体
        assert "With Extrude" in cmd
        assert '.LineTo "4", "-0.1"' in cmd                 # 新形状确实写进去了
        assert prj.model3d.solves == 2                      # init 1 次 + 这次 1 次

    assert A.list_iterations(cst_cfg.output.dir) == [0, 1]
    shape = json.loads((A.iteration_dir(cst_cfg.output.dir, 1) / "shape.json")
                       .read_text("utf-8"))
    assert shape["polygons"][0][2] == [4.0, -0.1]


def test_update_reuses_the_open_project_instead_of_reopening(cst_cfg, fake_cst):
    """同一个 .cst 绝不重复打开（两个工程对象会各改各的、互相覆盖存盘）。"""
    D.initialize(cst_cfg, "fwd", log=_quiet)
    D.initialize(cst_cfg, "bwd", log=_quiet)
    n_created = len(fake_cst.projects)

    D.update_design(cst_cfg, _polys(), log=_quiet)
    assert len(fake_cst.projects) == n_created          # 没有新开工程对象
    assert fake_cst.de.opened == []                     # 也没走 open_project


def test_solve_saves_before_reading_results(cst_cfg, fake_cst, monkeypatch):
    """cst.results 读的是磁盘结果：存盘必须排在读结果之前。

    顺序错了在真机上表现为"读到的永远是上一轮的值"——不报错，只是数据错。
    """
    D.initialize(cst_cfg, "fwd", log=_quiet)

    order: list[str] = []
    orig_save, orig_read = S.save_project, R.read_s_params

    def spy_save(*a, **kw):
        order.append("save")
        return orig_save(*a, **kw)

    def spy_read(*a, **kw):
        order.append("read")
        return orig_read(*a, **kw)

    monkeypatch.setattr(S, "save_project", spy_save)
    monkeypatch.setattr(R, "read_s_params", spy_read)

    sess = D.CstSession(cst_cfg, log=_quiet)
    sess.projects["fwd"] = fake_cst.project_of(cst_cfg.project_path("fwd"))
    sess.solve("fwd", export_fields=False)              # 场不是本条的重点

    assert order.index("save") < order.index("read")


def test_fields_are_cropped_to_the_design_region(cst_cfg, fake_cst):
    """导出的场必须裁到设计区 ± 余量，且坐标/数值都来自导出文件。

    不裁的话：整域导出的点数按面积放大一个量级，而伴随法只在设计区近旁
    用得到场。
    """
    D.initialize(cst_cfg, "fwd", log=_quiet)
    prj = fake_cst.project_of(cst_cfg.project_path("fwd"))
    exp = prj.model3d.ASCIIExport

    kinds = [c[0] for c in exp.calls]
    assert kinds.count("Execute") == 2                  # E 场与 H 场各导一次
    assert kinds[0] == "Reset"                          # Reset 必须最先
    assert ("Mode", "FixedWidth") in exp.calls          # 步长 = mm，与解析器自洽
    assert ("StepX", f"{cst_cfg.export_step_mm():g}") in exp.calls
    assert prj.model3d.selected and all(
        p.startswith("2D/3D Results\\") for p in prj.model3d.selected)

    # 裁剪后的网格：坐标范围在设计区 ± 余量内，且不是空片
    sess = D.CstSession(cst_cfg, log=_quiet)
    grid = sess._export(prj.model3d, "Efield", "fwd", cst_cfg.project_path("fwd"))
    box, m = cst_cfg.design_region.box, float(cst_cfg.design_region.field_margin_mm)
    ax = grid.axes()
    assert box.x[0] - m - 1e-9 <= ax[0][0] and ax[0][-1] <= box.x[1] + m + 1e-9
    assert box.y[0] - m - 1e-9 <= ax[1][0] and ax[1][-1] <= box.y[1] + m + 1e-9
    assert grid.data.shape[3] == 3
    assert np.any(grid.data != 0)                       # 数值确实是导出文件里的


def test_export_fields_is_a_real_switch(cst_cfg, fake_cst):
    """export_fields=False 时一次都不导出（排查时能省几分钟）。"""
    D.initialize(cst_cfg, "fwd", log=_quiet)
    prj = fake_cst.project_of(cst_cfg.project_path("fwd"))
    n_before = len(prj.model3d.ASCIIExport.calls)

    sess = D.CstSession(cst_cfg, log=_quiet)
    sess.projects["fwd"] = prj
    sol = sess.solve("fwd", export_fields=False)

    assert len(prj.model3d.ASCIIExport.calls) == n_before
    assert sol.e_field is None and sol.h_field is None
    assert sol.s_params                                 # S 参数照样有


# --------------------------------------------------------------------------- #
# 真配置 / CLI
# --------------------------------------------------------------------------- #
def test_the_shipped_coupler_yaml_still_loads_after_the_cst_rework():
    """配置字段改名（新增 project_*，去掉 field_backend）后仍要能读。"""
    cfg = CaseConfig.from_yaml(REPO / "configs" / "coupler.yaml")
    assert cfg.project_path("fwd") == Path("results/coupler/cst/coupler_fwd.cst")
    assert cfg.project_path("bwd") == Path("results/coupler/cst/coupler_bwd.cst")
    assert cfg.stimulus_port("fwd") == cfg.objective.from_port
    assert cfg.stimulus_port("bwd") == cfg.objective.to_port
    assert cfg.stimulus_port("fwd") != cfg.stimulus_port("bwd")   # 否则伴随无意义
    assert cfg.export_step_mm() == cfg.sampling.point_spacing_mm


def test_project_path_honours_explicit_config(tmp_path):
    """显式给了路径就用它；旧名 template_* 仍然认（COM 时代的配置不用改）。"""
    d = _coupler_dict(tmp_path)
    d["solver"]["project_fwd"] = str(tmp_path / "explicit.cst")
    d["solver"]["template_bwd"] = str(tmp_path / "legacy.cst")
    cfg = CaseConfig.from_dict(d)
    assert cfg.project_path("fwd") == tmp_path / "explicit.cst"
    assert cfg.project_path("bwd") == tmp_path / "legacy.cst"


def test_init_main_cli_reports_and_returns_zero(fake_cst, tmp_path, capsys):
    """CLI 报告块里不能有 [FAIL]，且要能看到 |S|、dB 与产物路径。"""
    yml = _write_real_cfg(tmp_path)

    rc = D.init_main("fwd", argv=[str(yml), "--lib-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[FAIL]" not in out
    assert "[OK] fwd @ iter 000" in out
    assert "|S1,1|" in out and "dB" in out
    assert "FoM" in out


def test_init_main_cli_reports_failure_without_a_traceback(fake_cst, tmp_path,
                                                           capsys):
    """工程已存在 -> 退出码 1 + 一行 [FAIL]，不是异常回溯。"""
    yml = _write_real_cfg(tmp_path)
    argv = [str(yml), "--lib-dir", str(tmp_path)]
    assert D.init_main("fwd", argv=argv) == 0
    capsys.readouterr()

    assert D.init_main("fwd", argv=argv) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "已经存在" in out


def test_update_main_cli_refuses_an_ambiguous_shape_source(fake_cst, tmp_path):
    """--shape 与 --from-ls 必须二选一（两个都给 = 不知道该信谁）。"""
    yml = _write_real_cfg(tmp_path)
    with pytest.raises(SystemExit) as ei:
        D.update_main(argv=[str(yml), "--from-ls", "--shape", str(tmp_path)])
    assert "--shape" in str(ei.value)


def test_update_main_cli_writes_the_next_iteration(fake_cst, tmp_path, capsys):
    """--shape 给文件 → 两个工程都更新 → iter_001 落盘。"""
    yml = _write_real_cfg(tmp_path)
    assert D.init_main("fwd", argv=[str(yml), "--lib-dir", str(tmp_path)]) == 0
    assert D.init_main("bwd", argv=[str(yml), "--lib-dir", str(tmp_path)]) == 0
    capsys.readouterr()

    cfg = CaseConfig.from_yaml(yml)
    shape = A.save_shape(cfg.output.dir, 0, _polys())
    rc = D.update_main(argv=[str(yml), "--shape", str(shape),
                             "--lib-dir", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[FAIL]" not in out
    assert "[OK] fwd @ iter 001" in out and "[OK] bwd @ iter 001" in out
    assert A.list_iterations(cfg.output.dir) == [0, 1]


def test_export_mode_is_fixed_width_everywhere():
    """导出设置是单一事实来源：脚本路径与 vba 模块不能各写一份。"""
    assert V.ASCII_EXPORT_MODE == "FixedWidth"
    assert dict(V.ascii_export_params(0.2))["Mode"] == V.ASCII_EXPORT_MODE


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _coupler_dict(tmp_path) -> dict:
    """真实 ``configs/coupler.yaml`` 的字典副本，输出目录改到 tmp_path。

    刻意从真配置派生（而不是手写一份小配置）：CLI 测试顺便锁住"这份 YAML
    在新代码里仍然能加载"，而手写的那份永远会和真配置漂移。
    """
    d = yaml.safe_load((REPO / "configs" / "coupler.yaml").read_text("utf-8"))
    d["output"]["dir"] = str(tmp_path / "results")
    d["solver"] = {"type": "cst"}       # 清掉 project_*/template_*，走缺省路径
    return d


def _write_real_cfg(tmp_path) -> Path:
    yml = tmp_path / "coupler.yaml"
    yml.write_text(yaml.safe_dump(_coupler_dict(tmp_path), allow_unicode=True),
                   encoding="utf-8")
    return yml
