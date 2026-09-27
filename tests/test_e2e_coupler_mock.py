"""端到端验证（论文完整耦合器配置 + mock）。"""

from pathlib import Path

import numpy as np

from eaopt.adjoint.fd_check import fd_check
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import LevelSet2D
from eaopt.pipeline import OptimizerPipeline, make_level_set
from eaopt.solver.mock import MockSolver

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "coupler.yaml"


def _load_cfg(tmp_path, max_iterations: int = 12) -> CaseConfig:
    cfg = CaseConfig.from_yaml(CFG_PATH)
    cfg.output.dir = str(tmp_path / "results")
    cfg.optimizer.max_iterations = max_iterations
    return cfg


def _prohibited_check(ls, cfg) -> bool:
    """是否有可动金属侵入固定金属的 min_gap 禁区。"""
    tmp = LevelSet2D(ls.box, ls.dx)
    tmp.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region])
    dist = np.abs(tmp.phi)
    prohibited = (tmp.phi > 0) & (dist < cfg.constraints.min_gap_mm)
    return bool(np.any((ls.phi < 0) & prohibited))


def test_e2e_coupler_pipeline(tmp_path):
    cfg = _load_cfg(tmp_path)
    ls = make_level_set(cfg)
    solver = MockSolver(cfg, ls)
    h = OptimizerPipeline(cfg, solver, ls).run()

    foms = [r["fom"] for r in h.records]
    assert len(h.records) >= 3
    assert foms[-1] > foms[0]  # FoM 上升

    # 最小间距硬约束
    assert not _prohibited_check(ls, cfg)

    # allowed_region 约束：可动金属不得越界
    b = cfg.constraints.allowed_region
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    metal = ls.phi < 0
    inside = (X >= b.x[0]) & (X <= b.x[1]) & (Y >= b.y[0]) & (Y <= b.y[1])
    fixed_mask = metal & inside
    outside = metal & ~inside
    # 设计区外没有网格，检查的是 allowed_region 外的金属（直通线在 y>1，
    # 属 allowed_region 外但为固定金属——允许）。故只查可动金属：
    # 可动金属 = 金属 且 不在固定区内
    tmp = LevelSet2D(ls.box, ls.dx)
    tmp.init_from_polygons([np.asarray(p.vertices, dtype=float) for p in cfg.fixed_region])
    in_fixed = tmp.phi < 0
    movable = metal & ~in_fixed
    assert not np.any(movable & ~inside)

    # 产物完整
    outdir = Path(cfg.output.dir)
    assert (outdir / "history.jsonl").exists()
    assert (outdir / "fom.png").exists()
    assert (outdir / "iter_000.png").exists()


def test_fd_check_coupler_config(tmp_path):
    cfg = _load_cfg(tmp_path, max_iterations=1)
    ls = make_level_set(cfg)
    solver = MockSolver(cfg, ls)
    r = fd_check(cfg, ls, solver)
    assert r["sign_ok"]  # 符号裁决：velocity_sign=+1
    assert r["deviation"] < 0.5  # 比值与理论值偏差 < 50%
