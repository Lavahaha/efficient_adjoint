"""``scripts/cst_update.py``：改设计区形状 → 两个工程各求解一次 → 写下一轮。

迭代循环里"改形状"这一步的**手工入口**（``run_coupler.py`` 里由 CstSolver 干
同一件事）。锁的纪律：

  * 每工程每轮**恰好一条**历史记录（模板命令块那些是建工程时的，不算）；
  * ``iter_NNN/shape.json`` 记的是**真正施加的**形状，且覆盖写；
  * 同一个 ``.cst`` 复用已打开的工程对象，绝不打开两次；
  * 两个工程都改成功之后才写 shape.json——半施加状态不留形状记录；
  * 轮次缺省 = 已有最新轮次 + 1。
"""

import json

import numpy as np
import pytest

from cst import interface as csti

from eaopt import artifacts
from eaopt.config import CaseConfig
from eaopt.pipeline import make_level_set

from conftest import load_script, write_case_yaml

INIT = {tag: load_script(f"cst_init_{tag}") for tag in artifacts.TAGS}
UPDATE = load_script("cst_update")

#: 一个闭合的矩形金属条（世界坐标 mm）——完全落在设计区里
RECT = [[0.5, 0.3], [3.5, 0.3], [3.5, 0.2], [0.5, 0.2], [0.5, 0.3]]


@pytest.fixture
def ready(tmp_path):
    """两个工程都 init 过的算例（iter_000 完成），事件表清空后交给测试。"""
    path = write_case_yaml(tmp_path)
    cfg = CaseConfig.from_yaml(path)
    for tag in artifacts.TAGS:
        assert INIT[tag].main([str(path)]) == 0
    csti.state().events.clear()
    return path, cfg


def _shape_file(tmp_path, polys=RECT) -> str:
    p = tmp_path / "given_shape.json"
    p.write_text(json.dumps(polys), encoding="utf-8")
    return str(p)


def _project(tag: str):
    for prj in csti.state().projects:
        if (prj.filename() or "").lower().endswith(f"_{tag}.cst"):
            return prj
    raise AssertionError(f"假 CST 里没有 {tag} 工程")


# --------------------------------------------------------------------------- #
# 一轮形状更新
# --------------------------------------------------------------------------- #
def test_update_writes_the_next_iteration_with_the_given_shape(ready, tmp_path):
    path, cfg = ready
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 0

    assert artifacts.latest_iteration(cfg.output.dir) == 1      # 000 之后是 001
    (poly,) = artifacts.load_shape(artifacts.iteration_dir(cfg.output.dir, 1))
    assert np.allclose(poly, RECT)
    for tag in artifacts.TAGS:
        assert artifacts.load_s_params(cfg.output.dir, 1, tag)


def test_update_appends_exactly_one_history_record_per_project(ready, tmp_path):
    """一轮 = 删组件 + 重建，整段作为一条：历史表每轮只长一条。"""
    path, _ = ready
    before = {tag: len(_project(tag).model3d.history) for tag in artifacts.TAGS}
    solves = {tag: _project(tag).model3d.solves for tag in artifacts.TAGS}

    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 0

    for tag in artifacts.TAGS:
        prj = _project(tag)
        assert len(prj.model3d.history) == before[tag] + 1
        _h, cmd = prj.model3d.history[-1]
        assert 'Component.Delete "design_region"' in cmd
        assert "Extrude" in cmd
        assert prj.model3d.solves == solves[tag] + 1


def test_update_reuses_the_open_projects(ready, tmp_path):
    """同一个 .cst 绝不打开两次：两边各改各的、后存的会覆盖先存的。"""
    path, _ = ready
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 0
    assert not [e for e in csti.state().events if e[0] == "open_project"]
    assert len(csti.state().projects) == 2


def test_update_honours_the_iteration_flag(ready, tmp_path):
    path, cfg = ready
    argv = [str(path), "--shape", _shape_file(tmp_path), "--iteration", "7"]
    assert UPDATE.main(argv) == 0
    assert artifacts.latest_iteration(cfg.output.dir) == 7
    assert (artifacts.iteration_dir(cfg.output.dir, 7) / "shape.json").is_file()


def test_update_overwrites_the_shape_file(ready, tmp_path):
    """先来一轮大矩形、再来一轮小矩形：磁盘上剩的必须是后一个。"""
    path, cfg = ready
    big = [[0.5, 0.3], [3.5, 0.3], [3.5, 0.2], [0.5, 0.2], [0.5, 0.3]]
    small = [[1.0, 0.3], [2.0, 0.3], [2.0, 0.2], [1.0, 0.2], [1.0, 0.3]]
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path, big),
                        "--iteration", "1"]) == 0
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path, small),
                        "--iteration", "1"]) == 0
    (poly,) = artifacts.load_shape(artifacts.iteration_dir(cfg.output.dir, 1))
    assert np.allclose(poly, small)


