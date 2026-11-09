# eaopt：基于伴随法的微波器件形状优化

复现论文 *Efficient Adjoint-Based Shape Optimization Method for the Inverse Design
of Microwave Components*，IEEE TMTT 73(1), 494–503, 2025，
DOI: 10.1109/TMTT.2024.3421558。

所有优化案例统一使用根目录的 `adjoint_levelset.py`。`eaopt/runner.py` 提供
公共 CST 适配器与运行流程，各案例脚本只负责选择配置和优化方向。

## 安装

需要 Python 3.10 或更新版本；实际运行 CST 时还须满足所安装 CST 官方
Python 库的版本要求。本机 CST 2024 链接包要求 Python <3.11，使用 Python 3.10。

```bash
pip install -e .

# CST 官方库随 CST 安装包提供，不从 PyPI 下载；路径按实际安装目录修改。
pip install --no-index --find-links "E:\CST2024\Library\Python\repo\simple" cst-studio-suite-link
```

`scikit-fmm` 用于符号距离重初始化；未安装时回退到 SciPy 距离变换。
Python 3.14 环境默认跳过该依赖。仓库根目录不要创建 `cst/`，以免遮蔽官方库。
Shapely 用于金属多边形的差集、并集与孔洞处理；更新代码后重新执行 `pip install -e .` 安装新增依赖。

## 运行案例

从项目根目录执行：

| 案例 | 命令 | 优化目标 |
|---|---|---|
| 耦合器 | `python scripts/run_coupler_adjoint_levelset.py` | 最大化单频 \|S31\| |
| 功分器 | `python scripts/run_divider_adjoint_levelset.py` | 最大化单频 \|S21\| |
| 天线 | `python scripts/run_antenna_adjoint_levelset.py` | 最小化单频 \|S11\| |
| 双频功分器 | `python scripts/run_divider_multifreq.py` | 最大化 4/6 GHz 相对 −3 dB 门槛的 dB 余量 soft-min |

四个入口均调用 `eaopt.runner.main`，再使用 `adjoint_levelset.AdjointLevelSetOptimizer`。
旧入口 `scripts/run_coupler.py` 及旧的 `eaopt.pipeline` 已删除。

单频默认使用频域四面体求解和 CST 指定边界点取场（`--solver fd --boundary-sampling cst-points`）；
多频默认路径不变。单频 PEC 配置使用外侧偏移和非零采样高度，绘图只导出该高度的二维场。
实现、真实短程结果及尚未通过的网格精度检查见[综合测试分析报告](docs/analysis_summary_2026-11-02.md)。

```bash
# 指定小规模运行、独立输出目录和日志
python scripts/run_divider_adjoint_levelset.py --iterations 2 --output-dir results/divider_smoke --log results/divider_smoke.log

# 有限差分梯度检查：会额外运行 CST，随后继续优化
python scripts/run_divider_multifreq.py --gradient-check --iterations 1

# 查看完整命令参数；此操作无需导入 CST 官方库
python scripts/run_antenna_adjoint_levelset.py --help
```

工程缺失时自动初始化。默认创建无界面的 CST 实例；`--attach` 附接已运行实例，
`--new` 强制创建新实例。`--save-fields` / `--no-save-fields` 控制规则场快照保存，单频直接采样另存边界点场。
正向/反向工程同时保持打开，每轮异步启动两个求解器并等待完成；后续梯度计算复用本轮后向结果。
运行结束时保存并关闭本次工程。天线 S11 等同端口目标仍只运行前向仿真。
程序不支持断点续跑。输出目录非空时自动创建 `run_<UTC时间戳>/` 子目录，
保留此前的日志、快照和 CST 工程；控制台会打印本次实际输出目录。
默认保存 `run.log`；指定的 `--log` 文件已存在时，本次日志保存在新运行目录中。

## 优化流程与参数

每轮在同一几何上获得前向与伴随 E/H 场，沿金属轮廓法向计算形状灵敏度，
将边界速度延拓到网格后，通过 Hamilton–Jacobi 方程演化水平集。
金属内 φ < 0，外部 φ > 0，正法向速度使金属扩张。几何约束包括最小间距、
最小特征尺寸以及按案例启用的连通性修复。

水平集只定义在 `design_region.box` 内。`initial_metal` 定义完整初始金属：
区内部分由水平集演化，区外部分固定保留，每次重建取两者的几何并集，再加上 CST 模板的固定金属。
例如 coupler 的设计区 x 改为 `[0.5, 11.5]`，初始金属仍覆盖 `[0, 12]`，
两侧连接段自动保留。初始迭代直接使用原始多边形，避免网格化改变初始形状。
与区外初始金属或固定馈线接触的设计区边界保持初始水平集值；其他边界仍可生长到设计区边缘。
几何简化在区内拼合前完成，不移动区外固定金属；图和 `shape.json`/`final_shape.json` 展示并保存完整金属。

