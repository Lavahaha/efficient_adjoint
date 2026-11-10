# CST 服务器运行手册

单频入口当前默认 `--solver fd --boundary-sampling cst-points`；多频仍使用原有 TD 默认路径。
请使用新输出目录，旧 TD 工程不会自动转换为 FD。PEC 边界点要求外侧偏移和非零材料侧高度，
正反向实际四面体网格不一致时终止计算。详细命令、产物格式及未通过的网格精度检查见
[综合测试分析报告](analysis_summary_2026-11-02.md)。手工 `cst_init_*` 工具仍默认 TD，需要 FD 时显式传 `--solver fd`。

FD 启动前会检查求解器类型和激励。若服务器 CST 报 `Method or property not found`
且缺失的是 `FDSolver.GetStimulationPort/GetStimulationMode`，当前代码会改用
`FDSolver.Stimulation "<目标端口>", "1"` 显式设置激励并打印提示；支持读取接口的版本
仍严格校验端口与模式。连接错误、设置失败和工程类型不符仍会终止运行。
同步 `eaopt/solver/cst.py` 后，用新的 `--output-dir` 先跑 `--iterations 1` 验证。

## 0. 环境准备

安装项目及 CST 自带的官方 Python 库，解释器版本必须同时满足项目
Python ≥3.10 和所安装 CST 版本的支持范围。本机 `E:\CST2024` 的
`cst-studio-suite-link` 包声明 `>=3.7,<3.11`，因此使用 Python 3.10。
以下路径按实际安装目录修改。

```bash
pip install -e .
pip install --no-index --find-links "E:\CST2024\Library\Python\repo\simple" cst-studio-suite-link
python -c "import cst.interface; import cst.results; print(cst.interface.__file__)"
```

仓库根目录不要创建 `cst/`，否则可能遮蔽官方库。运行前检查 YAML 的
`output.dir`。非空输出目录会自动生成 `run_<UTC时间戳>/` 子目录，
以控制台打印的本次路径为准；旧日志、快照和 CST 工程不会被覆盖。当前不支持断点续跑。
新增 Shapely 几何依赖后须重新执行 `pip install -e .`。

本机项目内的 Python 3.10 解释器是 `.venv\python.exe`。在 PowerShell 中显式调用，
避免误用默认的 Python 3.14：

```powershell
.\.venv\python.exe -c "import sys, cst.interface, cst.results; print(sys.version); print(cst.interface.__file__)"
.\.venv\python.exe scripts/run_coupler_adjoint_levelset.py --iterations 2 --output-dir results/coupler_local_smoke
```

## 1. 小规模验收

```bash
python scripts/run_coupler_adjoint_levelset.py --iterations 2 --output-dir results/coupler_smoke
python scripts/run_divider_adjoint_levelset.py --iterations 2 --output-dir results/divider_smoke
python scripts/run_antenna_adjoint_levelset.py --iterations 2 --output-dir results/antenna_smoke
python scripts/run_divider_multifreq.py --iterations 2 --output-dir results/divider_multifreq_smoke
```

四个入口都通过 `eaopt.runner` 调用 `adjoint_levelset.py`。默认创建无界面实例，
工程缺失时自动初始化。`--attach` 用于附接已运行的 GUI 实例。
三个单频入口使用 `shape_derivative_mine`，公式为
`Re[eps*E_perp·Eb_perp + MU0*H_parallel·Hb_parallel]`，
保留当前伴随源权重与速度比例，不再乘公式（25）的 `-2j*omega/Pin`。
默认实验权重为 `-1j*omega*conj(S)/|S|`，其中 `omega=2*pi*f`，单位 rad/s；磁场点乘项只乘 `MU0`。
`cst_evaluations.jsonl` 记录角频率、实际复数系数及作为诊断数据的入射/出射功率。
该系数此前的 3 轮仿真见 [-jω 实验记录](experimental_minus_jomega_validation_2026-11-09.md)；
该记录中的磁场核含 `j*omega`，其中约 `1e-20` 至 `1e-19` 的实验步长需按当前磁场核重新选择。
`run_config.json` 的 `shape_derivative` 记录所用公式；双频仍为 `paper_eq25`。
正向/反向工程同时保持打开；每轮几何更新后，通过 CST 原生 `start_solver()`
异步启动两个求解器，再查询运行状态并等待两者完成。每个求解都必须返回 `SUCCESS`；
启动或运行失败时停止本次启动的其他求解，并抛出异常。服务器需有足够的并发求解许可和计算资源。
求解完成后依次读取、保存结果并核对正反向网格；后续梯度计算直接复用本轮后向结果，
不再重复求解。天线 S11 等同端口目标只运行前向仿真。
运行结束保存并关闭本次工程，CST 环境仍保留。需要查看时重新打开对应 `.cst`。

