"""``scripts/cst_init_fwd.py`` / ``cst_init_bwd.py``：建工程 + 跑第一次仿真。

这两个程序是**给服务器人工跑**的入口（本机靠假 cst.interface 跑）。这里锁的
是"跑完之后磁盘上该有什么、不该有什么"，以及几条出错了也不报错的纪律：

  * 激励端口在建工程时写死（fwd=1、bwd=3），命令块全部进历史表；
  * init **不写** shape.json——首轮形状就是模板里的 initial_metal；
  * 两个 init 都跑完，iter_000 才算完成（``artifacts.list_iterations`` 的
    判据落在文件上，不看 meta 里少了什么）；
  * 工程已存在就报错停下（绝不覆盖别人正在用的工程）；
  * ``--attach`` 连不上必须报错，绝不偷偷起一个新实例。
"""

import importlib.util

import pytest

from cst import interface as csti

from eaopt import artifacts
from eaopt.config import CaseConfig
from eaopt.solver import cst_model as M
from eaopt.solver.cst_setup import COUPLER

from conftest import REPO_ROOT, load_script, write_case_yaml

#: 三个 CST 程序都是"import 时零副作用"，收集阶段就能加载
INIT = {tag: load_script(f"cst_init_{tag}") for tag in artifacts.TAGS}


@pytest.fixture
def case(tmp_path):
    """一份紧凑算例的 YAML（脚本只认 YAML 路径）。"""
    path = write_case_yaml(tmp_path)
    return path, CaseConfig.from_yaml(path)


def _project(tag: str):
    """假 CST 里当前打开的那个 tag 工程。"""
    for prj in csti.state().projects:
        if (prj.filename() or "").lower().endswith(f"_{tag}.cst"):
            return prj
    raise AssertionError(f"假 CST 里没有 {tag} 工程")


def _history(prj) -> str:
    return "\n".join(cmd for _h, cmd in prj.model3d.history)


# --------------------------------------------------------------------------- #
# 建工程
# --------------------------------------------------------------------------- #
def test_init_fwd_builds_the_project_and_runs_the_first_simulation(case):
    yaml_path, cfg = case
    assert INIT["fwd"].main([str(yaml_path)]) == 0

    path = COUPLER.project_path("fwd", cfg.output.dir, cfg.name)
    assert path.is_file()
    prj = _project("fwd")
    # 历史表 = 模板的全部命令块（没有多余的一条形状更新）
    assert len(prj.model3d.history) == len(M.template_blocks(f"{cfg.name}_fwd", 1))
    assert prj.model3d.solves == 1
    assert '.StimulationPort "1"' in _history(prj)

    sp = artifacts.load_s_params(cfg.output.dir, 0, "fwd")
    assert set(sp) == {(i, 1) for i in COUPLER.ports}
    assert (artifacts.iteration_dir(cfg.output.dir, 0) / "meta.json").is_file()


def test_init_bwd_uses_the_observation_port(case):
    """bwd 激励端口 3 = 论文的伴随仿真（读 S{i,3}）。"""
    yaml_path, cfg = case
    assert INIT["bwd"].main([str(yaml_path)]) == 0

    assert '.StimulationPort "3"' in _history(_project("bwd"))
    sp = artifacts.load_s_params(cfg.output.dir, 0, "bwd")
    assert set(sp) == {(i, 3) for i in COUPLER.ports}


def test_init_does_not_write_a_shape_file(case):
    """首轮形状 = 模板里的 initial_metal，init 不该留 shape.json。"""
    yaml_path, cfg = case
    INIT["fwd"].main([str(yaml_path)])
    assert not (artifacts.iteration_dir(cfg.output.dir, 0) / "shape.json").exists()


