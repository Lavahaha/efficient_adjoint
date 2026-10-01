"""命令行脚本的公共小事（各脚本共用的收尾工作）。

目前只有一件：让打印在 GBK 控制台上不炸。
"""

from __future__ import annotations

import sys

__all__ = ["safe_console"]


def safe_console() -> None:
    """把 stdout/stderr 的编码错误策略改成 ``replace``。

    服务器控制台常是 GBK（cp936）：中文本身没问题，但个别符号（如
    U+21D2 arrow、"≠" 之类）不在 GBK 里，一 print 就 UnicodeEncodeError
    ——**整个脚本带着前面所有有用的输出一起崩掉**（实测踩过）。改成
    ``errors="replace"`` 后，编不出的字符打印成 ``?``，其余照常。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:          # 非 TextIOWrapper（如被重定向到管道）
            pass
