"""边界采样与回写测试（intersection 交点方案 + contour 旧方案）。"""

import numpy as np
import pytest

from eaopt.adjoint.sampling import (boundary_points, boundary_samples,
                                    field_node_mask, scatter_inverse_distance,
                                    sample_boundary, scatter_to_grid)
from eaopt.geometry.contour import extract_contours
from eaopt.pipeline import make_level_set

from conftest import make_compact_cfg


@pytest.fixture
def ls(tmp_path):
    return make_level_set(make_compact_cfg(tmp_path))


def _slanted_ls(cfg):
    """斜界面 φ = y + 0.35 − 0.03x：交点落在格线**内部**（真的在做亚格点插值）。

    默认算例的金属是压着格线的矩形（交点恰好在节点上），验不出插值；这条
    斜线远离固定直通线（y≥0.4），不会被 is_fixed_contour 剔掉。
    """
    ls = make_level_set(cfg)
    X, Y = np.meshgrid(ls.xs, ls.ys, indexing="ij")
    ls.phi = Y + 0.35 - 0.03 * X
    return ls


# ---------------------------------------------------------------------- #
# contour 旧方案（留着 A/B，行为不变）
# ---------------------------------------------------------------------- #
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
# intersection 方案：marching 交点（亚格点）
# ---------------------------------------------------------------------- #
def test_boundary_points_are_sub_cell_crossings_on_grid_lines(tmp_path):
    """顶点由两端节点 φ 线性插值给出：恒落在网格线上，但**不在节点上**。"""
    cfg = make_compact_cfg(tmp_path)
    ls = _slanted_ls(cfg)
    polys = boundary_points(ls, cfg)
    assert len(polys) == 1                    # 一条从左边界穿到右边界的斜线
    p = polys[0]
    rx = (p[:, 0] - ls.xs[0]) / ls.dx
    ry = (p[:, 1] - ls.ys[0]) / ls.dx
    fx = np.abs(rx - np.rint(rx))
    fy = np.abs(ry - np.rint(ry))
    assert np.minimum(fx, fy).max() < 1e-9    # 每个顶点至少压在一根网格线上
    assert np.maximum(fx, fy).min() > 1e-3    # 但没有一个顶点落在节点上（走的是插值）
    d = np.hypot(*np.diff(p, axis=0).T)
    assert d.min() > 1e-9                     # 无零长段
    assert np.abs(p[:, 1] + 0.35 - 0.03 * p[:, 0]).max() < 1e-9   # 落在界面上


def test_boundary_points_dedupe_and_keep_closed_contours_joined(ls, tmp_path):
    """矩形金属（边压着格线）：闭合轮廓首尾精确相等、无零长段、剔掉固定区。"""
    cfg = make_compact_cfg(tmp_path)
    polys = boundary_points(ls, cfg)
    assert polys
    for p in polys:
        d = np.hypot(*np.diff(p, axis=0).T)
        assert d.min() > 1e-9
        if np.allclose(p[0], p[-1]):
            assert np.array_equal(p[0], p[-1])    # 精确相等（下游靠它判闭合）
    # 固定直通线（y ∈ [0.4, 1.0]）的边界不参与
    assert np.vstack(polys)[:, 1].max() < 0.4