检查模型 History List 非空，保存并关闭后重新打开 `.cst`，确认几何与端口仍在。
CST 工程包含 `.cst` 文件及其同名文件夹，复制工程时两者一起复制。
前向激励为目标输入端口，伴随激励为观测端口；天线 S11 使用同端口场。
`initial_metal` 是完整初始金属，`design_region.box` 只限定水平集定义域与演化范围。
缩小设计区不会裁掉区外初始金属；重建和绘图均使用“区外初始金属 + 区内演化金属”。
改变设计区后检查完整初始形状与馈电连接，不必跟随红框修改初始金属顶点。

检查运行目录的形状快照、场图与目标变化。每轮使用配置中的固定伪时间步，
有效几何直接提交；FoM 下降或不变也会继续记录，不执行 FoM 筛选或回溯减步。
CFL 演化子步、零速度/极小预测位移停止条件及几何和数值校验继续生效。
CST 评估编号不等于优化迭代编号。
双频案例应同时包含 4 GHz、6 GHz 的 S 参数记录，目标值是 soft-min dB 余量。
每个 `iter_NNN/` 的 `evaluation.json`、`evaluation_state.npz` 和 `evaluation.png`
记录该次求解，包括 FoM 变差或不变的更新；`optimization_evaluations.jsonl` 给出全部评估的
目标值、提交状态和固定时间步。每轮更新各自留档，不进行拒绝后的恢复求解。
`run_config.json` 和 `run.log` 保存本次设置与运行日志。

## 2. 梯度检查

```bash
python scripts/run_divider_adjoint_levelset.py --gradient-check --iterations 1 --output-dir results/divider_gradient
python scripts/run_divider_multifreq.py --gradient-check --iterations 1 --output-dir results/divider_multifreq_gradient
```

该选项会先运行额外的中心有限差分求解，写入 `gradient_check.json`，然后继续优化。
比较伴随斜率与有限差分斜率的方向和量级。单频使用 `shape_derivative_mine`，检查结果可
暴露它与幅值目标的偏差；双频采用 dB 链式因子及 soft-min 权重。
检查未通过时应先调查场、端口归一化、采样面和比例因子，不能仅凭目标值改善就
认定伴随梯度正确。`--prefactor-scale` 用于显式调整比例与符号。

## 3. 正式运行与重绘

```bash
python scripts/run_divider_multifreq.py --output-dir results/divider_multifreq_run
python scripts/plot_coupler_adjoint_levelset.py --help
python scripts/plot_coupler_adjoint_levelset.py --output-dir <本次实际运行目录> --only evaluations
python scripts/plot_layout.py configs/divider.yaml
```

材料、端口、场导出步长与求解设置在 `eaopt/solver/cst_setup.py` 和
`cst_model*.py`；几何约束和优化参数在 YAML 中。布局代码变更后使用新输出目录，
避免复用按旧模型初始化的工程。

## 4. 手工 CST 工具

```bash
python scripts/cst_init_fwd.py configs/coupler.yaml
python scripts/cst_init_bwd.py configs/coupler.yaml
python scripts/cst_update.py --help
```

这些脚本用于分步建模、求解和排查问题。几何改动必须经 `model3d.add_to_history`，
结果读取前必须先保存工程；不要改监视器名称，导出工具按其名称定位场条目。
已有结果导致确认框阻塞时，检查 `quiet_mode`、`clear_results` 配置及 CST 许可状态。

旧 pipeline、过期诊断脚本、`tests/` 及模拟 CST/求解的检查已移除。
`python scripts/check_design_geometry.py` 仅检查纯几何：四个案例的区外金属保留、
连接边界和孔洞；CST 重建、运行记录与梯度仍须通过真实小规模运行验收。
