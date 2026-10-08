# 服务器操作手册（第 6 步：CST 2024 验收与复现）

按顺序执行。任何一步报错：把**完整输出**（含报错原文）贴回给开发者。

CST 交互全部走**官方 Python 库**（`cst.interface` / `cst.results`，随 CST
安装包提供），没有 COM/pywin32。所有几何改动都经
`model3d.add_to_history(标题, 命令文本)`——**既执行、又写进 History
List**，坏 VBA 会当场抛 `RuntimeError`（失败完全可见，这是换官方库最大
的收益）。

CST 侧的一切参数（频点/材料/端口/会话/工程路径）都在
`eaopt/solver/cst_setup.py` 的 `COUPLER`——服务器上要调试时改那里，
不要在 YAML 里找（YAML 只有优化侧配置）。

## 0. 环境准备（一次性）

```bash
# 服务器上（官方库只支持 64 位 Python 3.6–3.11；CST 2024 已安装且许可证正常）
git clone https://github.com/Lavahaha/efficient_adjoint.git
cd efficient_adjoint
pip install -e .            # numpy/scipy/pyyaml/matplotlib
pip install pytest          # 可选：验证安装
python -m pytest tests/ -q  # 应 269 passed, 1 skipped（不装 CST 也能全绿）

# 官方 Python 库（不在 PyPI 上，CST 安装包自带）
pip install --no-index --find-links "D:\CST 2024\Library\Python\repo\simple" \
    cst-studio-suite-link
python -c "import cst.interface; print(cst.interface.__file__)"
```

> **导入守卫**：仓库根目录下**不要**有叫 `cst/` 的文件夹——它会被 Python
> 当成命名空间包、遮蔽官方库，`import cst` "成功"而 `cst.interface` 报错。
> 三个 `scripts/cst_*.py` 与 `eaopt/solver/cst.py` 在**顶层**直接
> `import cst.interface`（不封装官方库的代价），失败时给的就是这段提示；
> `tests/test_cst_contract.py` 也把"仓库根没有 `cst/`"锁成合同。

## 1. 建两个工程（分步；也可跳过直接跑第 2 节，它会自动建）

```bash
python scripts/cst_init_fwd.py configs/coupler.yaml   # 端口 1 激励 → iter_000/
python scripts/cst_init_bwd.py configs/coupler.yaml   # 端口 3 激励
```

产物：`results/coupler/cst/coupler_fwd.cst`（工程，含模型历史）与
`results/coupler/iter_000/`（S 参数；`--save-fields` 时另有 E/H 场）。

每个块都写进 History List 并**立刻存盘**；工程已存在时脚本会拒绝覆盖
（要重建就自己删掉 `.cst` **连同同名文件夹**）。

### 模板验证清单（判定建工程成功的唯一标准）

1. **History List 非空**（Modeling 树 / Home → History List）：应看到
   Units / Material / Brick / Extrude / Port / Monitor / Boundary 一条条
   记录。**空的就停下来**，把完整输出贴回开发者。
2. **关掉工程再重新打开那个 `.cst`**，确认几何与 4 个端口还在
   （历史表为空的话这一步就会变空——那才是问题所在）。
3. 文件层再确认（不用 CST）：`.cst` 是 `<名字>.cst` + **同名文件夹**两件
   东西，搬运/复制时只搬 `.cst` 会打开成缺模型的工程——连文件夹一起搬。

以下细节供核对：

- 模型（论文 Fig.5）：基板（Rogers4350B 30mil，x∈[−5.6,17.6]
  y∈[−7,5.6]）、接地 PEC、空气盒（Vacuum，z 到 4.0）、直通线（上方横贯
  整板）、"⊓"形耦合臂（横段 + 两条腿下到板底）、设计区金属
  （`design_region` 组件：横段 + 两端内侧圆角）；