优化器每轮使用配置中的固定 `optimizer.pseudo_time_step`，按 CFL 划分演化子步。
每轮有效几何直接提交并记录，FoM 上升、下降或不变均不影响提交，也不改变时间步。
每次更新都重初始化符号距离；保留零速度、极小预测位移的停止条件及几何和数值校验。
`optimizer.max_iterations` 控制最大迭代次数。

三个单频案例使用 `shape_derivative_mine(boundary, Ef, Hf, Eb, Hb, omega, eps=EPS0, mu=MU0)`：
`Re[eps * E_perp · Eb_perp + mu * H_parallel · Hb_parallel]`。
输入场为 `(3, N)` 复数数组，点积不取共轭；`omega` 参数保留以兼容现有调用。
`eps` 取实际采样侧介电常数，`mu` 默认 `MU0`。直接取场和网格插值均调用该接口。
`--adjoint-source objective` 的实验伴随源权重为 `-1j*omega*conj(S)/|S|`，
其中 `omega=2*pi*f`，单位 rad/s；每轮按当前 S 参数重新计算。
源权重的模为 `omega`；形状核磁场点乘前的系数为 `mu`。
S 为零或非有限时会报错，避免未定义相位进入梯度；端口功率继续记录为诊断数据。
当前磁场核尚未重新进行 CST 仿真；下方历史报告中的步长对应旧磁场核，运行前需重新选择。
权重在取实部前乘入反向场；`--adjoint-source unit` 使用单位反向场。
新公式不乘原来的 `-2j*omega/Pin`；`--prefactor-scale` 与 `velocity_sign` 只整体缩放结果。
双频路径按各频点的角频率计算灵敏度，通过 dB 目标的复数链式因子和 soft-min
动态权重汇总原公式（25）的梯度；初始评估、迭代记录和有限差分检查均使用相同的双频目标。
双频目标的值以 dB 余量表示，不等于单个频点的 S 参数幅值。

YAML 中保留当前优化器实际使用的参数：

- `objective`：端口；双频还包括 `frequencies_ghz`、`target_db`、`beta_per_db`。
- `design_region`、`initial_metal`、`fixed_region`、`constraints`：初始几何与约束。
- `sampling`：`sample_side`、`intersection_offset_mm`、`field_z_mm`。
- `optimizer`：`pseudo_time_step`、`cfl`、`velocity_sign`、`max_iterations`。
- `level_set.extension_band_mm`：速度作用带宽。
- `output.dir`：输出根目录；运行前检查配置中的路径是否适用于当前机器。

PEC 默认在边界外侧一个网格处取场；可通过 `--sample-offset-mm` 显式覆盖。
CST 的材料、频率、场导出步长、端口、求解设置和工程路径规则由
`eaopt/solver/cst_setup.py` 与对应 `cst_model*.py` 管理。

## 项目结构

```text
adjoint_levelset.py       # 唯一优化核心、固定时间步水平集演化与梯度检查
eaopt/
  runner.py              # 公共案例运行器、单频/双频 CST 适配
  config.py              # YAML 配置与校验
  artifacts.py           # CST 求解产物读写
  plotting.py            # 布局、场与 S 参数绘图
  cli.py                 # 公共参数、日志和控制台输出
  adjoint/fields.py      # CST 导出复矢量场的网格容器
  geometry/              # 初始符号距离、轮廓与几何处理
  optimize/              # 几何约束与双频目标
  solver/                # CST 接口、结果读取和各案例建模
configs/                 # 耦合器、功分器、天线、双频功分器
scripts/                 # 案例入口、CST 手工操作及绘图工具
docs/server_runbook.md   # 服务器操作说明
```

旧的导数、边界采样、步长和速度延拓模块已删除；仍被运行器调用的场网格、
几何及约束代码保留。`tests/`、假 CST 库和 pytest 开发依赖已按要求移除。
原来只服务旧流程的 WLS、旧水平集演化与未使用辅助函数也已清理。

## 输出与绘图

默认写入 `<output.dir>/adjoint_levelset/`，`--output-dir` 可直接指定运行目录。

- `iter_NNN/`：一次 CST 求解评估的几何、S 参数与可选场数据；编号是评估编号，
  包括初始评估、每轮更新和梯度检查产生的求解，不等同于优化迭代编号。
- 每个 `iter_NNN/` 还保存输入水平集 `levelset.npz`、本次评估的
  `evaluation.json`（目标值、更新状态和固定时间步）、
  `evaluation_state.npz`（形状、前向采样面 E/H 和整板电场幅值）及 `evaluation.png`。
  FoM 下降或不变的更新也全部保存；有限差分检查单独编号。
  输入水平集在建模前保存，因此建模或求解失败时仍能定位候选形状。
