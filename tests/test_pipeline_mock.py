"""优化闭环测试（mock 端到端）。"""

import json

import numpy as np

from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import apply_min_gap
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver.mock import MockSolver

from conftest import make_compact_cfg


def _run(tmp_path, velocity_sign):
    cfg = make_compact_cfg(tmp_path, min_gap=0.1, velocity_sign=velocity_sign)
    ls = make_level_set(cfg)
    solver = MockSolver(cfg, ls)
    return OptimizerPipeline(cfg, solver, ls).run()


def test_pipeline_raises_fom_with_positive_sign(tmp_path):
    h = _run(tmp_path, velocity_sign=1.0)
    foms = [r["fom"] for r in h.records]
    assert len(h.records) >= 3
    assert foms[-1] > foms[0]
    # 注：不断言"未收敛"——mock 的量化 FoM 在相邻迭代间完全相同，
    # 会误触发收敛窗口（对 CST 的平滑 S 参数则不会），这是 mock 固有局限


def test_pipeline_drops_fom_with_negative_sign(tmp_path):
    # 需更多迭代：mock 的电容-几何曲线有栅格化锯齿（局部尖峰非单调），
    # 收缩趋势要跑远一点才能压过局部抖动（真实求解器 S 参数平滑，无此问题）
    cfg = make_compact_cfg(tmp_path, min_gap=0.1, velocity_sign=-1.0,
                           max_iterations=20)
    ls = make_level_set(cfg)
    solver = MockSolver(cfg, ls)
    h = OptimizerPipeline(cfg, solver, ls).run()
    foms = [r["fom"] for r in h.records]
    assert foms[-1] < foms[0]


def test_pipeline_writes_artifacts(tmp_path):
    h = _run(tmp_path, velocity_sign=1.0)
    outdir = tmp_path / "results"
    lines = (outdir / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(h.records)
    rec = json.loads(lines[0])
    assert set(rec) >= {"iteration", "fom", "s31_db", "s21_db", "time_s"}
    assert (outdir / "iter_000.png").exists()
    assert (outdir / "fom.png").exists()


def test_min_gap_respected_after_run(tmp_path):
    cfg = make_compact_cfg(tmp_path, min_gap=0.1, velocity_sign=1.0,
                           max_iterations=20)
    ls = make_level_set(cfg)
    solver = MockSolver(cfg, ls)
    OptimizerPipeline(cfg, solver, ls).run()
    tmp = LevelSet2D(ls.box, ls.dx)
    tmp.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region])
    dist = np.abs(tmp.phi)
    prohibited = (tmp.phi > 0) & (dist < cfg.constraints.min_gap_mm)
    assert not np.any((ls.phi < 0) & prohibited)