def test_only_both_inits_together_complete_iter_000(case):
    """一个工程跑完还不算完成：续跑起点靠"两个 tag 的 S 参数都在"。"""
    yaml_path, cfg = case
    assert INIT["fwd"].main([str(yaml_path)]) == 0
    assert artifacts.list_iterations(cfg.output.dir) == []

    assert INIT["bwd"].main([str(yaml_path)]) == 0
    assert artifacts.list_iterations(cfg.output.dir) == [0]


def test_a_second_init_on_the_same_config_is_refused(case):
    """工程已存在 → 报错退出（绝不覆盖，也不偷偷再建一个）。"""
    yaml_path, _ = case
    assert INIT["fwd"].main([str(yaml_path)]) == 0
    assert INIT["fwd"].main([str(yaml_path)]) == 1
    assert len(csti.state().projects) == 1
    assert csti.state().events.count(("new_mws",)) == 1


def test_a_bad_template_block_stops_before_saving(case):
    """模板块写不进历史表就当场停：半成品工程不落盘。"""
    yaml_path, cfg = case
    csti.configure(history_error=("*", RuntimeError("VBA 语法错误")))
    assert INIT["fwd"].main([str(yaml_path)]) == 1
    assert not COUPLER.project_path("fwd", cfg.output.dir, cfg.name).is_file()


# --------------------------------------------------------------------------- #
# 连实例
# --------------------------------------------------------------------------- #
def test_default_connection_reuses_the_running_instance(case):
    """默认先附接：有实例就复用（常驻会话），绝不另起一个。"""
    yaml_path, _ = case
    INIT["fwd"].main([str(yaml_path)])
    INIT["bwd"].main([str(yaml_path)])
    assert csti.state().events.count(("connect_to_any",)) == 2
    assert ("new", ()) not in csti.state().events


def test_force_new_skips_the_attach_attempt(case):
    """``--new`` 直接起静态实例（附接被怀疑污染环境时用）。"""
    yaml_path, _ = case
    assert INIT["fwd"].main([str(yaml_path), "--new"]) == 0
    assert ("new", ()) in csti.state().events
    assert ("connect_to_any",) not in csti.state().events


def test_attach_without_a_running_instance_fails_loudly(case):
    """``--attach`` 是"只许附接"：连不上就退出，不许偷偷起新实例。"""
    yaml_path, _ = case
    csti.configure(fail_connect=True)
    assert INIT["fwd"].main([str(yaml_path), "--attach"]) == 1
    assert csti.state().projects == []


def test_quiet_mode_is_switched_on_when_connecting(case):
    """连上就切静默，且排在第一次求解**之前**（否则框已经弹出来了）。

    新建的工程没有旧结果、本不会弹"是否删除结果"的确认框，但求解设置里
    别的告警框一样会等人点——静默模式是无人值守的前提。
    """
    yaml_path, _ = case
    assert INIT["fwd"].main([str(yaml_path)]) == 0
    ev = [e[0] for e in csti.state().events]
    assert csti.state().quiet_mode is True
    assert ev.index("set_quiet_mode") < ev.index("run_solver")


def test_a_cst_without_the_quiet_api_still_initializes(case):
    """老版本没有 ``set_quiet_mode``：告警跳过，工程照建照跑（不能因此挂）。"""
    yaml_path, cfg = case
    csti.configure(has_quiet_mode_api=False)
    assert INIT["fwd"].main([str(yaml_path)]) == 0
    assert COUPLER.project_path("fwd", cfg.output.dir, cfg.name).is_file()


def test_solver_failure_returns_nonzero(case):
    yaml_path, _ = case
    csti.configure(solve_error=RuntimeError("网格太粗，求解器拒绝"))
    assert INIT["fwd"].main([str(yaml_path)]) == 1


# --------------------------------------------------------------------------- #
# import 时零副作用（load_script 的前提）
# --------------------------------------------------------------------------- #
def test_importing_a_script_does_not_touch_cst():
    path = REPO_ROOT / "scripts" / "cst_init_fwd.py"
    spec = importlib.util.spec_from_file_location("_fresh_cst_init_fwd", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert csti.state().events == []