- `optimization_evaluations.jsonl`：逐次评估记录，包含初始、每轮提交与梯度检查；
  每次求解完成即追加写入，每轮提交的 `accepted` 为 true，初始和梯度检查为 null。
- `adjoint_levelset_history.jsonl`：每次更新的目标变化、固定时间步及更新状态。
  为兼容旧记录读取代码，`backtracks` 保留为 0，`pseudo_time_step_next` 等于固定时间步。
- `cst_evaluations.jsonl`：CST 评估日志。
- `run_config.json`：本次生效的优化配置、CST 设置与命令行参数。
- `adjoint_levelset_state_NNN.npz`、`phi_before_iter_NNN.npz`、`phi_after_iter_NNN.npz`：状态快照。
- `fig6b_iter_NNN.png`、`fig7_sparams_vs_iteration.png`：布局/场图与 S 参数曲线；
  双频曲线带有频点标签，场图显示配置中的第一个目标频点。

### 绘制任意 z 平面的 xy 电场矢量图

`scripts/plot_efield_xy.py` 用 CST 导出的
`x y z ExRe ExIm EyRe EyIm EzRe EzIm` 文本文件绘制 xy 平面电场图：
底色和箭头颜色条表示 xy 平面复幅值
`|E_xy| = sqrt(|Ex|² + |Ey|²)`，箭头方向表示指定相位下的 `Ex/Ey`
瞬时方向，箭头长度按 `|E_xy|` 相对缩放。每个导出的 xy 网格点都会绘制
一个箭头；通过 `--z` 选择不同平面；脚本只把
选定平面读入内存，适合较大的 Volume 导出文件。

```bash
python scripts/plot_efield_xy.py --z 0
python scripts/plot_efield_xy.py --z -0.7485 --output docs/efield_z-0.7485.png --step 2
python scripts/plot_efield_xy.py --z 0 --phase-deg 90
python scripts/plot_efield_xy.py --z 0 --x 0.5 11.5 --y -2.6 0.9
```

`--z` 必须与文件中的导出平面一致；若需要查看可用 z 值，可根据 CST
导出文件中的第三列检查。`--step N` 可降低箭头密度，`--phase-deg` 用于
查看复场在其它相位的瞬时方向。`--x X_MIN X_MAX` 和
`--y Y_MIN Y_MAX` 指定绘制区域，例如 `--x 0.5 11.5 --y -2.6 0.9`；
只有落在区域内的原始网格点会绘制箭头，坐标轴范围仍显示为指定范围。标频点。
- `sparams_history.json` 及 `fig7_sparams_vs_iteration.png` 包含初始和每轮提交状态；
  `fig7_sparams_vs_evaluation.png` 展示全部评估，包括有限差分检查。
- `phi_final.npz`、`final_shape.json`、`adjoint_levelset_summary.json`：最终形状和运行摘要。

```bash
# 重绘统一优化器的结果
python scripts/plot_coupler_adjoint_levelset.py --help

# 重绘全部评估（含梯度检查），output-dir 使用本次实际运行目录
python scripts/plot_coupler_adjoint_levelset.py --output-dir results/coupler/adjoint_levelset --only evaluations

# 渲染模型布局参考图
python scripts/plot_layout.py configs/divider.yaml
```

`plot_results.py` 保留用于读取已有 `iter_NNN/` 格式的历史产物。
`cst_init_fwd.py`、`cst_init_bwd.py` 和 `cst_update.py` 保留为手工建模与求解工具。

## 验证

2026-11-08 清理了 `tmp/` 中已被替代的
`capture_divider_audit_logs.py`、`check_divider_frozen_baseline.py`、
`probe_fd_mesh.py` 和 `probe_pin_eq18.py`。当前报告引用的诊断脚本、原始求解数据
和公式（18）验证数据仍保留；不按文件日期直接判定过期。

```bash
# 无需 CST 的纯几何检查；模拟 CST/求解流程已移除
python scripts/check_design_geometry.py

# 新形状导数的解析场检查（无需 CST）
python scripts/check_shape_derivative_mine.py
```

`shape_derivative_mine` 的三个案例真实场回放和本机 CST 求解尝试见
[本地验证记录](docs/shape_derivative_mine_validation_2026-11-09.md)。
当前 `-j*omega` 实验系数此前完成的 3 轮仿真保留在
[-jω 系数验证记录](docs/experimental_minus_jomega_validation_2026-11-09.md)。
此前出射功率归一化系数的结果保留在
[历史实验记录](docs/experimental_coefficient_validation_2026-11-09.md)。

使用四个案例入口的 `--help` 检查参数和导入，使用 `--gradient-check` 在配置好
CST 的环境中检查目标变化与伴随梯度，并先做 1–2 轮小规模运行确认几何、场数据
和目标趋势。代码清理后的静态检查与离线数值检查不能代替实际 CST 验收。
