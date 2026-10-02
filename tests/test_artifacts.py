"""迭代产物落盘（artifacts）——不装 CST 也能全绿。

重点是**续跑的正确性**（错了不报错、只出错数据，最贵的一类）：
  * ``meta.json`` 是"这一轮齐了"的标记，缺它的目录不算完成；
  * ``truncate_history`` 必须把"记录写了、state 没写"的中断点截掉，
    否则续跑会出现重复轮次；
  * S 参数 / φ / 场 的往返必须逐值相等（含复数与网格坐标）。
"""

import json
from pathlib import Path

import numpy as np
import pytest

from eaopt import artifacts as A
from eaopt.adjoint.fields import FieldGrid
from eaopt.solver.base import Solution


def _solution(scale=1.0):
    grid = FieldGrid(origin=(0.0, -1.0, 0.0), spacing=(0.5, 0.5, 0.25),
                     data=(np.arange(36, dtype=float).reshape(2, 2, 3, 3) * scale
                           * (1 + 1j)))
    return Solution(s_params={(1, 3): 0.118 + 0.021j, (3, 3): 0.9 - 0.01j},
                    e_field=grid, h_field=grid, pin=0.5,
                    extra={"tag": "fwd", "solver_seconds": 12.5})


# --------------------------------------------------------------------------- #
# S 参数
# --------------------------------------------------------------------------- #
def test_s_params_json_roundtrip_keeps_tuple_keys_and_complex():
    """JSON 没有元组键，也没有复数——转回来必须一模一样（键序也要稳）。"""
    sp = {(2, 1): 0.5 - 0.25j, (1, 3): 0.118 + 0.0211j}
    rows = A.s_params_to_json(sp)
    assert json.loads(json.dumps(rows)) == rows        # 真的能进 JSON
    assert A.s_params_from_json(rows) == sp
    assert A.s_params_to_json(sp) == A.s_params_to_json(dict(reversed(list(sp.items()))))


# --------------------------------------------------------------------------- #
# iter_NNN/ 的完成语义
# --------------------------------------------------------------------------- #
def test_a_half_finished_iteration_does_not_count_as_complete(tmp_path):
    """两个工程只跑完一个 = 半截轮次，续跑不能从这里接下去。

    这正是一次中断最可能的样子：一个仿真几分钟，跑完 fwd、bwd 还没跑完时
    Ctrl-C。若把它当成"第 3 轮已完成"，`--resume` 会从第 4 轮接着写，
    而第 3 轮的 bwd 数据永远缺着却没人知道。
    """
    A.save_solution(tmp_path, 3, "fwd", _solution())

    assert A.list_iterations(tmp_path) == []
    assert A.latest_iteration(tmp_path) is None

    A.save_solution(tmp_path, 3, "bwd", _solution())
    assert A.list_iterations(tmp_path) == [3]
    assert A.latest_iteration(tmp_path) == 3

    # 只有目录和 fwd：仍然不算（而且 meta 也没有）
    (tmp_path / "iter_007").mkdir()
    (tmp_path / "iter_007" / "s_params_fwd.json").write_text("[]", "utf-8")
    assert A.list_iterations(tmp_path) == [3]
    # meta 在、数据缺一半：也不算
    (tmp_path / "iter_007" / "meta.json").write_text("{}", "utf-8")
    assert A.list_iterations(tmp_path) == [3]
    # 只关心一个 tag 时才认它
    assert A.list_iterations(tmp_path, tags=("fwd",)) == [3, 7]


def test_save_solution_writes_s_params_and_merges_tags(tmp_path):
    """两个工程各自写自己那份，meta 累积两个 tag（谁后写都要保留先写的）。"""
    p1 = A.save_solution(tmp_path, 0, "fwd", _solution())
    p2 = A.save_solution(tmp_path, 0, "bwd", _solution(scale=2.0))

    assert p1["s_params"].name == "s_params_fwd.json"
    assert p2["s_params"].name == "s_params_bwd.json"
    assert A.load_s_params(tmp_path, 0, "fwd") == {(1, 3): 0.118 + 0.021j,
                                                   (3, 3): 0.9 - 0.01j}

    meta = json.loads((tmp_path / "iter_000" / "meta.json").read_text("utf-8"))
    assert meta["tags_present"] == ["bwd", "fwd"]
    assert meta["iteration"] == 0
    assert meta["tags"]["fwd"]["pin_w"] == 0.5
    assert meta["tags"]["bwd"]["s_params"]["S1,3"] == [0.118, 0.021]


def test_save_fields_is_a_real_switch(tmp_path):
    """save_fields=False 只写 S 参数：场不落盘（20 轮能省上百 MB）。"""
    A.save_solution(tmp_path, 1, "fwd", _solution(), save_fields=False)
    assert not list((tmp_path / "iter_001").glob("*.npz"))

    paths = A.save_solution(tmp_path, 2, "fwd", _solution(), save_fields=True)
    assert paths["e_field"].name == "fields_fwd_e.npz"
    back = A.load_field(paths["e_field"])
    assert back.data.shape == (2, 2, 3, 3)
    assert np.allclose(back.data, _solution().e_field.data)
    assert back.origin == pytest.approx((0.0, -1.0, 0.0))
    assert back.spacing == pytest.approx((0.5, 0.5, 0.25))


