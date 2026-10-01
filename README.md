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
正向仿真（input 端口激励）   → E, H, S 参数, P_in
后向仿真（observation 端口激励）→ E_back, H_back
形状导数 δp_i = Re[−2jω/P_in·(ε E_⊥·E_⊥^back + μ0 H_∥·H_∥^back)]   ← 式(25)
梯度下降（固定步长 + 符号开关）→ 水准集 HJ 演化 → 最小间距投影 → 重建几何
```

形状导数只定义在边界采样点上，经**速度延拓**铺满窄带后驱动
Hamilton–Jacobi 方程 ∂φ/∂t + V|∇φ| = 0（Godunov 一阶上风格式）。

## 项目结构

```
eaopt/
├── config.py            # 算例配置系统（一个算例 = 一份 YAML）
├── geometry/
│   ├── levelset.py      # 水准集：SDF 初始化、HJ 演化、重初始化、速度延拓、法向
│   └── contour.py       # 零等值面提取（marching squares）与 B 样条平滑重采样
├── adjoint/
│   ├── fields.py        # FieldGrid（规则网格复矢量场）+ 三线性插值 + 法/切分解
│   ├── derivative.py    # 形状导数（论文式 25）
│   ├── sampling.py      # 边界采样（排除固定金属）与导数栅格化
│   └── fd_check.py      # 有限差分验证（符号裁决 + 数值一致性）
├── solver/
│   ├── base.py          # Solution / SolverInterface / make_solver 工厂
│   ├── mock.py          # MockSolver：2D 拉普拉斯静电场玩具模型（本地验证用）
│   ├── cst.py           # CstSolver：CST 2024 双模板 + COM 场导出
│   ├── cst_api.py       # COM 结果读取候选链（版本 API 名称差异收敛在此）
│   ├── vba.py           # CST VBA 命令串（建模/端口/监视器/求解器/导出）
│   ├── template_builder.py  # 双模板宏生成（建模 .mcs + 另存 .mcr）
│   └── ascii_fields.py  # CST ASCII 场文件解析
├── optimize/
│   ├── objective.py     # FoM 工厂（transmission 型 = |S_ij|）
│   ├── step.py          # 固定步长 + 归一化 + active 掩膜
│   └── constraints.py   # 速度掩膜（固定区边距/允许区/边缘 taper）、最小间距投影
└── pipeline.py          # 优化主循环（Fig. 4）+ 日志/快照

configs/coupler.yaml     # 算例配置（论文 III-A 耦合器；设计区尺寸按 Fig.5 定稿）
scripts/run_coupler.py   # 运行入口
scripts/fd_check.py      # FD 验证命令行工具
scripts/plot_layout.py   # 渲染 CST 侧布局参考图（docs/layout_reference.png）
tests/                   # 93 项测试（水准集数值、导数、mock、FD、端到端、VBA 宏、CST API）
```

## 安装与使用

```bash
pip install -e .            # 本地（numpy/scipy/pyyaml/matplotlib）
pip install -e .[dev]       # + pytest
pip install -e .[server]    # 服务器端 + pywin32/h5py（CST COM）

