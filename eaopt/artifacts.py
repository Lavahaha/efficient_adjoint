"""迭代产物落盘（``iter_NNN/``）与续跑读取。

布局（``cfg.output.dir`` 下）::

    iter_000/
      shape.json          可动轮廓（世界坐标 mm）——重建几何的唯一事实来源
      s_params_fwd.json   正向工程的 S 参数
      s_params_bwd.json   反向工程（伴随仿真）的 S 参数
      fields_fwd_e.npz    E 场（save_fields=true 时才写）
      fields_fwd_h.npz    H 场
      ls_phi.npz          φ 快照（水质集），续跑的**关键**文件
      meta.json           轮次、时间戳、耗时、各文件摘要
    history.jsonl        逐轮 FoM 记录（pipeline 写）

**先写数据文件、最后写 meta.json**：meta 是"这一轮齐了"的标记，
判断续跑起点时只看 meta。少了这个顺序，中断会留下一半数据却看着像成功。

所有 JSON 都是 UTF-8、``ensure_ascii=False``（中文备注能直接读），
写入走 tmp + ``os.replace`` 的原子替换——半截文件比没有文件更害人。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

__all__ = [
    "TAGS", "iteration_dir", "list_iterations", "latest_iteration",
    "save_json", "load_json", "s_params_to_json", "s_params_from_json",
    "save_shape", "load_shape", "save_solution", "load_s_params",
    "load_field", "save_ls_phi", "load_ls_phi", "truncate_history",
]

#: 两个 CST 工程的标签（fwd = 输入端口激励，bwd = 观测端口激励/伴随仿真）。
TAGS = ("fwd", "bwd")


def iteration_dir(outdir, n: int) -> Path:
    return Path(outdir) / f"iter_{int(n):03d}"


def list_iterations(outdir, tags=TAGS) -> list[int]:
    """**已完成**的轮次号。

    "完成"= 有 ``meta.json`` 且每个 tag 的 ``s_params_<tag>.json`` 都在。
    判据刻意落在**文件本身**而不是 meta 里的记账：中断最可能发生在
    "两个工程只跑完一个"的时刻（一个仿真几分钟，中途 Ctrl-C 很正常），
    只看 meta 会把这种半截轮次当成完成，续跑就从错的轮次接下去了。

    也正因为如此，``cst_init_fwd.py`` 单独跑完时 iter_000 还不算完成——
    两个 init 都跑完才算，之后 ``cst_update.py`` 才知道该写第 1 轮。
    """
    root = Path(outdir)
    if not root.is_dir():
        return []
    out = []
    for p in root.glob("iter_[0-9][0-9][0-9]"):
        if not (p / "meta.json").is_file():
            continue
        if not all((p / f"s_params_{t}.json").is_file() for t in tags):
            continue
        try:
            out.append(int(p.name.split("_", 1)[1]))
        except ValueError:                          # pragma: no cover - 命名防御
            continue
    return sorted(out)


def latest_iteration(outdir, tags=TAGS) -> int | None:
    iters = list_iterations(outdir, tags)
    return iters[-1] if iters else None


# --------------------------------------------------------------------------- #
# 原子读写
# --------------------------------------------------------------------------- #
def save_json(path, obj, *, indent: int = 2) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def s_params_to_json(sp: dict) -> list:
    """``{(响应端口, 激励端口): complex}`` → JSON 可存的列表。

    JSON 的键只能是字符串，直接 dump 会把元组键变成 ``"(1, 3)"`` 这种
    要再解析回来的东西；列表形式没有歧义，也方便人肉看。
    """
    return [[int(i), int(j), float(v.real), float(v.imag)]
            for (i, j), v in sorted(sp.items())]


def s_params_from_json(rows) -> dict:
    return {(int(i), int(j)): complex(float(re), float(im))
            for i, j, re, im in rows}


# --------------------------------------------------------------------------- #
# 形状
# --------------------------------------------------------------------------- #
def save_shape(outdir, n: int, polys, extra: dict | None = None) -> Path:
    """存可动轮廓（世界坐标 mm）。

    这是"几何长什么样"的唯一事实来源：CST 工程随时可以由它重建，
    所以它每轮都要写，且要能被人直接看懂。
    """
    polys = [np.asarray(p, dtype=float) for p in polys]
    payload = {
        "iteration": int(n),
        "unit": "mm",
        "n_polygons": len(polys),
        "polygons": [[[float(x), float(y)] for x, y in p] for p in polys],
    }
    if extra:
        payload.update(extra)
    return save_json(iteration_dir(outdir, n) / "shape.json", payload)


def load_shape(path_or_dir) -> list[np.ndarray]:
    """读形状：给 ``shape.json`` 本身或它所在的目录（``iter_NNN/``）都行。

    容忍两种写法：本模块写出的 ``{"polygons": [...]}`` 字典，以及裸列表
    （手工准备形状文件时最省事）——``[[[x,y],...], ...]`` 是多边形表，
    ``[[x,y], ...]`` 是单个多边形（手写一个矩形时很自然）。
    """
    path = Path(path_or_dir)
    if path.is_dir():
        path = path / "shape.json"
    if not path.is_file():
        raise FileNotFoundError(f"找不到形状文件：{path}")
    data = load_json(path)
    if isinstance(data, dict):
        polys = data.get("polygons")
        if polys is None:
            raise ValueError(f"{path} 里没有 polygons 字段（键：{list(data)}）")
    else:
        polys = data
    try:                                    # 裸列表：按第一个元素的维度认写法
        single = bool(polys) and np.ndim(polys[0]) == 1
    except (ValueError, TypeError, KeyError):   # 参差不齐/不是列表：交给校验
        single = False
    if single:
        polys = [polys]
    out = [np.asarray(p, dtype=float) for p in polys]
    for i, p in enumerate(out):
        if p.ndim != 2 or p.shape[1] != 2:
            raise ValueError(f"{path} 的 polygons[{i}] 形状必须是 (N,2)，"
                             f"收到 {p.shape}")
    return out


# --------------------------------------------------------------------------- #
# 解（S 参数 + 场）
# --------------------------------------------------------------------------- #
def save_solution(outdir, n: int, tag: str, sol, *, save_fields: bool = False,
                  extra: dict | None = None) -> dict:
    """把一次仿真的结果写进 ``iter_NNN/``，返回 ``{类别: 路径}``。

    ``save_fields=False`` 时只写 S 参数：场是给伴随法当场用的，落盘只是
    为了事后诊断，而 ``.npz`` 压缩后仍有 MB 量级（20 轮 = 上百 MB）。
    """
    if tag not in TAGS:
        raise ValueError(f"tag 只能是 {TAGS}，收到 {tag!r}")
    d = iteration_dir(outdir, n)
    d.mkdir(parents=True, exist_ok=True)
    out = {"s_params": save_json(d / f"s_params_{tag}.json",
                                 s_params_to_json(sol.s_params))}
    if save_fields:
        out["e_field"] = _save_field(d / f"fields_{tag}_e.npz", sol.e_field)
        out["h_field"] = _save_field(d / f"fields_{tag}_h.npz", sol.h_field)
    meta_path = d / "meta.json"
    meta = load_json(meta_path) if meta_path.is_file() else {"iteration": int(n)}
    meta.setdefault("tags", {})
    meta["tags"][tag] = {
        "pin_w": float(sol.pin),
        "s_params": {f"S{i},{j}": [float(v.real), float(v.imag)]
                     for (i, j), v in sorted(sol.s_params.items())},
        "files": {k: str(v) for k, v in out.items()},
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        meta["tags"][tag].update(extra)
    meta["tags_present"] = sorted(meta["tags"])
    save_json(meta_path, meta)
    out["meta"] = meta_path
    return out


def load_s_params(outdir, n: int, tag: str) -> dict:
    """读回 ``{(响应端口, 激励端口): complex}``。"""
    return s_params_from_json(load_json(
        iteration_dir(outdir, n) / f"s_params_{tag}.json"))


def _save_field(path: Path, grid) -> Path:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        np.savez_compressed(f, origin=np.asarray(grid.origin, dtype=float),
                            spacing=np.asarray(grid.spacing, dtype=float),
                            data=np.asarray(grid.data))
    os.replace(tmp, path)
    return path


def load_field(path):
    """读回 FieldGrid（``fields_<tag>_<e|h>.npz``）。"""
    from eaopt.adjoint.fields import FieldGrid

    with np.load(path) as z:
        return FieldGrid(origin=tuple(z["origin"]), spacing=tuple(z["spacing"]),
                         data=z["data"])


# --------------------------------------------------------------------------- #
# 水准集 φ 快照（续跑用）
# --------------------------------------------------------------------------- #
def save_ls_phi(outdir, n: int, ls) -> Path:
    """存 φ 与两个坐标轴。

    φ 才是续跑的**唯一**事实来源：CST 工程随时可以按 ``shape.json`` 重建，
    而水准集演化是路径相关的（HJ 方程按轮次累积），丢了就接不上。
    这个文件很小（12×5.5 mm / 0.05 → 241×111 双精度 ≈ 214 KB，压缩后更小）。
    """
    path = iteration_dir(outdir, n) / "ls_phi.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        np.savez_compressed(f, phi=np.asarray(ls.phi, dtype=float),
                            xs=np.asarray(ls.xs, dtype=float),
                            ys=np.asarray(ls.ys, dtype=float),
                            dx=float(ls.dx))
    os.replace(tmp, path)
    return path


def load_ls_phi(path) -> dict:
    """读回 ``{"phi": (nx,ny), "xs": (nx,), "ys": (ny,), "dx": float}``。"""
    with np.load(path) as z:
        return {k: z[k] for k in ("phi", "xs", "ys", "dx")}


# --------------------------------------------------------------------------- #
# 续跑：history.jsonl 截断
# --------------------------------------------------------------------------- #
def truncate_history(history_jsonl, keep_below: int) -> int:
    """删掉 ``iteration >= keep_below`` 的记录，返回删掉的条数。

    续跑时**必须**做这件事：中断可能发生在"记录已写、下一轮 state 还没写"
    之间，不截断就会从更早的 φ 接着跑、却把重复轮次的记录又追加一遍，
    历史里出现两个 iteration=7。文件先写 tmp 再原子替换。
    """
    path = Path(history_jsonl)
    if not path.is_file():
        return 0
    kept, dropped = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                it = int(rec.get("iteration"))
            except (ValueError, TypeError):        # 损坏行：保守保留
                kept.append(line)
                continue
            if it >= keep_below:
                dropped += 1
            else:
                kept.append(line)
    if dropped:
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
        os.replace(tmp, path)
    return dropped
