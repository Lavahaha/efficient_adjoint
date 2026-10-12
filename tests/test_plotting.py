"""结果图：iter_NNN 两栏 / S 参数曲线（论文 Fig.11）/ 电场演化（论文 Fig.10）。

只做冒烟 + 关键约定，不比像素（换个字体就红）：① 三张图真的写出来且非空；
② 数据全部来自 ``iter_NNN/`` 的落盘产物——所以事后重画脚本
（scripts/plot_results.py）与 pipeline 跑完时用的是同一份代码；
③ |E| 就是矢量模、切片必须单层（图与 δp 取场是同一个 z 面）。
"""

import numpy as np
import pytest

from eaopt import artifacts as A
from eaopt import plotting
from eaopt.adjoint.fields import FieldGrid
from eaopt.pipeline import make_level_set
from eaopt.solver.base import Solution

from conftest import make_compact_cfg


def _slice(cfg, value=1.0) -> FieldGrid:
    """一张覆盖设计区的单层场切片（x 分量 value、z 分量 2j·value）。"""
    box = cfg.design_region.box
    h = 0.25
    nx = int(round(box.width / h)) + 1
    ny = int(round(box.height / h)) + 1
    data = np.zeros((nx, ny, 1, 3), dtype=complex)
    data[:, :, 0, 0] = value
    data[:, :, 0, 2] = 2j * value
    return FieldGrid(origin=(box.x[0], box.y[0], -0.0985),
                     spacing=(h, h, 0.1), data=data)


def _write_iter(outdir, cfg, n: int, ls, *, s31=0.5, s21=0.1,
                with_slice=False) -> None:
    """写一个"已完成"的轮次（list_iterations 认的判据：两个 tag 的 S 参数都在）。"""
    sol = Solution(s_params={(3, 1): s31 + 0j, (2, 1): s21 + 0j},
                   e_field=None, h_field=None, pin=0.5)
    for tag in ("fwd", "bwd"):
        A.save_solution(outdir, n, tag, sol, save_fields=False)
    A.save_ls_phi(outdir, n, ls)
    if with_slice:
        A.save_field_slice(outdir, n, "fwd", _slice(cfg))


# ---------------------------------------------------------------------- #
# |E| 与场图
# ---------------------------------------------------------------------- #
def test_field_magnitude_is_the_vector_norm():
    data = np.zeros((2, 2, 1, 3), dtype=complex)
    data[0, 0, 0] = [3.0, 4j, 12.0]          # |E| = sqrt(9+16+144) = 13
    grid = FieldGrid(origin=(0.0, 0.0, 0.0), spacing=(0.1, 0.1, 0.1), data=data)
    mag = plotting.field_magnitude(grid)
    assert mag.shape == (2, 2)
    assert mag[0, 0] == pytest.approx(13.0)
    assert mag[1, 1] == 0.0

    with pytest.raises(ValueError, match="单层"):
        plotting.field_magnitude(FieldGrid(origin=(0.0, 0.0, 0.0),
                                           spacing=(0.1, 0.1, 0.1),
                                           data=np.zeros((2, 2, 2, 3), complex)))


def test_snapshot_figure_two_panels_and_one_without_a_slice(tmp_path):
    """有切片 = 两栏（+色标），没有 = 单栏：旧轮次重画也走得通。"""
    cfg = make_compact_cfg(tmp_path)
    ls = make_level_set(cfg)

    two = tmp_path / "two.png"
    fig = plotting.snapshot_figure(cfg, ls, it=3, e_slice=_slice(cfg),
                                   z_used=-0.0985, path=two)
    assert two.stat().st_size > 1000
    assert len(fig.axes) == 3                 # 左栏 + 右栏 + 色标

    one = tmp_path / "one.png"
    fig = plotting.snapshot_figure(cfg, ls, it=0, path=one)
    assert one.stat().st_size > 1000
    assert len(fig.axes) == 1


# ---------------------------------------------------------------------- #
# S 参数曲线（论文 Fig.11）
# ---------------------------------------------------------------------- #
def test_sparams_curves_and_figure_use_completed_iterations_only(tmp_path):
    cfg = make_compact_cfg(tmp_path)          # FoM = |S31|（objective from 1 to 3）
    outdir = tmp_path / "results"
    ls = make_level_set(cfg)
    _write_iter(outdir, cfg, 0, ls, s31=0.5, s21=0.1)
    _write_iter(outdir, cfg, 1, ls, s31=0.8, s21=0.05)
    (A.iteration_dir(outdir, 2)).mkdir(parents=True)      # 半截轮次：不算

    its, series = plotting.sparams_curves(outdir, cfg)
    assert its == [0, 1]
    assert set(series) == {(3, 1), (2, 1)}
    assert series[(3, 1)] == pytest.approx([20 * np.log10(0.5), 20 * np.log10(0.8)])
    assert series[(2, 1)] == pytest.approx([20 * np.log10(0.1), 20 * np.log10(0.05)])

    path = plotting.sparams_figure(cfg, outdir, outdir / "sparams_vs_iter.png")
    assert path.is_file() and path.stat().st_size > 1000


def test_sparams_figure_skips_an_empty_directory(tmp_path, capsys):
    cfg = make_compact_cfg(tmp_path)
    outdir = tmp_path / "results"
    outdir.mkdir()
    assert plotting.sparams_figure(cfg, outdir, outdir / "x.png") is None
    assert "跳过" in capsys.readouterr().out


# ---------------------------------------------------------------------- #
# 电场演化（论文 Fig.10）
# ---------------------------------------------------------------------- #
def test_efield_evolution_needs_slices_and_reuses_them(tmp_path, capsys):
    cfg = make_compact_cfg(tmp_path)
    outdir = tmp_path / "results"
    ls = make_level_set(cfg)
    for n in (0, 1, 2):
        _write_iter(outdir, cfg, n, ls)

    path = outdir / "efield_evolution.png"
    assert plotting.efield_evolution_figure(cfg, outdir, path) is None
    assert "跳过" in capsys.readouterr().out       # 旧轮次没有切片：提示而不是报错

    for n in (0, 2):                               # 只有两轮有切片 → 只画两格
        A.save_field_slice(outdir, n, "fwd", _slice(cfg, value=1.0 + n))
    assert plotting.efield_evolution_figure(cfg, outdir, path) is not None
    assert path.stat().st_size > 1000
