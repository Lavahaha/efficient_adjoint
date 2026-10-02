"""CstSolver：pipeline ↔ CST 的适配器（假 ``cst.interface``，不装 CST 也能全绿）。

锁的是**接口边界与不能出错的数据纪律**：
  * 不碰 CST 就不连实例（装配阶段零副作用）；``auto_init`` 默认关；
  * 每工程每轮**恰好一条**历史记录；shape.json = 真正施加的轮廓；
  * fwd 读 ``S{i,1}`` / bwd 读 ``S{i,3}``（读错端口不报错，只是模型错）；
  * **先存盘再读结果**（反了会读到上一轮的值，照样出数）；
  * 场每轮都导、裁到设计区 ± margin；``save_fields`` 只决定写不写 .npz；
  * ``close`` 只存盘，不关实例（常驻会话供下一次运行复用）。
"""

from pathlib import Path

import numpy as np
import pytest

from cst import interface as csti          # 假 cst.interface（conftest 注入）
from cst import results as cstr            # 假 cst.results

from eaopt import artifacts
from eaopt.solver import cst as C
from eaopt.solver import cst_model as M
from eaopt.solver import cst_results as R
from eaopt.solver.cst_setup import COUPLER

from conftest import make_compact_cfg


def _quiet(*_a, **_kw):
    pass


@pytest.fixture
def cfg(tmp_path):
    cfg = make_compact_cfg(tmp_path)
    cfg.objective.from_port = 1
    cfg.objective.to_port = 3
    return cfg


def _open_arm():
    """两端停靠设计区左右边界的开放臂（上下两条边）——pipeline 给的正是它。"""
    cross = np.array([0.0, 2.0, 4.0])
    return [np.column_stack([cross, np.full(3, 0.1)]),
            np.column_stack([cross, np.full(3, -0.1)])]


def _solver(cfg, **kw):
    return C.CstSolver(cfg, log=_quiet, **kw)


def _project(cfg, tag):
    """假 CST 里当前打开的该 tag 工程。"""
    want = str(COUPLER.project_path(tag, cfg.output.dir, cfg.name).resolve()).lower()
    for prj in csti.state().projects:
        got = prj.filename()
        if got and str(Path(got).resolve()).lower() == want:
            return prj
    raise AssertionError(f"假 CST 里没有 {tag} 工程")


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
def test_no_connection_until_something_is_asked_of_cst(cfg):
    """构造 CstSolver 不该连 CST（装配/导入阶段不碰外部资源）。"""
    _solver(cfg)
    assert csti.state().events == []


def test_missing_projects_without_auto_init_point_at_the_init_scripts(cfg):
    """静默建工程只允许显式打开；默认报错并告诉你去跑哪个脚本。"""
    s = _solver(cfg)
    with pytest.raises(FileNotFoundError, match="cst_init_fwd.py"):
        s.solve_forward()               # 两个工程都缺 → 先报 fwd

    # 只把 fwd 放到位：这时缺的是 bwd，提示也要跟着换（不是死报一个）
    path = COUPLER.project_path("fwd", cfg.output.dir, cfg.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="cst_init_bwd.py"):
        _solver(cfg).build_model(_open_arm(), [])


def test_auto_init_builds_both_projects_with_the_full_template(cfg):
    """auto_init：两个工程就地建好，模板命令块**全部**进历史表。"""
    s = _solver(cfg, auto_init=True)
    s.build_model(_open_arm(), [])

    assert len(csti.state().projects) == 2
    for tag, port in (("fwd", cfg.objective.from_port),
                      ("bwd", cfg.objective.to_port)):
        prj = _project(cfg, tag)
        expected = len(M.template_blocks(f"{cfg.name}_{tag}", port))
        # 模板 + 本轮那一条形状更新
        assert len(prj.model3d.history) == expected + 1
        assert f".StimulationPort \"{port}\"" in "\n".join(
            cmd for _h, cmd in prj.model3d.history[:expected])