- **两端拐弯过渡**（论文 Fig.5 的四分之一圆）：俯视图看，耦合臂横段与
  两条腿的连接应是**平滑等宽圆角**（腿上端外缘向外弯、臂端下缘向内弯，
  二者同心），而不是直角。对照 `docs/layout_reference.png`（本地渲染的
  同一几何；可随时用 `python scripts/plot_layout.py` 重新生成）。腿与臂
  在 x=0 / x=12 处相接，**不应有缝**。若形状不符，把俯视图截图发回；
- **4 个波导端口**：1/2 在直通线两端（xmin/xmax 面）、3/4 在两腿底
  （ymin 面）；端口面下缘触到接地板底面、上缘到空气盒顶；
- **监视器**（Modeling 树 → Field Monitors）：`e-field (f=5)` 与
  `h-field (f=5)` 各一个，**Dimension = Volume**（覆盖整个计算域，没有
  "位置"这个设置——所以它必然贴着四个端口面，端口 2 也在其中，这是正常
  的；导出场时只取设计区 ± 余量内的数据）。**名字不要改**：CST 用监视器
  名命名结果树条目，导出场按它选中，名字由 `cst_model.field_monitor_name()`
  统一给出（创建与导出共用，有测试锁定）；
- **边界**：X/Y/Zmin = magnetic，Zmax = electric；
- **激励（最关键的一项）**：Simulation → Time Domain Solver 对话框 →
  Stimulation/Excitation 分组里，**Source type 应指向端口 1**
  （fwd；bwd 为端口 3），Frequency range 应为 **0 – 10 GHz**。这一项错了
  不会让 S 参数变样（S 参数照样是全端口矩阵），但**场监视器里存的会是
  多个激励叠加的场，伴随梯度就全错了**。模板用 `Solver` 的
  `.StimulationPort "1"` + `.StimulationMode "1"` 设置它（不用 Excitation
  对象——那个对象在 CST 2024 报 10090、且失败是静默的）。**端口与模式
  必须成对**：实测 `.StimulationPort "1"` + `.StimulationMode "All"` 会让
  `Solver.Start` 直接报 "Invalid stimulation port, please specify."

## 2. 正式优化复现（一条命令）

```bash
python scripts/run_coupler.py configs/coupler.yaml
# 服务器无 GUI 许可时：--attach（附接已开着的 CST）或 --new（强制静态实例）
# 想把 E/H 场也存下来复盘：--save-fields
```

工程不存在时**自动初始化**（与两个 init 脚本同一套命令块：建模型 + 存盘，
**但不跑首轮仿真、不写 `iter_000/`**——首轮求解由主循环的第 0 轮完成，所以
`iter_000/` 是两个 tag 都求解完才落盘）；两个工程都齐就直接进主循环。
预期：约 20–30 次迭代（每次 = 2 次 CST 仿真 + 场导出，论文参考 55 min/20
次），输出在 `results/coupler/`。

**第一次跑先小步数**：把 `configs/coupler.yaml` 的
`optimizer.max_iterations` 改成 3、`convergence_window` 保持较大，盯
`history.jsonl` 里 `fom` 的走向（见第 4 节），确认方向对了再放开步数。

中途报错**不会**留下半截模型：`add_to_history` 是整块生效的，CST 拒绝坏块
= 模型停在这一块之前的状态（收尾时存的那次盘存的就是旧状态）。改完代码
直接重跑 `run_coupler.py` 即可（工程复用，仍从第 0 轮迭代）。

## 3. 产物清单（每轮验收）

每轮 `<output.dir>/iter_NNN/` 应同时有：

| 文件 | 内容 | 缺了说明 |
|---|---|---|
| `shape.json` | 该轮**真正施加**的多边形（mm） | 形状更新没走到落盘 |
| `s_params_fwd.json` | fwd 工程（端口 1 激励）的 S 参数 | 读结果失败或没求解 |
| `s_params_bwd.json` | bwd 工程（端口 3 激励）的 S 参数 | 同上（bwd 没跑） |
| `meta.json` | 该轮的标签清单 | 半截轮次 |
| `ls_phi.npz` | 产生该轮形状的 φ 快照（`cst_update --from-ls` 用） | pipeline 没跑到 |