def test_boundary_samples_are_the_intersections_themselves(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    polys = boundary_points(ls, cfg)
    pts, nrm = boundary_samples(polys, ls, cfg)
    # 闭合折线的重复点只采样一次（否则该点权重翻倍）
    n_expected = sum(len(p) - int(np.allclose(p[0], p[-1])) for p in polys)
    assert len(pts) == n_expected == len(nrm)
    assert np.allclose(np.linalg.norm(nrm, axis=1), 1.0, atol=1e-9)
    # intersection_offset_mm = 0（默认）→ 采样点就是交点本身
    v = np.vstack([p[:-1] if np.allclose(p[0], p[-1]) else p for p in polys])
    assert np.allclose(pts, v)
    # 臂上边缘（y=0）：外法向 +y
    sel = (np.abs(pts[:, 1]) < 1e-6) & (pts[:, 0] > 0.5) & (pts[:, 0] < 3.5)
    assert sel.sum() > 5
    assert np.allclose(nrm[sel][:, 1], 1.0, atol=0.2)


def test_boundary_samples_follow_the_offset_when_asked(ls, tmp_path):
    cfg = make_compact_cfg(tmp_path)
    cfg.sampling.intersection_offset_mm = 0.05
    polys = boundary_points(ls, cfg)
    pts0, _ = boundary_samples(polys, ls, cfg)
    cfg.sampling.intersection_offset_mm = 0.0
    pts1, nrm = boundary_samples(polys, ls, cfg)
    sel = (np.abs(pts1[:, 1]) < 1e-6) & (pts1[:, 0] > 0.5) & (pts1[:, 0] < 3.5)
    assert np.allclose(pts0[sel][:, 1] - pts1[sel][:, 1], 0.05, atol=1e-9)


# ---------------------------------------------------------------------- #
# intersection 方案：速度回写（最近节点 + 四邻居，反距离平方）
# ---------------------------------------------------------------------- #
def test_scatter_inverse_distance_matches_the_analytic_weights(ls):
    """交点在格线上 → 恰退化为该边两节点，权重 (1−t)²/(t²+(1−t)²)（进 counts）。"""
    t = 0.25
    i, j = 10, 13
    p = np.array([[ls.xs[i] + t * ls.dx, ls.ys[j]]])
    v, cnt = scatter_inverse_distance(ls, p, np.array([1.0]))
    w = (1 - t) ** 2 / (t ** 2 + (1 - t) ** 2)
    # 单点覆盖时值是"加权平均"，权重只体现在 counts（种子掩膜）上
    assert v[i, j] == pytest.approx(1.0) and v[i + 1, j] == pytest.approx(1.0)
    assert cnt[i, j] == pytest.approx(w, abs=1e-12)
    assert cnt[i + 1, j] == pytest.approx(1 - w, abs=1e-12)
    # 对角邻居距离 √(1+t²)·dx > 1 格、远侧邻居 1+t 格，都被距离规则排除
    assert cnt[i + 1, j + 1] == 0.0
    assert cnt[i - 1, j] == 0.0 and cnt[i, j + 1] == 0.0


def test_scatter_inverse_distance_lets_the_closer_point_win(ls):
    """一个节点被两个边界点覆盖：反距离平方 → 近者权重大（1/d² 不是等权平均）。"""
    i, j = 10, 13
    a = np.array([ls.xs[i] + 0.25 * ls.dx, ls.ys[j]])
    b = np.array([ls.xs[i] + 0.75 * ls.dx, ls.ys[j]])
    v, _ = scatter_inverse_distance(ls, np.vstack([a, b]), np.array([1.0, 0.0]))
    # 两点的权重分布互为镜像（0.9/0.1），节点值 = 加权平均
    assert v[i, j] == pytest.approx(0.9, abs=1e-12)
    assert v[i + 1, j] == pytest.approx(0.1, abs=1e-12)


def test_scatter_inverse_distance_gives_a_hit_node_everything(ls):
    """边界点正好落在节点上 → 直接赋值（不是插值、不摊给邻居）。"""
    i, j = 10, 13
    v, cnt = scatter_inverse_distance(ls, np.array([[ls.xs[i], ls.ys[j]]]),
                                      np.array([3.0]))
    assert v[i, j] == pytest.approx(3.0)
    assert cnt[i, j] == pytest.approx(1.0)
    assert np.count_nonzero(cnt) == 1


def test_scatter_inverse_distance_preserves_a_constant_field(ls):
    """常值速度回写后处处等于该常数（逐点归一化权重 → 值域不漂移）。"""
    pts = np.column_stack([np.linspace(0.5, 3.5, 30), np.full(30, 0.3)])
    v, cnt = scatter_inverse_distance(ls, pts, np.full(30, 2.5))
    touched = cnt > 0
    assert touched.sum() > 10
    assert np.allclose(v[touched], 2.5)
    assert np.all(v[~touched] == 0.0)


def test_scatter_inverse_distance_matches_the_shape_and_rejects_mismatch(ls):
    pts = np.array([[1.0, 0.0], [2.0, 0.0]])
    v, cnt = scatter_inverse_distance(ls, pts, np.array([1.0, 1.0]))
    assert v.shape == cnt.shape == ls.phi.shape
    with pytest.raises(ValueError, match="不匹配"):
        scatter_inverse_distance(ls, pts, np.array([1.0]))
    with pytest.raises(ValueError, match="neighbors"):
        scatter_inverse_distance(ls, pts, np.ones(2), neighbors="diagonal")


# ---------------------------------------------------------------------- #
# intersection 方案：WLS 的节点掩膜
# ---------------------------------------------------------------------- #
def test_field_node_mask_keeps_only_the_medium_side(ls, tmp_path):
    """掩膜 = φ_可动 > 0（介质侧）且 φ_固定 > 0（直通线内部不算空气）。

    直通线（fixed_region）在**可动** φ 里是正值（臂之外全是介质），光看它
    会把直通线内部 E≈0 的节点当成空气节点混进 WLS——必须用固定区 SDF 再排。
    """
    from eaopt.adjoint.fields import FieldGrid

    cfg = make_compact_cfg(tmp_path)
    field = FieldGrid(origin=(ls.xs[0], ls.ys[0], -0.5),
                      spacing=(ls.dx, ls.dx, 0.5), data=np.zeros((ls.nx, ls.ny, 3, 3)))
    j_air = int(round((0.2 - ls.ys[0]) / ls.dx))
    j_arm = int(round((-0.3 - ls.ys[0]) / ls.dx))
    j_line = int(round((0.7 - ls.ys[0]) / ls.dx))

    mask = field_node_mask(ls, field, cfg)
    assert mask.shape == (ls.nx, ls.ny)
    assert not mask[ls.phi < 0].any()          # 金属（臂）内不算
    assert not mask[:, j_line].any()           # 直通线内部也不算（固定区 SDF 排掉）
    assert mask[5, j_air]                      # 臂与直通线之间的介质算

    # 内侧（sample_side=inside）反过来：金属内算、介质不算
    cfg.sampling.sample_side = "inside"
    inside = field_node_mask(ls, field, cfg)
    assert inside[5, j_arm]
    assert not inside[5, j_air]