def test_close_saves_but_never_shuts_the_instance(cfg):
    """常驻会话：close 只存盘；关掉实例会让下一次运行重新起一个。"""
    s = _solver(cfg, auto_init=True)
    s.build_model(_open_arm(), [])
    saves = [e for e in csti.state().events if e[0] == "save"]
    s.close()
    assert len([e for e in csti.state().events if e[0] == "save"]) == len(saves) + 2
    assert csti.state().env_closed is False


def test_close_without_connecting_is_a_noop(cfg):
    """pipeline 在 finally 里调 close：没连过也要能安全收尾。"""
    _solver(cfg).close()
    assert csti.state().events == []


def test_update_reuses_the_open_project_instead_of_reopening(cfg):
    """同一个 .cst 绝不重复打开（两个工程对象会各改各的、互相覆盖存盘）。"""
    s = _solver(cfg, auto_init=True)
    s.build_model(_open_arm(), [])
    s.solve_forward()
    s.build_model(_open_arm(), [])

    assert csti.state().events.count(("new_mws",)) == 2      # 只建了两个工程
    assert not [e for e in csti.state().events if e[0] == "open_project"]
    assert len(csti.state().projects) == 2


def test_bad_objective_port_fails_at_assembly(cfg):
    """端口表在 CST 模板里：装配时就校验，别等建完工程才炸。"""
    cfg.objective.to_port = 99
    with pytest.raises(ValueError, match="to_port"):
        _solver(cfg)


# --------------------------------------------------------------------------- #
# 改形状
# --------------------------------------------------------------------------- #
def test_one_history_record_per_project_per_iteration(cfg):
    """每轮每工程**恰好一条**历史记录——多了会让历史表爆掉，少了形状没变。"""
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(0)
    s.build_model(_open_arm(), [])
    n0 = [len(_project(cfg, t).model3d.history) for t in artifacts.TAGS]

    s.begin_iteration(1)
    s.build_model(_open_arm(), [])
    n1 = [len(_project(cfg, t).model3d.history) for t in artifacts.TAGS]

    assert [b - a for a, b in zip(n0, n1)] == [1, 1]
    for tag in artifacts.TAGS:
        _h, cmd = _project(cfg, tag).model3d.history[-1]
        assert 'Component.Delete "design_region"' in cmd
        assert "Extrude" in cmd


def test_build_model_closes_open_contours_and_records_the_applied_shape(cfg):
    """穿出设计区两端的开放路径必须先闭合；shape.json 记的是真正施加的形状。"""
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(3)
    s.build_model(_open_arm(), fixed=[np.zeros((3, 2))])   # fixed 被忽略

    polys = artifacts.load_shape(artifacts.iteration_dir(cfg.output.dir, 3))
    (poly,) = polys
    assert np.allclose(poly[0], poly[-1])                  # 首尾相接
    box = cfg.design_region.box
    pad = 0.1                                              # 端点外扩一点点
    assert poly[:, 0].min() >= box.x[0] - pad
    assert poly[:, 0].max() <= box.x[1] + pad


def test_shape_json_is_overwritten_with_the_latest_shape(cfg):
    """重跑同一轮：磁盘上的形状必须等于工程里的形状（覆盖写）。"""
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(0)
    s.build_model(_open_arm(), [])
    first = artifacts.load_shape(artifacts.iteration_dir(cfg.output.dir, 0))[0]

    want = np.array([[0.5, 0.3], [3.5, 0.3], [3.5, 0.2],
                     [0.5, 0.2], [0.5, 0.3]])                # 闭合多边形
    s.build_model([want], [])
    second = artifacts.load_shape(artifacts.iteration_dir(cfg.output.dir, 0))[0]
    assert len(second) != len(first)        # 确实覆盖写了（点数都不一样）
    assert np.allclose(second, want)        # 且等于这一轮真正施加的形状


def test_without_begin_iteration_nothing_is_written(cfg):
    """没给轮次就不落盘（迭代号未知时宁可不写，也不写错目录）。"""
    s = _solver(cfg, auto_init=True)
    s.build_model(_open_arm(), [])
    s.solve_forward()

    d = artifacts.iteration_dir(cfg.output.dir, 0)
    assert not (d / "shape.json").exists()
    assert not (d / "s_params_fwd.json").exists()