外加 `<output.dir>/history.jsonl`（逐轮 FoM）与 `fom.png`（收敛曲线）。

**验证**：CST 里看第 N 轮，设计区形状应逐轮变化，且 History List 每轮
只**多一条**记录（删+重建合成一条）。若形状没变或历史表暴涨，贴回输出。

对照论文 Fig. 6–8：|S31| 由初始值升至约 −10 dB，定向性升至约 17 dB。

**初始值对不上论文是正常的**：论文初始设计 5 GHz 处 |S31| ≈ −17.9 dB、
定向性 ≈ 4.6 dB，但 |S31| 对耦合段电长度极敏感（d 接近 λg/4 附近，
εr、h、金属厚度、端口尺寸的一点点差异都会让耦合电平移动几个 dB），
而我们的几何是从论文插图上量的、端口尺寸也是按经验取的。判断复现是否
成功的标准是**趋势与终点**：|S31| 应被显著抬高（约 +8 dB 量级）并收敛
到 −10 dB 附近、定向性同步升到 17 dB 附近。

## 3.5 交点采样与统一网格（当前配置）

φ 网格与 CST 场导出步长**同一个数**：`design_region.grid_step_mm`（现为
0.1 mm，`CaseConfig.field_export_step_mm` 自动跟随），但**两者不要求对齐**
——导出网格的原点由 CST 包围盒定（实测 x −5.6 / y −3.55，设计区 y=−2.6
相对它差半格），旧 `nodes` 方案要求采样点与导出节点逐点重合，在服务器上
结构性做不到，已删除。

现在（`sampling.scheme: intersection`）每轮：

1. 边界 = **marching 在网格线上的交点**（两端节点 φ 线性插值，亚格点），
   它同时是交给 CST 重建的几何和做灵敏度分析的采样点；
2. 每个交点处用**加权最小二乘**从周围**介质侧**节点（`sample_side`，且不在
   固定金属里）拟合出 E/H（`wls_order` 阶、半径 `wls_radius_cells` 格），
   4 个场共用同一套插值算子；
3. δp 换算为速度后**反距离平方回写**到最近节点 + 四邻居（`scatter_max_cells`
   格内），再延拓到窄带（`level_set.extension_method`）。

一轮最大位移 = `optimizer.step_cells` 格（1 格 = 0.1 mm）。每轮会打印一行
诊断：`[采样] 交点 N 个（WLS 1 阶 R=2.0 格，回退 K），种子节点 M 个，延拓
skfmm|pde`——**K 应该为 0**（有回退 = 邻域太稀，把 `wls_radius_cells` 调大）；
**延拓应为 skfmm**（服务器装得上；打印 pde = 没装 scikit-fmm）。

跑起来要盯的三件事：

1. **导出体积**：0.2 → 0.1 使导出点数 ×7.7（ASCIIExport 没有范围属性，导的是
   整个包围盒）——四份场文件从 29 MB 涨到约 225 MB/份，`<output.dir>/cst_work/`
   峰值约 0.9 GB（每轮覆写，不累积）。解析本身很快（本地实测 29 MB / 0.36 s，
   约 0.1 mm 每轮 11 s），CST 侧导出时间按点数线性增长，**首次跑先看一轮的
   耗时**，明显拖慢再回来讨论裁体积。真要调粗导出步长（如 0.2），必须同时把
   `sampling.wls_radius_cells` 提到两倍（4.0），否则邻域相对变小、回退数上升。
