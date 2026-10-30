# eaopt：基于伴随法的微波器件形状优化

复现论文 *Efficient Adjoint-Based Shape Optimization Method for the Inverse Design
of Microwave Components*，IEEE TMTT 73(1), 494–503, 2025，
DOI: 10.1109/TMTT.2024.3421558。

所有优化案例统一使用根目录的 `adjoint_levelset.py`。`eaopt/runner.py` 提供
公共 CST 适配器与运行流程，各案例脚本只负责选择配置和优化方向。

## 安装

需要 Python 3.10 或更新版本；实际运行 CST 时还须满足所安装 CST 官方
Python 库的版本要求。

```bash
pip install -e .

# CST 官方库随 CST 安装包提供，不从 PyPI 下载；路径按实际安装目录修改。
pip install --no-index --find-links "D:\CST 2024\Library\Python\repo\simple" cst-studio-suite-link
```

`scikit-fmm` 用于符号距离重初始化；未安装时回退到 SciPy 距离变换。
Python 3.14 环境默认跳过该依赖。仓库根目录不要创建 `cst/`，以免遮蔽官方库。

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

```bash
# 指定小规模运行、独立输出目录和日志
python scripts/run_divider_adjoint_levelset.py --iterations 2 --output-dir results/divider_smoke --log results/divider_smoke.log

# 有限差分梯度检查：会额外运行 CST，随后继续优化
python scripts/run_divider_multifreq.py --gradient-check --iterations 1

# 查看完整命令参数；此操作无需导入 CST 官方库
python scripts/run_antenna_adjoint_levelset.py --help
```

工程缺失时自动初始化。默认创建无界面的 CST 实例；`--attach` 附接已运行实例，
`--new` 强制创建新实例。`--save-fields` / `--no-save-fields` 控制完整场快照保存。
程序不支持断点续跑。输出目录非空时自动创建 `run_<UTC时间戳>/` 子目录，
保留此前的日志、快照和 CST 工程；控制台会打印本次实际输出目录。
默认保存 `run.log`；指定的 `--log` 文件已存在时，本次日志保存在新运行目录中。

## 优化流程与参数

每轮在同一几何上获得前向与伴随 E/H 场，沿金属轮廓法向计算形状灵敏度，
将边界速度延拓到网格后，通过 Hamilton–Jacobi 方程演化水平集。
金属内 φ < 0，外部 φ > 0，正法向速度使金属扩张。几何约束包括最小间距、
最小特征尺寸以及按案例启用的连通性修复。

优化器使用 `optimizer.pseudo_time_step` 作为初始伪时间步，按 CFL 划分演化子步。
候选形状只有在目标改善时才接受，否则减半时间步并回溯；每次更新都重初始化
符号距离。`optimizer.max_iterations` 控制最大迭代次数。

单频路径保留原有的实部形状灵敏度约定，不额外乘 S 参数相位因子。
双频路径按各频点的角频率计算灵敏度，通过 dB 目标的复数链式因子和 soft-min
动态权重汇总梯度；初始评估、候选接受和有限差分检查均使用相同的双频目标。
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
adjoint_levelset.py       # 唯一优化核心、水平集演化、回溯与梯度检查
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
  包括回溯和梯度检查产生的求解，不等同于已接受的优化迭代编号。
- 每个 `iter_NNN/` 还保存输入水平集 `levelset.npz`、本次评估的
  `evaluation.json`（目标值、接受/拒绝原因、回溯次数和时间步）、
  `evaluation_state.npz`（形状、前向采样面 E/H 和整板电场幅值）及 `evaluation.png`。
  不改善目标的候选也全部保存；恢复求解和有限差分检查单独编号。
  输入水平集在建模前保存，因此建模或求解失败时仍能定位候选形状。
- `optimization_evaluations.jsonl`：逐次评估记录，包含初始、接受、拒绝、恢复与梯度检查；
  每次求解完成即追加写入，`accepted` 仅对优化候选为 true/false，其他类型为 null。
- `adjoint_levelset_history.jsonl`：每次更新的目标变化、时间步、回溯次数及接受状态。
- `cst_evaluations.jsonl`：CST 评估日志。
- `run_config.json`：本次生效的优化配置、CST 设置与命令行参数。
- `adjoint_levelset_state_NNN.npz`、`phi_before_iter_NNN.npz`、`phi_after_iter_NNN.npz`：状态快照。
- `fig6b_iter_NNN.png`、`fig7_sparams_vs_iteration.png`：布局/场图与 S 参数曲线；
  双频曲线带有频点标签，场图显示配置中的第一个目标频点。
- `sparams_history.json` 及 `fig7_sparams_vs_iteration.png` 只包含初始和已接受状态，
  恢复求解不会重写已接受状态的快照；`fig7_sparams_vs_evaluation.png` 展示全部评估。
- `phi_final.npz`、`final_shape.json`、`adjoint_levelset_summary.json`：最终形状和运行摘要。

```bash
# 重绘统一优化器的结果
python scripts/plot_coupler_adjoint_levelset.py --help

# 重绘所有候选（含拒绝/恢复/梯度检查），output-dir 使用本次实际运行目录
python scripts/plot_coupler_adjoint_levelset.py --output-dir results/coupler/adjoint_levelset --only evaluations

# 渲染模型布局参考图
python scripts/plot_layout.py configs/divider.yaml
```

`plot_results.py` 保留用于读取已有 `iter_NNN/` 格式的历史产物。
`cst_init_fwd.py`、`cst_init_bwd.py` 和 `cst_update.py` 保留为手工建模与求解工具。

## 验证

```bash
# 无需 CST 和 pytest 的记录逻辑回归检查
python scripts/check_evaluation_recording.py
```

使用四个案例入口的 `--help` 检查参数和导入，使用 `--gradient-check` 在配置好
CST 的环境中检查目标变化与伴随梯度，并先做 1–2 轮小规模运行确认几何、场数据
和目标趋势。代码清理后的静态检查与离线数值检查不能代替实际 CST 验收。
