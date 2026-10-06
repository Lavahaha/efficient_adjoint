"""速度窄带延拓：scikit-fmm 优先，缺库回退 PDE 上风格式。

skfmm 的真实数值路径只在装了它的机器上跑（服务器 conda py310 装得上；
本机 Python 3.14 没有轮子 → `importorskip` 跳过），**调用形态**（dx/order、
返回值的两种形态）用假模块锁住——那部分是版本兼容的坑（2022.2.2 起
`extension_velocities` 才返回 `(d, f_ext)` 二元组）。
"""

import sys
import types

import numpy as np
import pytest

from eaopt.config import BoxSpec
from eaopt.geometry import extension as ext
from eaopt.geometry.extension import extend_velocity, skfmm_available
from eaopt.geometry.levelset import LevelSet2D


@pytest.fixture
def ls():
    """[-1,1]² 上 φ = x：界面是 x=0 的直线，特征线沿 x 向外。"""
    l = LevelSet2D(BoxSpec(x=(-1.0, 1.0), y=(-1.0, 1.0)), 0.05)
    X, _ = np.meshgrid(l.xs, l.ys, indexing="ij")
    l.phi = X.copy()
    return l


@pytest.fixture
def seeded(ls):
    """边界已知值 = 界面两侧各半格内的节点，值恒为 1。"""
    known = np.abs(ls.phi) <= 0.5 * ls.dx
    v = np.zeros_like(ls.phi)
    v[known] = 1.0
    return v, known


def _install_fake_skfmm(monkeypatch, *, as_tuple=True, calls=None):
    """把假 skfmm 塞进 sys.modules（extension.py 是懒 import，装得上就生效）。"""
    fake = types.ModuleType("skfmm")

    def extension_velocities(phi, speed, dx=1.0, order=2, **kw):
        if calls is not None:
            calls.append({"dx": dx, "order": order, "phi": np.array(phi),
                          "speed": np.array(speed), **kw})
        ones = np.ones_like(np.asarray(phi, dtype=float))
        return (np.abs(phi), ones) if as_tuple else ones

    fake.extension_velocities = extension_velocities
    monkeypatch.setitem(sys.modules, "skfmm", fake)
    return fake


# ---------------------------------------------------------------------- #
# PDE 回退（本机默认走的就是这条）
# ---------------------------------------------------------------------- #
def test_pde_extension_keeps_the_band_value_and_zeroes_the_rest(ls, seeded):
    v, known = seeded
    V, method = extend_velocity(ls, v, 0.3, method="pde", known=known)
    assert method == "pde"
    band = np.abs(ls.phi) <= 0.3
    assert np.all(V[~band] == 0.0)                 # 带外严格为 0
    assert V[band].min() > 0.9                     # 常值速度延拓后仍是常值（≈1）
    assert V[band].max() < 1.1


def test_pde_extension_seeds_from_the_known_mask(ls):
    """known 掩膜决定种子：φ 被人为偏离 SDF 时，旧的 |φ|≤0.75dx 规则会漏种子。"""
    known = np.abs(ls.phi) <= 0.5 * ls.dx
    v = np.zeros_like(ls.phi)
    v[known] = 1.0
    # φ 拉伸 10 倍并平移半格：|∇φ| = 10，界面落在两节点之间 → 0.75dx 带内**无节点**
    ls.phi = 10.0 * (ls.phi + 0.25 * ls.dx)
    assert not (np.abs(ls.phi) <= 0.75 * ls.dx).any()
    V_known, _ = extend_velocity(ls, v, 0.3, method="pde", known=known)
    V_old, _ = extend_velocity(ls, v, 0.3, method="pde")
    assert np.any(V_known != 0.0)                   # 传了 known：照常延拓
    assert not np.any(V_old != 0.0)                 # 不传：种子全丢，静默得到零场


def test_zero_velocity_or_no_interface_needs_no_extension(ls, seeded):
    v, known = seeded
    V, method = extend_velocity(ls, np.zeros_like(v), 0.3, known=known)
    assert method == "none" and not np.any(V)
    ls.phi = np.abs(ls.phi) + 1.0                   # 没有界面（φ 不换号）
    V, method = extend_velocity(ls, v, 0.3, known=known)
    assert method == "none" and not np.any(V)


# ---------------------------------------------------------------------- #
# skfmm 路径：调用形态用假模块锁，数值用真库（没装则跳过）
# ---------------------------------------------------------------------- #
def test_auto_prefers_skfmm_and_unpacks_the_tuple(ls, seeded, monkeypatch):
    v, known = seeded
    calls = []
    _install_fake_skfmm(monkeypatch, as_tuple=True, calls=calls)
    V, method = extend_velocity(ls, v, 0.3, method="auto", known=known)
    assert method == "skfmm"
    assert calls[0]["dx"] == ls.dx and calls[0]["order"] == 2   # 传的是网格步长
    band = np.abs(ls.phi) <= 0.3
    assert np.all(V[band] == 1.0) and not np.any(V[~band])


def test_skfmm_path_accepts_old_versions_returning_a_bare_array(ls, seeded,
                                                               monkeypatch):
    """2022.2.2 之前 extension_velocities 只返回 f_ext（没有 (d, f_ext)）。"""
    v, known = seeded
    _install_fake_skfmm(monkeypatch, as_tuple=False)
    V, method = extend_velocity(ls, v, 0.3, method="skfmm", known=known)
    assert method == "skfmm" and np.all(V[np.abs(ls.phi) <= 0.3] == 1.0)


def test_auto_falls_back_to_pde_and_warns_once(ls, seeded, monkeypatch, capsys):
    v, known = seeded
    monkeypatch.setitem(sys.modules, "skfmm", None)   # import skfmm → ImportError
    monkeypatch.setitem(ext._WARNED, "skfmm", False)
    V, method = extend_velocity(ls, v, 0.3, method="auto", known=known)
    assert method == "pde"
    out = capsys.readouterr().out
    assert "scikit-fmm" in out and "回退" in out      # 绝不静默降级
    _, method2 = extend_velocity(ls, v, 0.3, method="auto", known=known)
    assert method2 == "pde" and capsys.readouterr().out == ""   # 告警只打一次


def test_skfmm_method_raises_without_the_library(ls, seeded, monkeypatch):
    v, known = seeded
    monkeypatch.setitem(sys.modules, "skfmm", None)
    with pytest.raises(ImportError):
        extend_velocity(ls, v, 0.3, method="skfmm", known=known)


def test_unknown_method_is_rejected(ls, seeded):
    v, known = seeded
    with pytest.raises(ValueError, match="extension_method"):
        extend_velocity(ls, v, 0.3, method="ffm", known=known)
    with pytest.raises(ValueError, match="形状"):
        extend_velocity(ls, np.zeros((2, 2)), 0.3)


def test_skfmm_available_matches_the_import(ls):
    try:
        import skfmm  # noqa: F401
        assert skfmm_available()
    except ImportError:
        assert not skfmm_available()


def test_skfmm_extends_a_constant_velocity_across_the_band(ls, seeded):
    """真库数值：φ = x 的直线界面 + 常值速度 → 窄带内处处相等、带外为 0。"""
    pytest.importorskip("skfmm")
    v, known = seeded
    V, method = extend_velocity(ls, v, 0.3, method="skfmm", known=known)
    assert method == "skfmm"
    band = np.abs(ls.phi) <= 0.3
    assert np.allclose(V[band], 1.0, atol=1e-9)
    assert np.all(V[~band] == 0.0)