2. **采样面**：`sampling.field_z_mm = -0.1`（金属下方介质里），启动时会打印
   一行"吸附到导出网格面 z=-0.0985"。**绝不能填 0.0**：0.1 mm 导出网格在
   z=+0.0015 有面、正好在 35 µm 金属体内，PEC 里 E/H≈0 → δp 恒 0、优化静默
   空转（pipeline 现在会直接报错拦住）。
3. **网格整除**：设计区尺寸必须被 `grid_step_mm` 整除（12/0.1=120 ✓、
   3.5/0.1=35 ✓），否则 `LevelSet2D` 的 linspace 会悄悄拉伸格距、所有按格数
   换算的量（WLS 半径、回写半径、步长格数）都跟着偏；`CaseConfig.validate()`
   会拦。**导出网格不再需要与设计区对齐**。

**近金属场是本地验不了的**（六面体网格在 PEC 界面阶梯化，贴界面节点的场有
O(网格) 级误差，WLS 单侧拟合会把它带进 δp）：首次正式运行建议先 `scheme:
contour` 跑一轮做 A/B（同一份场数据下的 δp 空间相关与 FoM 曲线），再切回
`intersection`。δp 若沿边界出现格距尺度的锯齿，先试 `wls_order: 2`，再试
`wls_radius_cells: 3.0`，最后才把 `intersection_offset_mm` 调到 0.05（半格，
把单侧外推变成内插、对阶梯噪声更鲁棒）。

## 3.6 第二个算例：不等分 Wilkinson 功分器（论文 III-B）

**脚本一模一样，只换 YAML**——算例由 `cfg.name` 分发（`eaopt/solver/case.py`
的 `load_case`：未知名字直接 `ValueError`，不会悄悄跑成耦合器）。`run_coupler.py`
这个文件名只对耦合器贴切，它本身能跑任意算例（改名留作后续）：

```bash
python scripts/cst_init_fwd.py configs/divider.yaml    # 端口 1 激励（输入）
python scripts/cst_init_bwd.py configs/divider.yaml    # 端口 2 激励（上输出）
python scripts/run_coupler.py  configs/divider.yaml    # 主循环（工程已存在就直接跑）
python scripts/plot_layout.py  configs/divider.yaml    # 重画布局图（本地，不连 CST）
```

产物与第 3 节完全一致，只是目录变成 `results/divider/`。几何核对用
`docs/layout_reference_divider.png`（与论文 Fig. 9 逐项对照：Y 形设计区、
框、三条馈线、三个端口面）。

> **模板几何只在建工程时写一次。** `run_coupler.py` 发现工程不存在会自动
> 初始化（`_ensure_projects`），而每轮 `build_model` **只重建设计区**，固定
> 馈线永远保持建工程时的样子。所以模板几何改过之后（例如修复"初始建模就
> 断裂"时把馈线伸进设计区 0.5 mm），**必须先删掉旧工程**再按上面两条
> `cst_init` 重建——否则跑的还是旧几何，而且不报错。
> 上一次失败的那次运行已经走到 `build_model`，说明工程当时就已经建出来了
> （旧几何），删掉重来。

**先核对这三条**（数值对不上时按此顺序查）：

1. **端口**：1 在板左边缘（xmin 面，输入）、2/3 在板上下边缘（ymax/ymin 面，
   上/下输出）——都是微带端口，面下缘贴合接地板底面；
2. **材料**：Rogers3003、30 mil（0.762 mm）、εr = 3.0、tanδ = 0.001、35 µm
   金属（论文 III-B；与耦合器的 Rogers4350B 不同，别拿旧工程对照）；
3. **设计区**：`[6.0, 33.7] × [−9, 9]` mm，0.1 mm 网格（277×180）；框内那条
   Y 形（λg/2 输入段 + 两根 λg/4 分路臂）整体可动。三条固定馈线**伸进框内
   0.5 mm**（`cst_model_divider.FEED_OVERLAP`）——与 Y 形重叠成一块，看图时
   金属应当是**连续**的：只贴着框边界相触的话 CST 里是两个独立实体，
   就是"初始建模就断裂"那个样子。

