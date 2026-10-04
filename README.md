# eaopt — 基于伴随法的微波器件形状优化（论文复现）

复现论文：

> J. Ji, S. HuYan, L. Du, X. Xu, J. Zhao, "Efficient Adjoint-Based Shape
> Optimization Method for the Inverse Design of Microwave Components,"
> *IEEE Trans. Microwave Theory and Techniques*, vol. 73, no. 1, pp. 494–503, 2025.
> DOI: 10.1109/TMTT.2024.3421558

## 方法概述

论文方法的核心：把"伴随仿真"实现为**换端口激励的第二次正常仿真**（后向场），
从而只依赖商业求解器的场解（E/H 场监视器导出），不依赖系统矩阵与网格信息。

每轮迭代（论文 Fig. 4）：

```
正向仿真（input 端口激励）      → E, H, S 参数, P_in
后向仿真（observation 端口激励）→ E_back, H_back
形状导数 δp_i = Re[−2jω/P_in·(ε E_⊥·E_⊥^back + μ0 H_∥·H_∥^back)]   ← 式(25)
梯度下降（固定步长 + 符号开关）→ 水准集 HJ 演化 → 最小间距投影 → 重建几何
```

形状导数只定义在边界采样点上，经**速度延拓**铺满窄带后驱动
Hamilton–Jacobi 方程 ∂φ/∂t + V|∇φ| = 0（Godunov 一阶上风格式）。

## 项目结构

```
eaopt/
├── config.py            # 算例配置（一个算例 = 一份 YAML，**只描述优化问题**）
├── pipeline.py          # 优化主循环（Fig. 4）+ 日志/快照/φ 快照
├── artifacts.py         # iter_NNN/ 产物读写（shape / s_params / meta / ls_phi）
├── geometry/
│   ├── levelset.py      # 水准集：SDF 初始化、HJ 演化、重初始化、速度延拓、法向
│   └── contour.py       # 零等值面提取（marching squares）与 B 样条平滑重采样
├── adjoint/
│   ├── fields.py        # FieldGrid（规则网格复矢量场）+ 三线性插值 + 法/切分解
│   ├── derivative.py    # 形状导数（论文式 25）
│   └── sampling.py      # 边界采样（排除固定金属）与导数栅格化
├── optimize/
│   ├── objective.py     # FoM 工厂（transmission 型 = |S_ij|）
│   ├── step.py          # 固定步长 + 归一化 + active 掩膜
│   └── constraints.py   # 速度掩膜（固定区边距/允许区/边缘 taper）、最小间距投影
├── solver/              # 6 个文件；**不再封装官方库**
│   ├── base.py          # Solution / SolverInterface（pipeline 只认这个契约）
│   ├── cst_setup.py     # **CST 侧单一事实来源**：布局/频点/材料/端口/工程路径
│   ├── cst_model.py     # 模板命令块（VBA 文本 + 几何常量；纯文本，不 import cst）
│   ├── cst_results.py   # S 参数 + 场条目定位/导出/解析/裁剪（顶层 import）
│   └── cst.py           # CstSolver：pipeline ↔ 两个工程的适配器（顶层 import）

configs/coupler.yaml     # 算例配置（论文 III-A 耦合器）
scripts/cst_init_fwd.py  # 建前向工程（端口 1 激励）+ 首次仿真 → iter_000/
scripts/cst_init_bwd.py  # 建反向工程（端口 3 激励）+ 首次仿真
scripts/cst_update.py    # 改形状 → 两个工程各求解一次 → 写 iter_NNN/
scripts/run_coupler.py   # 一条命令跑完整优化（缺工程时自动初始化）
scripts/plot_layout.py   # 渲染 CST 侧布局参考图（docs/layout_reference.png）
docs/server_runbook.md   # 服务器逐步操作手册（含判据与常见故障）
tests/                   # 166 项测试（不装 CST 也全绿：含假 CST 库的端到端）
```

三个 `scripts/cst_*.py` 是**完整、自包含**的程序：各自在顶层
`import cst.interface`，自己连实例/开工程/写历史表/求解/取数，中间没有封装层
（流程代码重复几份是这条路的代价）。`cst_init_fwd.py` 与 `cst_init_bwd.py`
逐字相同，只有文件顶部 `TAG` 常量区不同（`tests/test_cst_contract.py` 锁定，
防两份悄悄漂移）。

## 安装与使用

```bash
pip install -e .            # 本地（numpy/scipy/pyyaml/matplotlib）
pip install -e .[dev]       # + pytest

# CST 官方 Python 库（cst.interface / cst.results）不在 PyPI 上，由 CST
# 安装包自带；服务器上装（路径按实际安装目录）：
pip install --no-index --find-links "D:\CST 2024\Library\Python\repo\simple" \
    cst-studio-suite-link

python scripts/run_coupler.py          # 跑优化（缺工程会自动初始化，约 20 轮）
python scripts/plot_layout.py          # 渲染布局参考图（核对 CST 模型用）
python -m pytest tests/ -q             # 测试（不需要 CST）
```

## CST 侧参数在哪（`eaopt/solver/cst_setup.py`）

