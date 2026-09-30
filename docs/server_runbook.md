# 服务器操作手册（第 7 步：CST 2024 实测与复现）

按顺序执行。任何一步报错：把完整输出（含报错）贴回给开发者。

## 0. 环境准备（一次性）

```bash
# 服务器上（需 64 位 Python 3.10+；CST 2024 已安装且许可证正常）
git clone https://github.com/Lavahaha/efficient_adjoint.git
cd efficient_adjoint
pip install -e .[server]      # numpy/scipy/pyyaml/matplotlib + pywin32/h5py
pip install pytest            # 可选：验证安装
python -m pytest tests/ -q   # 应 54 passed（本机可先确认代码完整）
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
4. 运行宏 `Main` → 自动另存为 `cst/coupler_fwd.cst`
5. **再 File → New**，导入并运行 `cst/build_coupler_bwd.mcr` →
   `cst/coupler_bwd.cst`
6. 检查产物：两个 .cst 都已生成
7. **打开 fwd 工程检查**（布局 = 论文 Fig.5）：
   - 模型：基板（Rogers4350B 30mil，x∈[−5.6,17.6] y∈[−7,5.6]）、
     接地 PEC、空气盒（Vacuum，z 到 4.0）、直通线（上方横贯整板）、
     "⊓"形耦合臂（横段 + 两条腿下到板底）、设计区矩形金属
     （design_region 组件）
   - 4 个波导端口：1/2 在直通线两端（xmin/xmax 面）、3/4 在两腿底
     （ymin 面）；端口面下缘触到接地板底面、上缘到空气盒顶
   - 5 GHz 的 E-Field / H-Field 监视器各一个
   - 边界：X/Y/Zmin = magnetic，Zmax = electric
   - **检查腿与耦合臂的交界**：切换俯视图（视图工具条点 z 轴）
     或只显示 design_region 组件，交界应是**直线直角**（模板里没有
     任何挤出/曲线对象，全部是 Brick）。若看到弧边，跑第 2.5 节的
     诊断宏并把截图发回
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
  版本命令名修正宏生成器。
- **端口相关（CST 2024 实测结论，改宏时勿违反）**：
  `.Coordinates` 只认 `"Free"/"Full"/"Picks"`（写 `"Ranges"` 报
  "Invalid coordinate type"）；`.Orientation` 只认**边界面名**
  `"xmin"/"xmax"/"ymin"/"ymax"`；微带类端口必须 `"Free"` + 显式
  Xrange/Yrange/Zrange，且端口面下缘要贴合接地板底面。
- **端口报错（如 "port is too small"）**：把端口面横向余量调大
  （`eaopt/solver/template_builder.py` 的 `_ports()` 中 `m`），重新生成宏。
- **SaveAs 未生效**：手工 File → Save As 保存两个模板工程。