**Stage 1 判据（几何/端口/材料的判定关口）**：`iter_000/` 里 5 GHz 处
**|S21| ≈ |S31| ≈ −3.5 ± 1 dB**（论文初始 −3.49/−3.49）、|S11| ≤ −10 dB。
差得多基本就是上面三条里有一条不对。场文件约 **53 MB/份**（导出步长
XY=0.2 / Z=0.1，在 `cst_setup.DIVIDER` 的 `export_step_mm` /
`export_step_z_mm`，每轮 4 份 ≈ 210 MB，`cst_work/` 每轮覆写不累积）。

**Stage 2（先跑 5–8 轮）**：与耦合器同样的两件事——盯 `history.jsonl` 里
`fom` 的走向（这就是第 4 节 `velocity_sign` 的裁决机会），以及看
`[约束]` 打印了什么（下一段）。方向反了就改 `velocity_sign` 重跑。

**Stage 3 判据（跑满 50 轮）**：|S21| 从 −3.5 dB 升到 **≥ −1 dB**、|S31| 降到
**≤ −10 dB**（功率比 ≥ 9:1），形状出现"细颈 + 上臂鼓包"，趋势与论文 Fig. 11
一致（不追求逐点吻合：臂长取的是原文文字的 λg/4，论文图上量出来是 14.2，
两者自相矛盾；板子也比论文的紧凑）。

### 连通性约束（本算例特有）

论文对 III-B 只加了一条几何约束：**"the microstrip lines are always
connected"**（两输出必须同相）。`configs/divider.yaml` 里
`constraints.require_connected: true` 把它变成硬保证：每个 HJ 子步后检查
可动金属是否仍是**一块**、且贴着初始那三个"穿出设计区"的锚点（左界输入段
+ 右界两个臂端，从初始 φ 自动推导，不在 YAML 里写）；被掐断就按最短路径
桥接，**桥接节点永久冻结**（速度置 0），补不上则整子步回滚。

启动时会打印一行 `[约束] 连通性已开：锚点 …`（三个锚点）。运行中可能出现：

- `[约束] 连通性破坏 → 桥接（最短路径 x.xx mm），冻结 N 节点`——正常运作。
  **冻结点数是"补丁有多大"的度量**：偶尔几次无所谓，持续出现说明速度场在
  反复撕裂结构。
- `[约束] 警告：冻结节点已达设计区的 5%…`——补丁开始接管演化，那时该回头调
  `optimizer.step_cells`（步长太大）或改用"最小颈宽投影"，别让它一路打补丁
  到收敛。
- `优化结束（connectivity）`——连续 3 个子步连桥接都补不上（只有"金属被整块
  抹掉"会这样），直接停了。这是保护：断掉的中间态继续演化等于在优化另一个
  器件。

**`constraints.min_gap_mm` 必须保持 0**：三条馈线都伸进框内 0.5 mm，非零
min_gap 会在框内（x ≈ [6.5, 6.7] 与 [33.0, 33.2]）铺一条禁区带，横着把可动
金属削断（CST 照样出 S 参数，结果是另一个器件的）——
`tests/test_cst_model_divider.py::test_config_min_gap_must_stay_zero` 把这个
后果锁成了测试。同理 `taper_edges: x` 只 taper 左右两条边（可动金属在那里
穿出去接馈线）；上下边是臂自己的生长边界，taper 不得开。

## 4. 一等 TODO：`velocity_sign` 尚未裁决

形状导数 → 速度的符号（论文式 (24)/(31) 之间有符号矛盾）目前只能靠有限
差分验证定夺，而 FD 工具已随本地玩具模型一并删除，**CST 端还没有复核过**。
缺省 `velocity_sign: 1.0`。符号错的表现是 FoM **反向跑**（下降）且不报错，
所以第一次正式运行必须小步数盯 `history.jsonl`：若 FoM 单调下降，把它改成
`-1.0` 再跑。定论之后应补一个基于 CST 的 FD 校验脚本（对比单轮形状导数
与"扰动边界后重算 FoM"的差商）。