def test_update_from_ls_rebuilds_the_contours(ready):
    """``--from-ls``：用最近一轮的 φ 快照重算轮廓（不依赖 pipeline 在场）。"""
    path, cfg = ready
    ls = make_level_set(cfg)
    artifacts.save_ls_phi(cfg.output.dir, 0, ls)

    assert UPDATE.main([str(path), "--from-ls"]) == 0
    polys = artifacts.load_shape(artifacts.iteration_dir(cfg.output.dir, 1))
    assert polys, "φ 快照里至少该有一个可动金属轮廓"
    box = cfg.design_region.box
    for p in polys:
        assert np.allclose(p[0], p[-1])                          # 闭合
        assert p[:, 0].min() >= box.x[0] - 0.1
        assert p[:, 0].max() <= box.x[1] + 0.1
        assert p[:, 1].min() >= box.y[0] - 0.1
        assert p[:, 1].max() <= box.y[1] + 0.1
    # 初始可动金属是 y∈[-0.6, 0] 的横段：轮廓该落在它的边界附近
    ys = np.vstack(polys)[:, 1]
    assert ys.min() < 0.0 and ys.max() <= 0.15


def test_resolve_shape_hands_cst_simplified_contours(ready, monkeypatch):
    """送 CST 的轮廓必须简化，参数与 ``CstSolver.build_model`` 同一套。

    2026-10-09 功分器 Stage 1：854 点的网格描线超过 CST ``Extrude .Create``
    的点表容量，CST 只回一句 "Profile is self-intersecting"（497 点能用）。
    两个调用点各有一份路径，所以各守一条。"""
    from eaopt.geometry.contour import SIMPLIFY_TOL_CELLS

    path, cfg = ready
    artifacts.save_ls_phi(cfg.output.dir, 0, make_level_set(cfg))

    seen = {}
    real = UPDATE.close_open_contours

    def spy(contours, box, **kw):
        seen.update(kw)
        return real(contours, box, **kw)

    monkeypatch.setattr(UPDATE, "close_open_contours", spy)
    assert UPDATE.main([str(path), "--from-ls"]) == 0
    assert seen["simplify_tol_mm"] == pytest.approx(
        SIMPLIFY_TOL_CELLS * cfg.design_region.grid_step_mm)


# --------------------------------------------------------------------------- #
# 无人值守：模态框（已有结果 + 重跑 ⇒ CST 弹框等人点）
# --------------------------------------------------------------------------- #
def _gates_off(monkeypatch, **flags):
    """把脚本看到的闸门关掉（并按需熄掉假会话里已经开着的静默模式）。

    ``ready`` 里的 init 已经把假会话切成静默了——它是**会话级**状态，不会
    因为下一个脚本用别的设置就自己熄掉。要测"没有静默"的路径，两边都得关。

    设置现在由 :func:`load_case` 按算例给（``solver/case.py``），所以这里
    patch 的是**分发函数**：返回一份改过字段的 ``CstSetup`` + 真模板模块。
    """
    import dataclasses
    from eaopt.solver import cst_model
    from eaopt.solver.cst_setup import COUPLER
    settings = dict(quiet_mode=False, **flags)
    setup = dataclasses.replace(COUPLER, **settings)
    monkeypatch.setattr(UPDATE, "load_case", lambda cfg: (setup, cst_model))
    if not settings["quiet_mode"]:
        csti.configure(quiet_mode=False)


def test_update_clears_the_previous_results_even_without_quiet_mode(
        ready, tmp_path, monkeypatch):
    """回归：工程里带着上一轮的 S 参数与场，改形状前先清掉——否则弹框等人点。

    静默模式关掉（服务器上老版本 CST 没有这个方法时就是这样），闸门只剩
    DeleteResults 一道。"不清也能过"是不可能的：假库里那个框会抛错。
    """
    path, _ = ready
    _gates_off(monkeypatch)
    assert all(_project(t).model3d.has_results for t in artifacts.TAGS)

    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 0

    for tag in artifacts.TAGS:
        prj = _project(tag)
        assert prj.model3d.vba_calls[-1].count("DeleteResults") == 1
        assert all("DeleteResults" not in cmd for _h, cmd in prj.model3d.history)


def test_update_with_both_gates_off_reproduces_the_original_failure(
        ready, tmp_path, monkeypatch):
    """两闸都关 = 复现原始故障（对照上面那条：证明它真的在测东西）。"""
    path, _ = ready
    _gates_off(monkeypatch, clear_results=False)
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 1


# --------------------------------------------------------------------------- #
# 出错路径
# --------------------------------------------------------------------------- #
def test_shape_source_must_be_given_and_only_one(ready, tmp_path):
    """``--shape`` / ``--from-ls`` 二选一；错了就别烧一次仿真。"""
    path, cfg = ready
    with pytest.raises(SystemExit):
        UPDATE.main([str(path)])                                 # 都不给
    with pytest.raises(SystemExit):
        UPDATE.main([str(path), "--shape", _shape_file(tmp_path), "--from-ls"])
    assert not (artifacts.iteration_dir(cfg.output.dir, 1) / "shape.json").exists()


def test_missing_projects_point_at_the_init_scripts(tmp_path):
    """没建工程就改形状：报错要说清先去跑哪两个脚本。"""
    path = write_case_yaml(tmp_path)
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 1


def test_no_shape_is_written_when_the_update_fails(ready, tmp_path):
    """半施加状态不留形状记录：工程没改成，shape.json 就不该出现。"""
    path, cfg = ready
    csti.configure(history_error=("design region", RuntimeError("VBA 语法错误")))
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 1
    assert not (artifacts.iteration_dir(cfg.output.dir, 1) / "shape.json").exists()


def test_solver_failure_returns_nonzero(ready, tmp_path):
    path, cfg = ready
    csti.configure(solve_error=RuntimeError("求解器拒绝"))
    assert UPDATE.main([str(path), "--shape", _shape_file(tmp_path)]) == 1