def test_save_solution_rejects_unknown_tag(tmp_path):
    with pytest.raises(ValueError, match="tag"):
        A.save_solution(tmp_path, 0, "sideways", _solution())


# --------------------------------------------------------------------------- #
# 形状
# --------------------------------------------------------------------------- #
def test_shape_roundtrip_from_file_or_directory(tmp_path):
    polys = [np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]),
             np.array([[2.0, 2.0], [3.0, 2.0], [3.0, 3.0]])]
    path = A.save_shape(tmp_path, 4, polys, extra={"note": "手工给的"})

    payload = json.loads(path.read_text("utf-8"))
    assert payload["n_polygons"] == 2 and payload["unit"] == "mm"
    assert payload["note"] == "手工给的"

    for where in (path, path.parent):                   # 给文件、给目录都行
        back = A.load_shape(where)
        assert len(back) == 2
        assert np.allclose(back[0], polys[0])


def test_load_shape_accepts_a_bare_polygon_list(tmp_path):
    """手工准备形状文件时最省事的写法：裸的 [[[x,y],...], ...]。"""
    p = tmp_path / "hand.json"
    p.write_text(json.dumps([[[0, 0], [1, 0], [1, 1]]]), encoding="utf-8")
    assert np.allclose(A.load_shape(p)[0], [[0, 0], [1, 0], [1, 1]])


def test_load_shape_rejects_a_bad_shape(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([[[0, 0, 0], [1, 1, 1]]]), encoding="utf-8")  # 3 列
    with pytest.raises(ValueError, match=r"\(N,2\)"):
        A.load_shape(p)
    with pytest.raises(FileNotFoundError):
        A.load_shape(tmp_path / "nope.json")


# --------------------------------------------------------------------------- #
# φ 快照与续跑
# --------------------------------------------------------------------------- #
def test_ls_phi_roundtrip(tmp_path):
    class LS:
        phi = np.linspace(-1.0, 1.0, 12).reshape(4, 3)
        xs = np.linspace(0.0, 1.5, 4)
        ys = np.linspace(-1.0, 0.0, 3)
        dx = 0.5

    path = A.save_ls_phi(tmp_path, 6, LS())
    back = A.load_ls_phi(path)
    assert np.allclose(back["phi"], LS.phi)
    assert np.allclose(back["xs"], LS.xs)
    assert np.allclose(back["ys"], LS.ys)
    assert back["dx"] == 0.5


def test_truncate_history_drops_the_interrupted_tail(tmp_path):
    """中断点：记录写了、下一轮 φ 还没写。续跑前必须截到这个点之前。

    不截的话，从更早的 φ 接着跑会把 iteration=7 的记录又追加一遍，
    历史里出现两个 7，收敛判断和画图都会错。
    """
    log = tmp_path / "history.jsonl"
    recs = [{"iteration": i, "fom": 0.1 * i} for i in range(10)]
    log.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")

    dropped = A.truncate_history(log, keep_below=7)

    assert dropped == 3
    left = [json.loads(ln)["iteration"]
            for ln in log.read_text("utf-8").splitlines() if ln.strip()]
    assert left == list(range(7))


def test_truncate_history_is_a_noop_when_nothing_to_drop(tmp_path):
    log = tmp_path / "history.jsonl"
    log.write_text('{"iteration": 0}\n', encoding="utf-8")
    assert A.truncate_history(log, keep_below=5) == 0
    assert log.read_text("utf-8").strip() == '{"iteration": 0}'
    assert A.truncate_history(tmp_path / "missing.jsonl", keep_below=1) == 0


def test_truncate_history_keeps_unparsable_lines(tmp_path):
    """损坏行不动它——宁可留下看不懂的一行，也不要静默删掉记录。"""
    log = tmp_path / "history.jsonl"
    log.write_text('{"iteration": 0}\n<oops>\n{"iteration": 9}\n', "utf-8")
    assert A.truncate_history(log, keep_below=5) == 1
    assert "<oops>" in log.read_text("utf-8")


# --------------------------------------------------------------------------- #
# 原子写
# --------------------------------------------------------------------------- #
def test_writes_are_atomic_no_tmp_files_left_behind(tmp_path):
    """写盘一律 tmp + os.replace：不能留下 .tmp（会被下一次读当成数据）。"""
    A.save_json(tmp_path / "a.json", {"k": 1})
    A.save_solution(tmp_path, 0, "fwd", _solution(), save_fields=True)
    A.save_ls_phi(tmp_path, 0, type("LS", (), {
        "phi": np.zeros((2, 2)), "xs": np.zeros(2), "ys": np.zeros(2), "dx": 1.0})())

    leftovers = [p.name for p in Path(tmp_path).rglob("*.tmp")]
    assert leftovers == []
