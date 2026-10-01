"""CST 工程在**文件层面**的操作（纯文件，不依赖 COM）。

CST 的一个工程在磁盘上可能是**两件东西**：

    <目录>/<名字>.cst     工程文件（常常只有几十 KB）
    <目录>/<名字>/        同名文件夹（外部结果目录，求解后出现）

两者是配套的：只搬 `.cst` 而丢掉同名文件夹，打开后可能缺结果（某些版本
连模型都是空的），而 CST 不会报任何错——导航树里光秃秃的，端口、结果
全都读不到。所以复制工程必须整份搬，并跳过 ``*.lok``（CST 的锁文件，
工程打开着就会出现；复制它对目标工程只有坏处）。

本模块只做文件搬运与查看；COM/结果读取的东西在 cst_api.py。
"""

from __future__ import annotations

import shutil
import time
import zipfile
import zlib
from pathlib import Path

__all__ = ["LOCK_SUFFIX", "MODEL_NEEDLES", "companion_dir", "copy_project",
           "describe", "grep_ascii"]

LOCK_SUFFIX = ".lok"

# 找"模型在不在"时搜的对象名。取模板宏建出来的实体名（见
# template_builder）：搜得到 => 这个工程里确实有几何/端口。
MODEL_NEEDLES = ("substrate", "design_region", "thru_line", "leg_left",
                 "leg_right", "arm_init", "ground", "Rogers4350B", "Port 1")

# 单个文件最多读多少字节（超过就跳过：结果文件可能很大）
_MAX_FILE_BYTES = 40 * 10 ** 6


def companion_dir(cst_path: Path) -> Path:
    """工程的同名文件夹路径（去掉 `.cst` 后缀；不保证存在）。"""
    return Path(cst_path).with_suffix("")


def copy_project(src_cst, dst_dir, dst_name: str | None = None,
                 overwrite: bool = True) -> Path:
    """把工程整份复制到 dst_dir，返回目标 .cst 路径。

    复制内容：`.cst` 文件 + 同名文件夹（若存在，递归复制，排除 `*.lok`）；
    dst_name 给出时连文件夹名一起改（如 `fwd_coupler_fwd.cst`）。
    """
    src = Path(src_cst)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (dst_name or src.name)
    if dst.exists() and not overwrite:
        raise FileExistsError(f"目标已存在: {dst}")
    shutil.copy(src, dst)
    src_sub = companion_dir(src)
    if src_sub.is_dir():
        dst_sub = companion_dir(dst)
        if dst_sub.exists():
            shutil.rmtree(dst_sub)
        shutil.copytree(src_sub, dst_sub,
                        ignore=shutil.ignore_patterns("*" + LOCK_SUFFIX))
    return dst


