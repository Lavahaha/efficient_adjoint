"""建模板脚本的 COM 编排逻辑（不连 CST：用假工程对象）。

真机行为靠服务器实测；这里锁住的是**编排**：每一块都要走
AddToHistory（既执行又记历史），单块失败不能中断其余块，
另存要把候选参数个数都试一遍。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from eaopt.solver import template_builder as T

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cst_build_template.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("_cst_build_template", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_cst_build_template"] = m
    spec.loader.exec_module(m)
    return m


class _FakeMWS:
    """最小假工程：记录 AddToHistory / SaveAs 调用。"""

    def __init__(self, refuse=(), save_fail=0):
        self.calls: list[tuple[str, str]] = []
        self.refuse = set(refuse)       # 返回 False 的标题
        self.raise_on = None            # (header) → 抛异常
        self.save_attempts: list[tuple] = []
        self.save_fail = save_fail      # 前 n 次 SaveAs 抛错

    def AddToHistory(self, header, contents):
        if self.raise_on == header:
            raise RuntimeError("boom")
        self.calls.append((header, contents))
        return header not in self.refuse

    def SaveAs(self, *args):
        self.save_attempts.append(args)
        if len(self.save_attempts) <= self.save_fail:
            raise RuntimeError("(10097) wrong number of parameters")


def test_build_sends_every_block_through_add_to_history(mod):
    mws = _FakeMWS()
    assert mod.build(mws, "coupler_fwd", 1) is True
    headers = [h for h, _ in mws.calls]
    expected = [h for h, _ in T.template_blocks("coupler_fwd", 1)]
    assert headers == expected                 # 顺序与内容都来自单一事实来源
    assert headers[0] == "Units"
    assert "Brick substrate" in headers
    assert "Extrude arm_init" in headers
    assert sum(h.startswith("Port") for h in headers) == 4
    # 激励端口写进了命令文本
    assert any('.StimulationPort "1"' in c for _, c in mws.calls)


def test_build_continues_after_a_failed_block(mod):
    mws = _FakeMWS(refuse={"Brick substrate"})
    assert mod.build(mws, "coupler_fwd", 1) is False   # 有失败 → 整批算失败
    assert "Brick substrate" in [h for h, _ in mws.calls]
    assert len(mws.calls) == len(T.template_blocks("coupler_fwd", 1))  # 没中断


def test_build_reports_exception_and_keeps_going(mod):
    mws = _FakeMWS()
    mws.raise_on = "Brick ground"
    assert mod.build(mws, "coupler_fwd", 1) is False
    assert "Brick ground" not in [h for h, _ in mws.calls]
    assert "Brick air" in [h for h, _ in mws.calls]     # 后面照跑


def test_build_bwd_uses_port_3(mod):
    mws = _FakeMWS()
    mod.build(mws, "coupler_bwd", 3)
    assert any('.StimulationPort "3"' in c for _, c in mws.calls)


def test_save_as_tries_parameter_variants(tmp_path, mod):
    mws = _FakeMWS(save_fail=2)                 # 前两种写法都失败
    out = tmp_path / "sub" / "tpl.cst"
    assert mod.save_as(mws, out) is True
    assert out.parent.is_dir()                  # 目录先建好
    assert len(mws.save_attempts) == 3
    assert mws.save_attempts[0][1] is True      # 先 True，再 "True"，再只给路径
    assert all(p[0] == str(out.resolve()) for p in mws.save_attempts)


def test_save_as_reports_total_failure(tmp_path, mod):
    mws = _FakeMWS(save_fail=9)
    assert mod.save_as(mws, tmp_path / "x.cst") is False


def test_template_blocks_headers_are_readable():
    """历史表标题从命令文本推出来（Brick substrate / Extrude leg_left …）。"""
    blocks = dict(T.template_blocks("coupler_fwd", 1))
    assert T.block_header(T.UNITS_BLOCK) == "Units"
    assert set(blocks) >= {"Units", "Brick substrate", "Extrude arm_init",
                           "Extrude leg_left", "Port p1"}
    assert blocks["Units"] == T.UNITS_BLOCK
    assert '    .Name "substrate"' in blocks["Brick substrate"]


def test_template_blocks_match_the_macro_content(tmp_path):
    """COM 路径与 GUI 宏路径必须建同一个模型（同一份命令文本）。

    宏里为了缩进/容错包装动过缩进，故按 strip 后的行比对；命令行本身
    必须一字不差——两条路径漂移的话，建出来的模板就不一样了。
    """
    text = T.build_macro(tmp_path, "coupler_fwd", 1).read_text("utf-8")
    in_macro = {ln.strip() for ln in text.splitlines()}
    for header, cmd in T.template_blocks("coupler_fwd", 1):
        for line in cmd.rstrip("\n").splitlines():
            assert line.strip() in in_macro, \
                f"{header} 的命令行不在宏里：{line.strip()!r}"
