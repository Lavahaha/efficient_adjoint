# 服务器操作手册（第 7 步：CST 2024 实测与复现）

按顺序执行。任何一步报错：把完整输出（含报错）贴回给开发者。

## 0. 环境准备（一次性）

```bash
# 服务器上（需 64 位 Python 3.10+；CST 2024 已安装且许可证正常）
git clone https://github.com/Lavahaha/efficient_adjoint.git
cd efficient_adjoint
pip install -e .[server]      # numpy/scipy/pyyaml/matplotlib + pywin32/h5py
pip install pytest            # 可选：验证安装
python -m pytest tests/ -q   # 应 65 passed（本机可先确认代码完整）
```

## 1. 在服务器上生成模板宏（重要：宏里包含绝对路径，必须在服务器上重新生成）

```bash
python scripts/build_cst_template.py cst/
# 产物: cst/build_coupler_fwd.mcr（端口 1 激励）
#       cst/build_coupler_bwd.mcr（端口 3 激励）
#       cst/polygon_test.mcr（诊断宏，可选，见第 2.5 节）
```

## 2. CST GUI 生成双模板工程

**宏里不含 NewProject**——CST 里新建/另存工程这类工程级指令只在命令宏
（.mcr）上下文合法，实测在结构宏（.mcs）中会报 "Invalid instruction
(NewProject)"。所以新建工程由你在 GUI 里做，宏只负责建模 + 另存。

1. 打开 CST Studio 2024（GUI）
2. **File → New**（模板选 `<None>`）新建一个空工程
3. Home → Macros → Import Macro...，在导入对话框里把**文件类型切到
   "CST Macro Files (\*.mcs; \*.mcr)"**（默认过滤器看不到宏文件），
   选中 `cst/build_coupler_fwd.mcr` 打开
   （不同小版本 UI 文案略有差异；另一条路：把文件直接拖进 CST 窗口）
4. 运行宏 `Main` → 结尾弹**报告框** → 自动另存为 `cst/coupler_fwd.cst`
   - 报告框写 "Template saved OK ... all blocks applied" = 全部成功；
   - 写 "Template saved, but some blocks FAILED ..." = 括号里那些块
     （激励/监视器/边界/求解器）**没生效**，按报告里的名字在 GUI 手工补，
     再保存。**把这张报告框截图发回开发者**（用于按版本修正宏）。
5. **再 File → New**，导入并运行 `cst/build_coupler_bwd.mcr` →
   `cst/coupler_bwd.cst`
6. 检查产物：两个 .cst 都已生成
7. **打开 fwd 工程检查**（布局 = 论文 Fig.5）：
   - 模型：基板（Rogers4350B 30mil，x∈[−5.6,17.6] y∈[−7,5.6]）、
     接地 PEC、空气盒（Vacuum，z 到 4.0）、直通线（上方横贯整板）、
     "⊓"形耦合臂（横段 + 两条腿下到板底）、设计区金属
     （design_region 组件：横段 + 两端内侧圆角）
   - 4 个波导端口：1/2 在直通线两端（xmin/xmax 面）、3/4 在两腿底
     （ymin 面）；端口面下缘触到接地板底面、上缘到空气盒顶
   - 监视器（Modeling 树 → Field Monitors）：`e-field (f=5)` 与
     `h-field (f=5)` 各一个，**Dimension = Volume**（覆盖整个计算域，
     没有"位置"这个设置——所以它必然贴着四个端口面，端口 2 也在其中，
     这是正常的；导出场时只按设计区附近 z=±0.1 mm 的薄层取数）。若对话框
     里 Dimension 变成了 Plane/Position，说明宏的 Monitor 块没生效，
     按报告框提示手工建两个 Volume 监视器
   - 边界：X/Y/Zmin = magnetic，Zmax = electric
   - **检查两端拐弯过渡**（论文 Fig.5 的四分之一圆）：俯视图看，
     耦合臂横段与两条腿的连接应是**平滑等宽圆角**（腿上端外缘向外
     弯、臂端下缘向内弯，二者同心），而不是直角。对照
     `docs/layout_reference.png`（本地渲染的同一几何；可随时用
     `python scripts/plot_layout.py` 重新生成）。腿与臂在 x=0 / x=12
     处相接，**不应有缝**。若形状不符（例如成了直角、或出现明显
     折线感），把俯视图截图发回
   - **激励**：端口 1 被勾选（Excitations 下应有 excitation1 → Port 1）。
     若没有（宏的 Excitation 命令被版本拒绝）：在端口对话框中手工勾选
     端口 1 的激励，保存
8. **bwd 工程同样检查，激励改为端口 3**，保存

## 2.5 （可选）诊断宏 polygon_test.mcr

pipeline 每轮迭代都用 `Extrude "Pointlist"` 重建任意轮廓（design_region
组件），所以必须确认这个模式生成的是**直边多边形**（尖角保留），而不是
把点列拟合成曲线。

