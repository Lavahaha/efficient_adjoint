"""边界采样与栅格化测试。"""

import numpy as np
import pytest

from eaopt.adjoint.sampling import sample_boundary, scatter_to_grid
from eaopt.geometry.contour import extract_contours
from eaopt.pipeline import make_level_set

from conftest import make_compact_cfg


@pytest.fixture
def ls(tmp_path):
    return make_level_set(make_compact_cfg(tmp_path))


def test_sample_boundary_offsets_outside_with_outward_normals(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    contours = extract_contours(ls.xs, ls.ys, ls.phi)
    pts, nrm = sample_boundary(contours, ls, cfg)
    # 臂上边缘的采样点：y ≈ +0.06（外侧偏移），法向 (0,1)
    off = cfg.sampling.sample_offset_mm
    sel = (pts[:, 1] > off - 0.03) & (pts[:, 1] < off + 0.03) & (pts[:, 0] > 0.5) & (pts[:, 0] < 3.5)
    assert sel.sum() > 10
    assert np.abs(pts[sel][:, 1] - off).max() < 0.03
    assert np.allclose(nrm[sel][:, 1], 1.0, atol=0.1)


def test_sample_boundary_spacing(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    contours = extract_contours(ls.xs, ls.ys, ls.phi)
    pts, _ = sample_boundary(contours, ls, cfg)
    off = cfg.sampling.sample_offset_mm
    sel = (pts[:, 1] > off - 0.03) & (pts[:, 1] < off + 0.03)
    x_sorted = np.sort(pts[sel][:, 0])
    d = np.diff(x_sorted)
    assert np.abs(d - cfg.sampling.point_spacing_mm).max() < 0.05


def test_scatter_to_grid_assigns_and_averages(ls):
    p = np.array([[2.0, 0.2], [2.04, 0.2], [1.0, 0.0]])  # 前两点同最近节点
    v = scatter_to_grid(ls, p, np.array([3.0, 5.0, 7.0]))
    ix = int(round((2.0 - ls.box.x[0]) / ls.dx))
    iy = int(round((0.2 - ls.box.y[0]) / ls.dx))
    assert v[ix, iy] == pytest.approx(4.0)  # (3+5)/2
    ix2 = int(round((1.0 - ls.box.x[0]) / ls.dx))
    iy2 = int(round((0.0 - ls.box.y[0]) / ls.dx))
    assert v[ix2, iy2] == pytest.approx(7.0)