## 5. 断点续跑

`run_coupler.py --resume` **尚未实现**（功分器同理，把下面的 YAML 换成
`configs/divider.yaml` 即可）。当前的容错方式是：每轮都存盘 +
`iter_NNN/` 完整落盘，中断后可以
① `python scripts/cst_update.py configs/coupler.yaml --from-ls`（用最近一轮
φ 快照手工推一轮），或 ② 直接重跑 `run_coupler.py`（工程还在，会从第 0 轮
重新迭代，旧的 `iter_NNN/` 被覆盖）。

## 常见问题

- **`import cst.interface` 失败（脚本顶层就退出）**：按提示逐条排查——
  解释器是不是 3.6–3.11（官方库不支持更新版本）、`pip install ...
  cst-studio-suite-link` 装过没有、仓库根有没有 `cst/` 文件夹遮蔽官方包。
  **没有 `--lib-dir`**：顶层 import 发生在命令行解析之前，运行期再指定库
  路径不可能生效；让解释器找到它只有两条路——CST 安装时写入的 `.pth`
  （`python -c "import sys; print(sys.path)"` 里应能看到
  `...\AMD64\python_cst_libraries`），或设 `PYTHONPATH` 指向它。
- **打开的工程是个空壳（导航树光秃秃、没几何、没端口、没结果）**：先看
  History List 是不是空的——空的就是建工程时模型没写进去（第 1 节判据 1）；
  历史表有记录但导航树没东西，则多半是工程只存了 `.cst`、同名文件夹没跟
  着搬（CST 工程是**两件东西**）。
- **`zipfile.BadZipFile` / "读不出任何成员"，但工程打开有几何**：这个
  诊断**不是判定标准**。CST 这个版本的 `.cst` 常常既没有 zip 本地文件头、
  也没有可用中央目录（0.04 MB 的私有容器），解析失败**不代表**工程是空
  的——几何完全可能在**同名文件夹**里。
- **`Profile is self-intersecting, please check (.Create)`（`(&H8000ffff)`）**：
  设计区多边形自己穿过了自己。**这条路现在被 Python 侧拦住了**：
  `close_open_contours` 闭合开放轮廓后会自查自交，抛出的 `ValueError` 会
  指出是哪两条边、交在哪（CST 只回一句 "Profile is self-intersecting"）。
  2026-10-04 修过一次实测案例：两条开放轮廓**走向相反**（提取器不保证
  走向）时，闭合路径会从条带内部斜穿过去——现在先按"末端对末端、首端对
  首端"摆正走向，再沿外扩 box 周长走**较短的一侧**闭合，并且闭合折线必须
  带上终点所在边的角点（否则最后一段会斜切）。若升级后仍看到 CST 这句
  诊断，说明多边形不是从这里出去的（例如 `--shape` 直接给了自交形状）。
- **`The ASCII export option is not available for the current view.`
  （`ASCIIExport.Execute` 时报）**：**不是**"没有结果"，而是"当前视图不是
  可导出的场结果"。代码现在三层防住它：① 导出前到结果树上按叶子名认领
  条目（不再硬拼路径）；② 树上找不到就报错并列出树里全部条目；③
  `SelectTreeItem` 返回 False（条目不存在）当场抛。三层都过了仍看到这句，
  按可能性排查：该监视器还没有结果数据（求解没跑完/没存盘）、CST 处于
  **网格视图**或 2D 标量图视图（切回 3D 结果视图再试）、工程窗口未激活
  （被别的工程/对话框挡住）。2026-10-04 服务器第一次跑就是①：拼出来的
  ` [AC]` 后缀与真实条目名对不上，`SelectTreeItem` 静默失效（不报错），
  到 Execute 才以这句收场。
