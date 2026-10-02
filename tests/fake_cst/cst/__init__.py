"""**假的 CST 官方库**（测试专用）——形状照 CST 2024 的 `cst.interface` /
`cst.results`，行为可编程。

真库只支持 Python 3.6–3.11、且只装在装了 CST 的机器上；本机（无 CST）跑
测试时靠 ``tests/conftest.py`` 把本目录插到 ``sys.path[0]``，于是生产代码里
顶层的 ``import cst.interface`` / ``import cst.results`` 拿到的就是这里的东西。

**它不是 mock 求解器**：几何/物理一概不算，只负责"照官方 API 的形状接住
调用、把调用记录下来、按测试设定的方式回应"。被锁的是**我们的编排逻辑**
（存盘先于读结果、每轮一条历史记录、读的是哪一条结果、场有没有裁……），
这些错了不会报错、只会产出看着正常的错数据。

用法（测试里）::

    import cst.interface as csti
    csti.configure(fail_connect=True)      # 设定本次会话的行为
    csti.state().events                    # 断言调用时序

每个测试开始前由 conftest 的 autouse fixture 自动 ``reset()``。
"""

__version__ = "fake-2024"

__all__ = ["interface", "results"]