python scripts/run_coupler.py          # 跑优化（solver.type=mock 时本地）
python scripts/fd_check.py             # FD 验证（符号裁决）
python scripts/plot_layout.py          # 渲染布局参考图（核对 CST 模型用）
python -m pytest tests/ -v             # 测试
```

## 配置要点（configs/coupler.yaml）

- 坐标约定：设计平面 x-y（mm），金属沿 z 挤出（35 µm）；φ<0 为金属
- 边界条件（论文）：x、y、z-min 磁边界，z-max 电边界；频点 5 GHz
- `sampling.sample_offset_mm` 必须 **> 半网格步长**（插值窗口跨金属边界会压掉一半场）
- `optimizer.velocity_sign`：导数→速度的符号开关；mock 世界 FD 已验证 +1 正确，
  CST 端待服务器 FD 验证裁决
- 约束：`min_gap_mm`（硬投影）、`allowed_region`（速度掩膜）、固定区自动外扩
  `max(min_gap, 2dx)` 边距防速度泄漏

## 已验证的性质

- **FD 一致性（mock，解析级）**：式(25) 整条链退化为 Hadamard 电容形状导数，
  实测/预测比值与理论值 0.3·P_in/(2ω·C0) 偏差 14%（`scripts/fd_check.py`）
- 水准集：SDF 初始化误差 <1e-10；均匀速度平移精确；Godunov 上风重初始化稳定
  （中心差分方案会振荡，勿用）；凸角附近收敛慢是 PDE 法固有特性
- 符号：mock 世界 velocity_sign=+1（V>0 金属扩张）使 FoM 上升

## MockSolver 的已知局限（不影响 CST 端）

- 电容-几何曲线有栅格化锯齿（固定网格 + 单元翻转），FoM 非严格单调、
  可能过早触发收敛窗口——真实求解器的平滑 S 参数无此问题
- 量化台阶使 FD 验证需要 h_eff 校准（脚本自动完成）

## CST 服务器流程（第 6–7 步，CST 2024）

代码已就绪（`eaopt/solver/cst.py` + `eaopt/solver/cst_api.py` +
`eaopt/solver/vba.py` + `eaopt/solver/ascii_fields.py` +
`eaopt/solver/template_builder.py`，102 项本地测试全过）；服务器实测步骤：

1. 生成模板宏：`python scripts/build_cst_template.py cst/`（产物
   `cst/build_coupler_{fwd,bwd}.mcs` = 建模**结构宏**、
   `cst/save_coupler_{fwd,bwd}.mcr` = 另存**控制宏**，以及诊断宏
   `polygon_test.mcs`。**建模必须用结构宏**：CST 的模型是"历史表重放"
   出来的，控制宏的动作不进 History List —— 会话里几何/端口都正常，
   存盘重开却是空工程（实测踩过，"打开一片空白"的根因）。另存是工程级
   指令、只在控制宏里合法，故拆成两个文件）；
2. CST GUI：**File → New**（模板 `<None>`）→ **从主界面 Macros 下拉菜单**
   运行 `build_coupler_fwd.mcs`（**不要**在 VBA 编辑器里点运行图标——
   那样即使是结构宏也不写历史表）→ 结尾弹报告框 →
   **先确认 History List 非空** → 运行 `save_coupler_fwd.mcr`（或手工
   File → Save As）→ `coupler_fwd.cst`（端口 1 激励）；
   **再 File → New** → 同样跑 bwd 那两个宏 → `coupler_bwd.cst`
   （端口 3 激励）。几何布局按论文 Fig.5（直通线横贯
   整板 + "⊓"形耦合臂、**设计区两端为四分之一圆过渡**、腿下到板底、
   端口 1/2 在线两端 / 3/4 在腿底；参考图见 `docs/layout_reference.png`）；
   检查 4 个波导端口（`.Coordinates "Free"` + 边界面名 `xmin/xmax/ymin`，
   端口面下缘贴合接地板、上缘到空气盒顶）、两个 Volume 监视器
   `e-field (f=5)` / `h-field (f=5)`（名字 = 结果树条目名，导出场按它选中，
   见 `vba.field_monitor_name`）、边界（x/y/zmin 磁、zmax 电）、
   **激励只勾选本模板的端口**（fwd→端口 1，bwd→端口 3；用
   `Solver.StimulationPort` + `Solver.StimulationMode` **成对**设置——
   实测端口配 `"All"` 会让求解直接报 "Invalid stimulation port"）
   + 频段 0–10 GHz。
   **设置类块（激励/频段/监视器/边界/求解器/另存）逐块容错**：CST 2024
   实测设置类命令报过 "(10090) ActiveX Automation error" 与
   "(10097) wrong number of parameters"，未加保护会中止整个宏；现由宏
   结尾的报告框列出失败块，照提示在 GUI 手工设置即可；
3. 把两个模板路径填入 `configs/coupler.yaml` 的 `solver` 段，
   `solver.type: cst`；
4. 先跑 smoke：`python scripts/cst_smoke.py`——输出 COM 连接、求解、
   S 参数读取候选方法、结果树条目、场导出文件头（用于核对
   `ascii_fields` 解析器与 CST 2024 真实格式），把完整输出贴回给开发者
   收敛候选 API；
5. `python scripts/fd_check.py`（CST 上 FD 验证，重新裁决符号）→
   `python scripts/run_coupler.py` 正式优化（预期约 20 次迭代，
   参考论文 55 min）。

## 路线图

- [x] 第 1 步：项目骨架 + 配置系统
- [x] 第 2 步：几何核心（水准集 + 轮廓）
- [x] 第 3 步：场与导数（FieldGrid + 式 25）
- [x] 第 4 步：求解器抽象 + MockSolver + 优化闭环
- [x] 第 5 步：本地端到端验证 + FD 检查工具
- [x] 第 6 步：CST 接口代码（双模板设计 + VBA 生成 + ASCII 场解析，
      59 测试全过；服务器实测待 smoke）
- [ ] 第 7 步：服务器 smoke → FD 验证 → 耦合器正式复现（对齐论文 Fig. 6–8）
