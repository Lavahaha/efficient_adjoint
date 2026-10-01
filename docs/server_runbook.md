# 服务器操作手册（第 7 步：CST 2024 实测与复现）

按顺序执行。任何一步报错：把完整输出（含报错）贴回给开发者。

## 0. 环境准备（一次性）

```bash
# 服务器上（需 64 位 Python 3.10+；CST 2024 已安装且许可证正常）
git clone https://github.com/Lavahaha/efficient_adjoint.git
cd efficient_adjoint
pip install -e .[server]      # numpy/scipy/pyyaml/matplotlib + pywin32/h5py
pip install pytest            # 可选：验证安装
python -m pytest tests/ -q   # 应 85 passed（本机可先确认代码完整）
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
   - **激励（最关键的一项）**：Simulation → Time Domain Solver 对话框 →
     Stimulation/Excitation 分组里，**Source type 应指向端口 1**
     （bwd 端口 3），Frequency range 应为 **0 – 10 GHz**。
     这一项错了不会让 S 参数变样（S 参数照样是全端口矩阵），但**场监视器里
     存的会是多个激励叠加的场，伴随梯度就全错了**。宏用 Solver 的
     `.StimulationPort "1"` + `.StimulationMode "1"` 设置它（不再用
     Excitation 对象——那个对象在 CST 2024 命令宏里报 10090、且失败是
     静默的）。**端口与模式必须成对**：实测 `.StimulationPort "1"` +
     `.StimulationMode "All"` 会让 Solver.Start 直接报
     "Invalid stimulation port, please specify."
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

它会依次验证并打印：

1. COM 连接 → 打开模板副本（同时打印模板 .cst 的大小与**最后修改时间**：
   若是旧时间戳，说明宏的 SaveAs 没覆盖掉旧文件，你打开的是陈旧工程）；
2. **2b. 模板 .cst 里内嵌的激励字符串**（看宏究竟把什么存了进去）；
3. **工程状态探针**：读 `mws.GetSolverType`（**属性**，别加括号——
   加了报 `'str' object is not callable`）、`Solver.GetNumberOfPorts`、
   `Solver.GetPortNames`、`ObjectExists("substrate")`、`ObjectExists("Port 1")`。
   **端口数为 0 会大声报警**——那说明模板被存成了空壳，先别管激励；
4. **激励候选闭环**：再依次试
   `"1"+"1"` / `"Port 1"+"1"` / `1+1` / `"1"+"All"` / `"All"+"All"`，
   每个 COM 调用单独 try/except（报告会点明失败在 StimulationPort、
   StimulationMode 还是 Start），每个候选都真调一次 `Solver.Start()` ——
   写法不对会**立刻**报 "Invalid stimulation port"（不耗时），试到能跑
   为止（**能跑的那次会真的算完，几分钟**；期间 CST 界面若弹报错
   对话框，点掉即可）；
5. **COM 方法枚举**：直接问类型库要 `mws.ResultTree` / `mws.ASCIIExport`
   / `mws.Solver` 的**真实成员表**（工程对象按 `Result/Export/Tree/
   Field/ASCII` 过滤打印）——比逐个猜 API 名字可靠，一轮就能定下来；
6. S 参数读取：先列 `1D Results` 与 `1D Results\S-Parameters` 的真实
   子条目（**原始报错一并打印**：列举为空既可能是"真没结果"，也可能是
   "方法名不对"），再用解析到的真实路径走
   `GetResultIDsFromTreeItem` + `GetResultFromTreeItem` +
   `GetArray("x"/"yre"/"yim")`；随后是 4c 备选链
   `GetFileFromTreeItem` + `Result1DComplex`；
7. 结果树中监视器条目的**惯例路径 vs 实际匹配** + `SelectTreeItem` 实测；
8. E 场 ASCII 导出：用实际匹配到的条目名导出 + **属性探针**（列出哪些
   ASCIIExport 属性真的存在）+ 导出文件头 20 行。

**把完整输出贴回给开发者**——用于把 `CstSolver` 的候选 API 列表与
`ascii_fields` 解析器收敛到 CST 2024 真实格式（大概率只需一轮修正）。

自检：smoke 中若求解正常且第 4 节能给出合理 S31（|S31| 约 −18 dB
量级，与布局细节有关），即可进入下一步。若第 3 节所有候选都失败，
smoke 会打印"改用 GUI 设置 + 录宏"的步骤，照做并把生成的 VBA 贴回。

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

**初始值对不上论文是正常的**：论文初始设计 5 GHz 处 |S31| ≈ −17.9 dB、
定向性 ≈ 4.6 dB，但 |S31| 对耦合段电长度极敏感（d 接近 λg/4 附近，
εr、h、金属厚度、端口尺寸的一点点差异都会让耦合电平移动几个 dB），
而我们的几何是从论文插图上量的、端口尺寸也是按经验取的。判断复现是否
成功的标准是**趋势与终点**：|S31| 应被显著抬高（约 +8 dB 量级）并收敛
到 −10 dB 附近、定向性同步升到 17 dB 附近。

## 常见问题

- **COM 连接失败**：先启动 CST GUI 保持运行（脚本会附接运行实例）；
  或确认许可证正常、CST 可以独立打开。
- **宏运行到某条命令报错**：把宏日志/报错截图或文本贴回，按 2024
  版本命令名修正宏生成器。**几何/端口段**的报错会中止宏（这是故意的：
  模板建不出来就没有意义）；**设置段**（激励/监视器/边界/求解器）
  已逐块容错，只会出现在结尾报告框里，不会中止。
- **`(10090) ActiveX Automation error. (.Reset)`**：CST 2024 命令宏
  上下文里 `Excitation.Reset` 会报这个（实测两次）。**模板宏已不再使用
  Excitation 对象**，"只激励哪个端口"改用 Solver 的 `.StimulationPort`
  （见下一条：要带模式）。其余设置类块（频段/监视器/边界/求解器/另存）
  都用 `On Error Resume Next` 逐个包住 → 失败不中止宏，结尾报告框会
  点名哪个块失败，照提示在 GUI 手工补（频段：Simulation → Frequency；
  监视器：Home → Field Monitors，建 `e-field (f=5)` 与 `h-field (f=5)`
  两个 Volume 监视器；边界：Boundaries；求解器：Time Domain Solver
  对话框）。把报告框截图发回，用于按版本修正宏。
- **`Invalid stimulation port, please specify.`（Solver.Start 时报）**：
  两种原因，报错文本能区分——
  - 带 **`please specify a positive integer value or "All"`** ⇒ 值格式错
    （`"Port 1"` 这种就报这个）；
  - **不带**这半句 ⇒ 格式对（`"1"` 是数字字符串）但**该端口不存在**，
    先用 smoke 第 3 节的 `Solver.GetNumberOfPorts` 看工程里到底有几个端口。
  另外实测 `.StimulationPort "1"` 配 `.StimulationMode "All"` 也会炸：
  **端口与模式必须成对**（`.StimulationPort "1"` + `.StimulationMode "1"`，
  单模端口）。宏本身不报错、SaveAs 也正常，只在求解时炸。
  `scripts/cst_smoke.py` 第 3/4 节会逐个候选试到能跑为止。
- **通用兜底：任何 GUI 设置不知道怎么写成宏**——在 GUI 里手工做那一步
  （比如 Time Domain Solver 对话框里选端口），然后 **Edit → History
  List**，选中刚出现的行 → 点 **Macro** 按钮 → 生成对应 VBA → 原样贴回。
  这比查文档可靠（CST 各版本命令名有出入）。
- **结果读取 API（CST 2024 实测）**：`ResultTree.GetResultItem(...)` 与
  `ResultTree.GetAllItems()` **都不存在**（报 `<unknown>.xxx`）。可用的是
  `GetResultIDsFromTreeItem(path)` → `GetResultFromTreeItem(path, id)` →
  `GetArray("x"/"yre"/"yim")`；列目录用 `GetFirstChildName(folder)` /
  `GetNextItemName(item)`（返回空串结束）。这些候选链收敛在
  `eaopt/solver/cst_api.py`，生产代码与 smoke 共用。
  **两个容易踩的点**：① `GetResultIDsFromTreeItem` 要的是**条目**路径
  （`1D Results\S-Parameters\S1,1`），给文件夹通常返回空列表；② 结果是
  **读不到就报原始错误**，不再静默返回空——"列举为空"与"方法名不对"
  是两种病，看 smoke 里 `[诊断] ...` 那几行区分。路径不要硬拼：CST 会给
  结果条目自动加后缀（`e-field (f=5)` → `… [AC]`），用
  `cst_api.find_item(rt, 文件夹, 监视器名)` 按前缀找真实路径，找不到才
  退回惯例路径。
- **不知道某版本有哪些方法可用**：跑 smoke 第 5 节——它直接读 IDispatch
  的类型库，把 `mws` / `ResultTree` / `ASCIIExport` / `Solver` 的**真实
  成员名**打出来（比照文档猜名字可靠）。本地也可以这么干：
  `obj._oleobj_.GetTypeInfo()` → `GetTypeAttr().cFuncs` → `GetFuncDesc(i)`
  → `GetNames(fd.memid)`。
- **ASCIIExport 的属性集（CST 2024 实测）**：只有 `Reset` / `FileName` /
  `Mode` / `StepX` / `StepY` / `StepZ` / `Execute`——**没有
  XStart/XEnd/YStart/YEnd/ZStart/ZEnd**（报 `<unknown>.XStart`）。所以
  导出范围 = 选中结果的整个包围盒（Volume 监视器 ⇒ 整个计算域），要限制
  范围只能在解析端裁剪。属性清单是 `vba.ascii_export_params()` 一份事实
  来源，cst.py 与 smoke 共用。
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
- **`(10097) ActiveX Automation: wrong number of parameters. (SaveAs "...")`**：
  CST 命令宏里 `SaveAs` 必须带**两个**参数（路径 + 布尔）。宏已改成
  先试 `SaveAs "<路径>", "False"`、失败再试 `"True"`（两种布尔的含义在
  不同版本文档里说法不一：覆盖开关 / 另存副本），并把这一步放进容错区。
  若两种都失败，报告框会点名 `SaveAs`，此时手工 File → Save As 保存即可。
- **模板存出来的工程内容不全 / 像是空壳**（老版本 CST 有"宏保存的项目丢了
  端口/监视器"的报告）：跑 smoke 就能发现——模板 .cst 只有 0.04 MB、
  `Solver.GetNumberOfPorts` 为 0、`ObjectExists("substrate")` 为 False、
  结果树里没有 `e-field (f=5) [AC]`=监视器没存上。**先看 smoke 第 2 节打印的
  模板最后修改时间**：若是旧时间戳，说明宏的 `SaveAs` 没能覆盖旧文件（第二
  个布尔参数在不同版本里可能是"覆盖开关"而不是"另存副本"），此时宏报告框
  仍会写 "saved OK"——只要在 GUI 里 **File → Save As 手工覆盖**一次（或干脆
  删掉旧 .cst 再跑宏）即可。若时间戳是新的但内容仍缺，就手工补端口/监视器
  后另存，并把 GUI 里 Ports 树和 Field Monitors 的截图发回。