- **`add_to_history` 抛 `RuntimeError`**：官方库会把 CST 的诊断原样抛出
  （哪一块、什么错都在里面），把原始报错贴回开发者即可。历史上 CST 2024
  报过的两类设置错误：`(10090) ActiveX Automation error`（`Excitation`
  对象——模板已不用它）与 `(10097) wrong number of parameters`（`SaveAs`
  参数个数——`prj.save()` 已做成两段式：先按文档的
  `save(path, allow_overwrite=True)`，形参不存在（`TypeError`）再退回
  `save(path)`）。
- **脚本卡住不动、CST 弹窗问"Existing MWS result need to be deleted and
  re-simulated ... Do you want to proceed?"**：这是 CST 在**已有结果**的工程
  上重跑仿真时的确认框，`run_solver()` 是同步的，脚本就停在那一行等人点。
  现在有两道闸（都在 `eaopt/solver/cst_setup.py` 的 `CstSetup` 上，缺省都开）：
  1. `quiet_mode=True`：一连上就 `DesignEnvironment.set_quiet_mode(True)`，
     模态框不再弹（真库有这个方法的版本才有用；没有时日志里一行
     `[ -- ] 该 CST 版本没有 set_quiet_mode()`，然后靠第 2 条）。
     **这是会话级状态**：`--attach` 时你自己的 GUI 会话也被静默，要恢复
     只能重开 CST。
  2. `clear_results=True`：每轮改形状**之前**先 `DeleteResults`（走
     `_execute_vba_code` 控制宏，不进 History List，所以"每轮恰好一条历史
     记录"的验收判据不受影响）。旧结果不存在，框就无从弹起。
  两道都失效（例如 CST 不认 `DeleteResults`）时日志里会出现
  `[ -- ] ... 求解时 CST 可能弹确认框`，那时按下一段的老办法手工兜底。
  **想手工确认正确 VBA**：在 GUI 里做一次那步操作（Edit → History List →
  选到那一条 → 右下 Macro 按钮生成 VBA），比查文档可靠——`DeleteResults`
  这个命令名就是这么从 py4cst（照官方库自动生成）核出来的。
- **`Invalid stimulation port, please specify.`（Solver.Start 时报）**：
  两种原因，报错文本能区分——
  - 带 **`please specify a positive integer value or "All"`** ⇒ 值格式错
    （`"Port 1"` 这种就报这个）；
  - **不带**这半句 ⇒ 格式对（`"1"` 是数字字符串）但**该端口不存在**，
    到 Modeling 树里数一下端口。
  另外 `.StimulationPort "1"` 配 `.StimulationMode "All"` 也会炸：**端口
  与模式必须成对**（`.StimulationPort "1"` + `.StimulationMode "1"`，单模
  端口）。建工程时不报错、存盘也正常，只在求解时炸。
- **端口报错（如 "port is too small"）**：把端口面横向余量调大
  （`eaopt/solver/cst_model.py` 的 `_ports()` 中 `m`），重新建工程。