def _dir_size(path: Path) -> tuple[int, int]:
    files = [f for f in path.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


# zip 本地文件头（中央目录损坏时靠它逐个恢复成员）
_PK_LOCAL = b"PK\x03\x04"


def _read_members(path: Path, limit: int = 60) -> tuple[list[tuple[str, bytes]], list[str]]:
    """尽量读出 `.cst`（zip 容器）里的成员 → ([(名字, 内容)], 诊断行)。

    三种情况都容忍（诊断脚本不能自己崩掉）：
      1. 标准 zip（有中央目录）→ zipfile 直接读；
      2. 中央目录损坏/缺失（截断、边写边复制…）→ 扫本地文件头逐个恢复，
         能解压的解压，解不了的只报名字；
      3. 根本不是 zip → 空列表 + 说明。
    """
    data = path.read_bytes()
    notes: list[str] = []
    members: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist()[:limit]:
                if info.file_size > 40e6:
                    continue
                try:
                    members.append((info.filename, z.read(info)))
                except Exception as e:
                    notes.append(f"    成员 {info.filename} 读失败：{e}")
        notes.insert(0, f"  .cst 是标准 zip 容器，{len(members)} 个成员")
        return members, notes
    except Exception as e:
        notes.append(f"  zipfile 读不了（{type(e).__name__}: {e}）")

    # 中央目录坏了：扫本地文件头做恢复
    i, recovered = 0, 0
    while True:
        i = data.find(_PK_LOCAL, i)
        if i < 0:
            break
        try:
            method = int.from_bytes(data[i + 8:i + 10], "little")
            csize = int.from_bytes(data[i + 18:i + 22], "little")
            nlen = int.from_bytes(data[i + 26:i + 28], "little")
            elen = int.from_bytes(data[i + 28:i + 30], "little")
            name = data[i + 30:i + 30 + nlen].decode("utf-8", "replace")
            start = i + 30 + nlen + elen
            raw = data[start:start + csize] if csize else b""
            if method == 0:                     # 未压缩
                members.append((name, raw))
                recovered += 1
            elif method == 8 and raw:           # deflate
                try:
                    members.append((name, zlib.decompress(raw, -15)))
                    recovered += 1
                except Exception:
                    members.append((name, b""))
                    notes.append(f"    成员 {name} 解压失败（文件可能被截断）")
            else:
                members.append((name, b""))
            i = start + csize if csize else start + 1
        except Exception:
            i += 4
    if members:
        notes.append(f"  按本地文件头恢复出 {len(members)} 个成员"
                     f"（其中 {recovered} 个内容完整）")
        notes.append("  !! 这个 .cst 的 zip 中央目录坏了（文件被截断、或是在"
                     "CST 还开着的时候复制出来的）——内容已按本地文件头恢复")
    else:
        notes.append("  连本地文件头 PK\\x03\\x04 都没有：这个 .cst "
                     "**不是 zip 格式**（老式/私有容器），内容只能整体搜")
    return members, notes


def _count_needles(blob: bytes, needles) -> dict[str, int]:
    hits: dict[str, int] = {}
    for nd in needles:
        b = nd.encode("ascii", "ignore")
        n = blob.count(b) + blob.count(b.decode("ascii").encode("utf-16-le"))
        if n:
            hits[nd] = n
    return hits


def _scan_tree(root: Path, needles) -> tuple[list[str], dict[str, int]]:
    """扫同名文件夹：每个子目录一行，命中对象名的文件单独列出。

    服务器实测教训：几何确实会在**同名文件夹**里（只复制 `.cst` 得到的
    工程是空的，把文件夹一起搬过来几何就出现了），所以判定"工程空不空"
    不能只看 `.cst`。
    """
    lines: list[str] = []
    total: dict[str, int] = {}

    def scan(files: list[Path], label: str) -> None:
        size = sum(f.stat().st_size for f in files)
        hit_rows: list[tuple[str, dict[str, int]]] = []
        for f in files:
            try:
                if f.stat().st_size > _MAX_FILE_BYTES:
                    continue
                h = _count_needles(f.read_bytes(), needles)
            except OSError:
                continue
            if h:
                hit_rows.append((f.relative_to(root).as_posix(), h))
                for k, v in h.items():
                    total[k] = total.get(k, 0) + v
        tag = "   <<< 命中对象名" if hit_rows else ""
        lines.append(f"    {label}：{len(files)} 个文件，"
                     f"{size / 1e6:.2f} MB{tag}")
        for rel, h in hit_rows[:6]:
            lines.append(f"       命中 {rel}  {h}")

    subs = sorted(x for x in root.iterdir() if x.is_dir())
    for d in subs:
        scan(sorted(f for f in d.rglob("*") if f.is_file()), f"子目录 {d.name}/")
    tops = sorted(f for f in root.iterdir() if f.is_file())
    if tops:
        scan(tops, "（顶层文件）")
    return lines, total


def grep_ascii(path, keys, limit: int = 2) -> list[str]:
    """在文件（或目录下所有文件）里搜 ASCII/UTF-16 关键字，返回片段行。

    用途：看宏到底把哪些命令写进了工程（`With Port` / `StimulationPort`
    / `Brick` …）——CST 把历史/设置以文本内嵌在工程数据里，搜到就说明
    那条命令真的执行过。搜不到不代表没执行（可能整体压缩），所以调用方
    要把"搜不到"当"无效证据"，不能当反证。
    """
    p = Path(path)
    files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
    out: list[str] = []
    for f in files:
        try:
            if f.stat().st_size > _MAX_FILE_BYTES:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        for key in keys:
            for enc in ("ascii", "utf-16-le"):
                kb = key.encode(enc)
                start, n = 0, 0
                while n < limit:
                    i = raw.find(kb, start)
                    if i < 0:
                        break
                    seg = raw[max(0, i - 24):i + 72]
                    step = 1 if enc == "ascii" else 2
                    txt = "".join(chr(c) if 32 <= c < 127 else "."
                                  for c in seg[::step])
                    out.append(f"[{key}] {f.name}: {txt}")
                    start = i + len(kb)
                    n += 1
    return out


def describe(path, needles=MODEL_NEEDLES) -> list[str]:
    """人看的工程清单（供 smoke / scripts/cst_inspect_template.py 打印）。

    回答两个问题：
      1. `.cst` 里有没有模型（新版 `.cst` 是 zip 容器，列出成员并搜
         对象名；容器读不动时退回搜原始字节）；
      2. **同名文件夹**里有没有模型——服务器实测：新建工程只复制 `.cst`
         打开是空的，连文件夹一起搬几何就出来了，所以模型很可能在这儿。

    两处都搜不到对象名才判定"空工程"。
    """
    p = Path(path)
    out: list[str] = []
    if not p.exists():
        return [f"{p}: **不存在**"]
    st = p.stat()
    out.append(f"{p}: {st.st_size / 1e6:.3f} MB，最后修改 "
               f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
    raw = p.read_bytes()
    magic = " ".join(f"{b:02X}" for b in raw[:8])
    ascii_magic = "".join(chr(b) if 32 <= b < 127 else "." for b in raw[:8])
    out.append(f"  文件头 8 字节：{magic}  ('{ascii_magic}')"
               + ("   —— 标准 zip（本地文件头）" if raw[:4] == _PK_LOCAL
                  else "   —— **不是** zip 本地文件头"))

    sub = companion_dir(p)
    folder_hits: dict[str, int] = {}
    if sub.is_dir():
        n, total = _dir_size(sub)
        kids = sorted(x.name for x in sub.iterdir())[:12]
        out.append(f"  同名文件夹 {sub.name}/：{n} 个文件，{total / 1e6:.2f} MB，"
                   f"子项 {kids}")
        rows, folder_hits = _scan_tree(sub, needles)
        out.extend(rows)
    else:
        out.append(f"  **没有**同名文件夹 {sub.name}/（求解过的工程一般会有）")

    members, notes = _read_members(p)
    out.extend(notes)
    for name, blob in sorted(members, key=lambda m: -len(m[1]))[:8]:
        out.append(f"    {len(blob) / 1e3:9.1f} KB  {name}")

    hits: dict[str, int] = {}
    for _, blob in members:
        for k, v in _count_needles(blob, needles).items():
            hits[k] = hits.get(k, 0) + v
    raw_hits = _count_needles(raw, needles)

    # ---- 判定：模型到底在哪 ----
    if hits:
        out.append(f"  .cst 解压后搜到对象名：{hits}")
    elif raw_hits:
        out.append(f"  .cst 原始字节里搜到对象名：{raw_hits}（容器读不完整，"
                   f"内容还在）")
    else:
        out.append(f"  .cst 里读不出任何成员，也搜不到对象名（找过 {list(needles)}）")
    if folder_hits:
        out.append(f"  同名文件夹里搜到对象名：{folder_hits}")

    if hits or raw_hits:
        out.append("  => 模型数据在 .cst 里" + ("（容器解析不完整）"
                                                if not hits else ""))
    elif folder_hits:
        out.append("  => **模型在同名文件夹里**（.cst 里没有）：复制工程必须"
                   "连文件夹一起搬，否则 CST 打开是个空工程"
                   "（用 cst_project.copy_project，别用 shutil.copy）")
    else:
        out.append("  => .cst 与同名文件夹里都搜不到对象名：这个工程是"
                   "**空的/损坏的**，与读取 API 无关，需重建模板")
    return out