- 在任意**空工程**里运行 `cst/polygon_test.mcr`（File → New → 导入宏 →
  运行 Main），会建出两个实体：L 形（6 点、含 90° 内角）和方形（4 点）
- 俯视图看：两者都应是直边、尖角
- **只在第 7 步看到弧边时才需要做这一步**；把结果（或截图）发回开发者。
  若确实出现弧边，重建方式要改（这是 pipeline 的硬依赖）

> 若宏在某条指令上报错（如 "Invalid instruction (xxx)" 或
> "no such property (xxx)"）：把那条指令贴回来即可。**建模是逐条执行的，
> 报错前的部分已经建好**，多数情况只需在 GUI 里手工补那一步，不必重跑。

## 3. 修改配置

编辑 `configs/coupler.yaml` 的 solver 段：

```yaml
solver:
  type: cst                # 由 mock 改为 cst
  template_fwd: cst/coupler_fwd.cst   # 若路径不同按实际改
  template_bwd: cst/coupler_bwd.cst
  cst_version: "2024"
  field_backend: ascii
```

## 4. Smoke 测试（关键诊断步骤）

```bash
python scripts/cst_smoke.py configs/coupler.yaml
```

它会依次验证并打印：COM 连接 → 打开模板副本 → 时域求解（会真实跑一次
仿真，几分钟）→ S 参数读取的**每个候选方法**哪个可用 → 结果树中监视器
条目的实际路径 → E 场 ASCII 导出（含文件头 20 行）。

**把完整输出贴回给开发者**——用于把 `CstSolver` 的候选 API 列表与
`ascii_fields` 解析器收敛到 CST 2024 真实格式（大概率只需一轮修正）。

自检：smoke 中若求解正常，`GetValueAtFrequency` 可用且给出合理 S31
（|S31| 约 −18 dB 量级，与布局细节有关），即可进入下一步。

## 5. FD 验证（符号裁决）

```bash
python scripts/fd_check.py configs/coupler.yaml
```

注意：CST 端的 fd_check 扰动实现（轮廓偏移→重建）在 mock 版基础上需要
按 smoke 结果适配后才能跑；先把 smoke 输出给开发者，此步在修正后执行。

## 6. 正式优化复现

```bash
python scripts/run_coupler.py configs/coupler.yaml
```

预期：约 20–30 次迭代（每次 = 2 次 CST 仿真 + 场导出，论文参考 55 min/20 次），
输出在 `results/coupler/`（history.jsonl、每轮形状快照、fom.png）。
对照论文 Fig. 6–8：|S31| 由初始值升至约 −10 dB，定向性升至约 17 dB。

## 常见问题

- **COM 连接失败**：先启动 CST GUI 保持运行（脚本会附接运行实例）；
  或确认许可证正常、CST 可以独立打开。
- **宏运行到某条命令报错**：把宏日志/报错截图或文本贴回，按 2024
  版本命令名修正宏生成器。**几何/端口段**的报错会中止宏（这是故意的：
  模板建不出来就没有意义）；**设置段**（激励/监视器/边界/求解器）
  已逐块容错，只会出现在结尾报告框里，不会中止。
- **`(10090) ActiveX Automation error. (.Reset)`**：CST 2024 命令宏
  上下文里 `Excitation.Reset` 会报这个（实测）。模板宏已用
  `On Error Resume Next` 包住该类块 → 宏继续跑完并另存，结尾报告框
  会写明哪个块失败，照提示在 GUI 手工设置该块即可（激励：端口对话框
  里勾选；监视器：Home → Field Monitors，建 `e-field (f=5)` 与
  `h-field (f=5)` 两个 Volume 监视器；边界：Boundaries；求解器：
  Time Domain Solver 对话框）。把报告框截图发回，用于按版本修正宏。
- **监视器命名不能随便改**：CST 用监视器名命名结果树条目
  （`2D/3D Results\E-Field\e-field (f=5) [AC]`），`CstSolver` 导出场时
  就按这个名字选中条目。名字由 `vba.field_monitor_name()` 统一给出，
  创建与导出共用（有测试锁定），改宏时保持该名字。
- **端口相关（CST 2024 实测结论，改宏时勿违反）**：
  `.Coordinates` 只认 `"Free"/"Full"/"Picks"`（写 `"Ranges"` 报
  "Invalid coordinate type"）；`.Orientation` 只认**边界面名**
  `"xmin"/"xmax"/"ymin"/"ymax"`；微带类端口必须 `"Free"` + 显式
  Xrange/Yrange/Zrange，且端口面下缘要贴合接地板底面。
- **端口报错（如 "port is too small"）**：把端口面横向余量调大
  （`eaopt/solver/template_builder.py` 的 `_ports()` 中 `m`），重新生成宏。
- **SaveAs 未生效**：手工 File → Save As 保存两个模板工程。