# --------------------------------------------------------------------------- #
# 求解 / 取结果
# --------------------------------------------------------------------------- #
def test_forward_and_backward_read_their_own_stimulus_port(cfg):
    """fwd 读 S{i,1}、bwd 读 S{i,3}：读错端口照样出数，但模型是错的。"""
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(0)
    s.build_model(_open_arm(), [])

    cstr.state().asked.clear()
    s.solve_forward()
    assert cstr.state().asked == [R.s_param_item(i, cfg.objective.from_port)
                                  for i in COUPLER.ports]

    cstr.state().asked.clear()
    s.solve_backward()
    assert cstr.state().asked == [R.s_param_item(i, cfg.objective.to_port)
                                  for i in COUPLER.ports]


def test_solving_saves_before_reading_results(cfg):
    """cst.results 读的是磁盘结果：存盘必须排在读结果之前。

    这里的假库照真库的规矩——工程文件不在就读不到。所以把文件删掉再求解：
    顺序错了会直接 FileNotFoundError，顺序对了则存盘把它写回来。
    """
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(0)
    s.build_model(_open_arm(), [])
    path = C.CstSolver(cfg, log=_quiet).project_path("fwd")
    path.unlink()

    sol = s.solve_forward()
    assert path.is_file()
    assert sol.s_params


def test_fields_are_exported_every_solve_and_cropped_to_the_design_region(cfg):
    """场每轮都导（伴随法要用），并裁到设计区 ± 余量。"""
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(0)
    s.build_model(_open_arm(), [])
    sol = s.solve_forward()

    prj = _project(cfg, "fwd")
    kinds = [c[0] for c in prj.model3d.ASCIIExport.calls]
    assert kinds.count("Execute") == 2                  # E 场与 H 场各导一次
    assert kinds[0] == "Reset"                          # Reset 必须最先
    assert ("Mode", "FixedWidth") in prj.model3d.ASCIIExport.calls
    assert prj.model3d.selected == [M.field_result_path("Efield", 5.0),
                                    M.field_result_path("Hfield", 5.0)]

    box = cfg.design_region.box
    m = float(cfg.design_region.field_margin_mm)
    for grid in (sol.e_field, sol.h_field):
        ax = grid.axes()
        assert box.x[0] - m - 1e-9 <= ax[0][0] and ax[0][-1] <= box.x[1] + m + 1e-9
        assert box.y[0] - m - 1e-9 <= ax[1][0] and ax[1][-1] <= box.y[1] + m + 1e-9
        assert np.any(grid.data != 0)                   # 数值来自导出文件


def test_save_fields_switch_only_controls_the_npz(cfg):
    """``save_fields`` 只决定写不写 .npz——场本身照导（求解器契约要它）。"""
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(0)
    s.build_model(_open_arm(), [])
    sol = s.solve_forward()                             # COUPLER.save_fields=False
    d = artifacts.iteration_dir(cfg.output.dir, 0)
    assert sol.e_field is not None and sol.h_field is not None
    assert (d / "s_params_fwd.json").is_file()
    assert not (d / "fields_fwd_e.npz").exists()

    s2 = _solver(cfg, auto_init=True, save_fields=True)
    s2.begin_iteration(1)
    s2.build_model(_open_arm(), [])
    s2.solve_forward()
    d1 = artifacts.iteration_dir(cfg.output.dir, 1)
    assert (d1 / "fields_fwd_e.npz").is_file()
    assert (d1 / "fields_fwd_h.npz").is_file()


def test_a_full_solve_writes_the_iteration_dir(cfg):
    """两个 tag 都求解完，iter_NNN/ 就是一个完整的轮次。"""
    s = _solver(cfg, auto_init=True)
    s.begin_iteration(0)
    s.build_model(_open_arm(), [])
    s.solve_forward()
    s.solve_backward()
    s.close()

    assert artifacts.latest_iteration(cfg.output.dir) == 0
    d = artifacts.iteration_dir(cfg.output.dir, 0)
    sp = artifacts.load_s_params(cfg.output.dir, 0, "fwd")
    assert set(sp) == {(i, cfg.objective.from_port) for i in COUPLER.ports}
    assert (d / "shape.json").is_file()
    assert (d / "meta.json").is_file()