YAML 只管优化问题（设计区、初始/固定金属、采样、约束、优化器、水准集、
目标、输出目录）——CST 侧要用的信息**不在 YAML 里**，而在
`eaopt/solver/cst_setup.py` 的 `COUPLER = CstSetup(...)`：

- 物理/材料（论文 III-A）：频点 5 GHz（= 监视器 = 扫频 = S 参数读取）、
  εr = 3.66、tanδ = 0.0037、基板 0.762 mm、金属 35 µm PEC、端口表 (1,2,3,4)；
- 会话/运行：`attach_gui`、`port_power_w`、`export_step_mm`（缺省跟随
  `sampling.point_spacing_mm`）、`save_fields`、`project_dir`（缺省
  `<output.dir>/cst`）；
- 规则：`project_path()`、`stimulus()`（fwd=from_port / bwd=to_port）、
  `validate_objective()`。

模板命令块（`cst_model.py`）与 pipeline 的 ω、εr 都从这里取值：
**改 `frequency_ghz` 时监视器频点、扫频带、S 参数读取与形状导数同时跟着变**。
算例数据（几何布局说明）也写在那个模块的 docstring 里。

## 关键约定（错了不会报错，只出错数据）

- **激励"烤死"在模板里**：fwd 工程激励端口 1、bwd 激励端口 3，建工程时
  写死（`Solver.StimulationPort` + `StimulationMode` 成对设置），pipeline
  全程不触碰激励 API——多激励叠加的场会让伴随梯度静默失效而 S 参数照常出数；
- **建模必走历史表**：所有几何改动都经 `model3d.add_to_history(标题, 命令)`
  （每轮每工程一条记录：删 `design_region` + 重建）。直接调对象模型只改当前
  会话，重放历史时旧形状会复活；
- **先存盘再读结果**：`cst.results` 读的是磁盘结果文件，顺序反了会一直读到
  上一轮的值；
- **场条目到活结果树上按叶子名认领**（`cst_results.resolve_field_item`），
  不硬拼路径：条目名由 CST 起（带 ` [AC]` 之类后缀），而 `SelectTreeItem`
  对不存在的路径**不抛错、只是不生效**（返回 False），拼错的名字要到
  `ASCIIExport.Execute` 才以一句 "not available for the current view" 收场；
- **读不到就抛**：S 参数/场读失败一律报错，绝不把 0 塞进伴随法；
- **场要裁到设计区** ± `design_region.field_margin_mm`（监视器导出的是整个
  计算域）；
- **`shape.json` = 真正施加的形状**：两个工程都改成功后才写、每次覆盖写，
  它是事后复盘"第 N 轮模型长什么样"的唯一依据；
- **`iter_NNN/` 只有一个写入口**：脚本链与 pipeline 都走 `eaopt.artifacts`
  （脚本在轮次已知时调它，pipeline 经 `CstSolver.begin_iteration(n)` 告诉
  求解器本轮号）。

判定模板是否建对：**History List 非空** → 关掉工程再重开 → 几何与 4 个
端口还在。几何布局按论文 Fig. 5（直通线横贯整板 + "⊓"形耦合臂、设计区
两端为四分之一圆过渡、腿下到板底、端口 1/2 在线两端 / 3/4 在腿底；参考图
见 `docs/layout_reference.png`）。逐步操作、判据与常见故障见
`docs/server_runbook.md`。

## 已验证的性质（本地，不装 CST）

- **端到端**：用假 CST 库（照官方 API 形状搭）跑通"初始化 → 形状更新 →
  求解 → 读 S 参数/导场裁剪"整条链，以及一轮完整 pipeline（`iter_000/` 落下
  `shape.json`、`s_params_{fwd,bwd}.json`、`meta.json`、`ls_phi.npz`）；
- 水准集：SDF 初始化误差 <1e-10；均匀速度平移精确；Godunov 上风重初始化稳定
  （中心差分方案会振荡，勿用）；凸角附近收敛慢是 PDE 法固有特性；
- 配置系统：未知键（旧 YAML 的 `solver`/`substrate`/`ports` 等）**报错而非
  静默丢弃**——迁移期的护栏；
- 仓库合同（`tests/test_cst_contract.py`）：仓库根没有遮蔽官方库的 `cst/`；
  `cst_setup`/`cst_model`/`pipeline` 等模块导入后 `sys.modules` 里没有
  `cst`（没装 CST 的机器也能用它们）；两个 init 脚本除 `TAG` 常量区外逐字节
  相同。

## 路线图

- [x] 第 1 步：项目骨架 + 配置系统
- [x] 第 2 步：几何核心（水准集 + 轮廓）
- [x] 第 3 步：场与导数（FieldGrid + 式 25）
- [x] 第 4 步：优化闭环
- [x] 第 5 步：CST 交互（官方 Python API：两个 init / 形状更新 /
      CstSolver 适配器；CST 侧常量收敛到 `cst_setup`）
- [ ] 第 6 步：服务器验收（init → run_coupler → 按 runbook 清单核对，
      对齐论文 Fig. 6–8）
- [ ] 待办：`optimizer.velocity_sign` 未经 CST 有限差分裁决（见 runbook
      第 1 节）；`run_coupler.py --resume`（断点续跑）尚未实现。