- **结果怎么读（CST 2024 实测）**：走**磁盘**结果文件，不用活工程的
  ResultTree（`ResultTree.GetResultItem(...)` / `GetAllItems()` 在这个版本
  根本不存在，报 `<unknown>.xxx`）：

  ```python
  import cst.results
  rm = cst.results.ProjectFile(str(工程路径), allow_interactive=True).get_3d()
  item = rm.get_result_item("1D Results\\S-Parameters\\S3,1")
  item.get_xdata(), item.get_ydata()
  ```

  实现在 `eaopt/solver/cst_results.py`（`read_s_params`）。**三个容易踩的
  点**：① 读的是**磁盘**结果——必须先 `prj.save()`，"求解→存盘→读"这个
  顺序反了会一直读到上一轮的值且不报错；② 条目路径不要硬拼后缀（CST 会
  给结果条目自动加 `[AC]` 之类），全路径读不到时按**叶子名**再试一次是
  唯一的兜底；③ 场条目**导出前到活结果树上按叶子名认领**（
  `cst_results.resolve_field_item`：`ResultTree.GetFirstChildName` /
  `GetNextItemName` 枚举 2D/3D Results 子树，先完全同名、再同名前缀——
  兼容 ` [AC]`/`[run N]` 后缀；树上没有就报错并列出树里的全部条目）。
  选中那一步还要看 `SelectTreeItem` 的**布尔返回值**（False = 树上没有
  这条，真库不抛错、静默失效）；枚举不可用时退回
  `cst_model.field_result_path()` 的惯例路径并告警。监视器名仍是共同源头
  （创建与认领都用 `field_monitor_name`，有测试锁定）。读不到一律抛错并把
  **原始错误**写进诊断——绝不返回 0（伴随法对场是线性的，一个 0 会静默
  污染整个梯度）。
- **ASCIIExport 的属性集（CST 2024 实测）**：只有 `Reset` / `FileName` /
  `Mode` / `StepX` / `StepY` / `StepZ` / `Execute`——**没有
  XStart/XEnd/YStart/YEnd/ZStart/ZEnd**（报 `<unknown>.XStart`）。所以
  导出范围 = 选中结果的整个包围盒（Volume 监视器 ⇒ 整个计算域），要限制
  范围只能在解析端裁剪（`cst_results.export_field_cropped`）。属性清单是
  `cst_model.ascii_export_params()` 一份事实来源。
- **导出的 ASCII 文件长什么样（`Mode "FixedWidth"`，CAT 实测原样）**：

  ```
             x [mm]           y [mm]           z [mm]       ExRe [V/m]       ExIm [V/m]  ...
  ------------------------------------------------------------------------------------------
                   -5.6             -6.9          -0.6985    1.0364752e-07    -5.4345449e-08  ...
  ```

  即**表头一行（列名）+ 分隔线一行 + 每个点一行、9 列**
  `x y z Re1 Im1 Re2 Im2 Re3 Im3`（mm；Re/Im 按分量成对；E 场 V/m、
  H 场 A/m）。点序 x 变最快，但解析端**按坐标归位、不依赖行序**；坐标轴
  由文件反推（不是我们给的 StepX/Y/Z 起点——实测网格与包围盒对齐、略有
  出入，例如 z 从 −0.6985 到 4.1015）。实测规模：耦合器 E 场
  117×64×25 = 187200 点 ≈ 13 MB、解析 0.4 s。
  **列数不是 9 一律报错**（`parse_ascii_field`）——错列的场会静默污染
  伴随梯度，宁可不解析。
- **端口相关（CST 2024 实测结论，改模板时勿违反）**：
  `.Coordinates` 只认 `"Free"/"Full"/"Picks"`（写 `"Ranges"` 报
  "Invalid coordinate type"）；`.Orientation` 只认**边界面名**
  `"xmin"/"xmax"/"ymin"/"ymax"`；微带类端口必须 `"Free"` + 显式
  Xrange/Yrange/Zrange，且端口面下缘要贴合接地板底面。
- **任何 GUI 设置不知道怎么写成 VBA**：在 GUI 里手工做那一步（比如 Time
  Domain Solver 对话框里选端口），然后 **Edit → History List**，选中刚
  出现的行 → 点 **Macro** 按钮 → 生成对应 VBA → 原样贴回。这比查文档
  可靠（CST 各版本命令名有出入）。模板里的 VBA 已与录制结果逐行对齐
  （Port 的 `XrangeAdd/SingleEnded/WaveguideMonitor`、Monitor 的
  `UseSubvolume`、Solver 的 `CalculateModesOnly`/`SParaSymmetry`/… 与
  独立的 `Mesh.SetCreator "High Frequency"`，`SteadyStateLimit` 取该版本
  默认 −40 dB）。
