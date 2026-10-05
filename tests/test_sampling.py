"""边界采样与栅格化测试（contour 旧方案 + nodes 节点方案）。"""

import numpy as np
import pytest

from eaopt.adjoint.fields import FieldGrid
from eaopt.adjoint.sampling import (boundary_nodes, boundary_row,
                                    node_field_indices, sample_boundary,
                                    sample_nodes, scatter_to_grid,
                                    spread_to_grid)
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


# ---------------------------------------------------------------------- #
# nodes 方案：采样点 = 边界网格节点
# ---------------------------------------------------------------------- #
def _lattice_field(ls, nz=3, dz=0.5, z0=-0.5):
    """与水准集 x/y 网格**同源**的场（原点是设计区原点、步长是 ls.dx）。"""
    shp = (ls.nx, ls.ny, nz, 3)
    return FieldGrid(origin=(ls.xs[0], ls.ys[0], z0), spacing=(ls.dx, ls.dx, dz),
                     data=np.zeros(shp, dtype=complex))


def test_boundary_row_is_one_line_on_the_chosen_side(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    row = boundary_row(ls.phi, ls.dx, cfg.sampling.offset_cells, "outside")
    # 臂上边缘 y=0 正好落在格线上：这一排就是边界节点本身
    j = int(round((0.0 - ls.ys[0]) / ls.dx))
    assert row[:, j].all()
    assert not row[:, j + 1].any()        # 再外一排（φ=dx）不在带内
    # 单侧：内侧节点不在带内（x=0/4 两列是臂贴设计区边框的竖直边界，另算）
    assert not row[1:-1, j - 1].any()
    assert ls.phi[row].min() >= -1e-9     # 一整排的 φ 都落在 [0, dx)
    assert ls.phi[row].max() < ls.dx      # 带外没有第二排混进来

    # 边界压在格线上时内外两侧的"第 1 排"是同一排（φ≈0 的节点两侧同属），
    # 但更内侧的一排不该入选
    inside = boundary_row(ls.phi, ls.dx, 1, "inside")
    assert inside[:, j].all()
    assert not inside[1:-1, j - 1].any()


def test_boundary_row_moves_outward_with_offset(ls, tmp_path):
    f = boundary_row(ls.phi, ls.dx, 1, "outside")
    g = boundary_row(ls.phi, ls.dx, 2, "outside")
    assert not np.any(f & g)              # 两排互不重叠
    assert g.sum() > 0                    # 第二排存在（臂上方是空气）


def test_boundary_nodes_are_ordered_lattice_nodes_without_fixed_metal(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    polys = boundary_nodes(ls, cfg)
    assert polys
    pts = np.vstack(polys)
    # 全在格点上
    for k, ax0 in ((0, ls.xs[0]), (1, ls.ys[0])):
        r = (pts[:, k] - ax0) / ls.dx
        assert np.abs(r - np.rint(r)).max() < 1e-9
    # 固定直通线（y ∈ [0.4, 1.0]）的边界不参与
    assert pts[:, 1].max() < 0.35
    # 单排：只走 y=0（臂上边缘）/ y=-0.6（下边缘）/ x=0,4（框中两条竖直边）
    ys = set(np.round(pts[:, 1], 6))
    assert ys <= {round(-0.1 * k, 6) for k in range(7)}
    assert 0.0 in ys and 0.1 not in ys
    # 折线连续：相邻点最多隔一格对角（不跳、不来回穿边界）
    for p in polys:
        d = np.hypot(*np.diff(p, axis=0).T)
        assert d.max() <= np.sqrt(2.0) * ls.dx + 1e-9


def test_sample_nodes_are_the_contour_vertices(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    polys = boundary_nodes(ls, cfg)
    pts, ixy = sample_nodes(ls, polys)
    # 采样点 = 折线顶点（去重后的一一对应）
    assert len(pts) == len(ixy)
    assert len(pts) <= sum(len(p) - int(np.allclose(p[0], p[-1])) for p in polys)
    assert len({(int(i), int(j)) for i, j in ixy}) == len(ixy)   # 无重复节点
    # 世界坐标 ↔ 网格下标一致，且严格落在节点上
    assert np.allclose(ls.xs[ixy[:, 0]], pts[:, 0])
    assert np.allclose(ls.ys[ixy[:, 1]], pts[:, 1])
    # 采到的正是边界那一排
    row = boundary_row(ls.phi, ls.dx, cfg.sampling.offset_cells, "outside")
    assert row[ixy[:, 0], ixy[:, 1]].all()


def test_node_field_indices_reads_exact_lattice_position(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    f = _lattice_field(ls)
    pts, _ = sample_nodes(ls, boundary_nodes(ls, cfg))
    ix, iy, iz, z_used = node_field_indices(f, pts, 0.0)
    assert np.allclose(f.axes()[0][ix], pts[:, 0])
    assert np.allclose(f.axes()[1][iy], pts[:, 1])
    assert z_used == pytest.approx(0.0) and iz == 1        # z 面 −0.5 / 0.0 / 0.5
    # 数据按索引直接取：场值等于节点上的值
    f.data[ix, iy, iz] = np.arange(len(ix))[:, None]
    assert np.array_equal(f.data[ix, iy, iz][:, 0], np.arange(len(ix)))


def test_node_field_indices_snaps_z_to_nearest_plane(ls, tmp_path):
    # 真实导出网格：z 从 −0.6985 起、步长 0.2 —— z=0 不在面上
    f = _lattice_field(ls, nz=5, dz=0.2, z0=-0.6985)
    pts = np.array([[1.0, 0.0]])
    _, _, iz, z_used = node_field_indices(f, pts, 0.0)
    assert z_used == pytest.approx(-0.0985) and iz == 3


def test_node_field_indices_rejects_off_lattice_points(ls):
    f = _lattice_field(ls)
    with pytest.raises(ValueError, match="不在场导出网格节点上"):
        node_field_indices(f, np.array([[1.0, 0.05]]), 0.0)     # y 方向差半格
    with pytest.raises(ValueError, match="不在场导出网格节点上"):
        node_field_indices(f, np.array([[1.05, 0.0]]), 0.0)     # x 方向差半格
    with pytest.raises(ValueError, match="不在场导出网格节点上"):
        node_field_indices(f, np.array([[9.9, 0.0]]), 0.0)      # 出界


def test_spread_to_grid_takes_nearest_sample_value(ls):
    ixy = np.array([[10, 10], [20, 10], [30, 10]])   # (1,0) 每 2 格一个
    v = np.array([1.0, 2.0, 4.0])
    V = spread_to_grid(ls, ixy, v)
    assert V[10, 10] == 1.0 and V[20, 10] == 2.0 and V[12, 10] == 1.0
    assert V[19, 10] == 2.0       # 取最近采样点，不平均
    assert V.shape == ls.phi.shape
