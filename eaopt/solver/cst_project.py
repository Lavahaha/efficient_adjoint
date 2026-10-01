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
from pathlib import Path

__all__ = ["LOCK_SUFFIX", "companion_dir", "copy_project", "describe"]

LOCK_SUFFIX = ".lok"


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


def describe(path, needles=("substrate", "design_region", "Port 1",
                            "Rogers4350B")) -> list[str]:
    """人看的工程清单（供 smoke / scripts/cst_inspect_template.py 打印）。

    重点回答"模型到底在不在这个文件里"：新版 `.cst` 是 zip 容器，
    直接列出内部成员，并在成员里搜模型对象名（substrate 等）。找不到
    任何对象名 ⇒ 这个工程文件就是空的，跟读结果的 API 无关。
    """
    p = Path(path)
    out: list[str] = []
    if not p.exists():
        return [f"{p}: **不存在**"]
    st = p.stat()
    out.append(f"{p}: {st.st_size / 1e6:.3f} MB，最后修改 "
               f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
    sub = companion_dir(p)
    if sub.is_dir():
        n, total = _dir_size(sub)
        kids = sorted(x.name for x in sub.iterdir())[:12]
        out.append(f"  同名文件夹 {sub.name}/：{n} 个文件，{total / 1e6:.2f} MB，"
                   f"子项 {kids}")
    else:
        out.append(f"  **没有**同名文件夹 {sub.name}/（求解过的工程一般会有）")

    if not zipfile.is_zipfile(p):
        out.append("  .cst 不是 zip 容器（旧格式或已损坏）——里面有什么"
                   "只能靠 CST 打开看")
        return out
    with zipfile.ZipFile(p) as z:
        members = [(i.filename, i.file_size) for i in z.infolist()]
    out.append(f"  .cst 是 zip 容器，含 {len(members)} 个成员")
    for name, size in sorted(members, key=lambda m: -m[1])[:8]:
        out.append(f"    {size / 1e3:9.1f} KB  {name}")
    hits: dict[str, int] = {}
    for name, size in members:
        if size > 40e6:                       # 太大就不读了（网格文件之类）
            continue
        try:
            with zipfile.ZipFile(p) as z:
                blob = z.read(name)
        except Exception:
            continue
        for nd in needles:
            hits[nd] = hits.get(nd, 0) + blob.count(nd.encode("ascii", "ignore"))
    found = {k: v for k, v in hits.items() if v}
    if found:
        out.append(f"  在 .cst 内部搜到模型对象名：{found}")
        out.append("  ⇒ 模型数据在文件里，复制 .cst 不会丢模型")
    else:
        out.append(f"  在 .cst 内部**找不到**任何模型对象名（找过 {list(needles)}）")
        out.append("  ⇒ 这个 .cst 是**空工程**：宏保存出来的东西不完整，"
                   "跟读取 API 无关")
    return out
