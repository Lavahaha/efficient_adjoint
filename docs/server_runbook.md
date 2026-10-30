# CST 服务器运行手册

## 0. 环境准备

安装项目及 CST 自带的官方 Python 库，解释器版本必须同时满足项目
Python ≥3.10 和所安装 CST 版本的支持范围。以下路径按服务器实际目录修改。

```bash
pip install -e .
pip install --no-index --find-links "D:\CST 2024\Library\Python\repo\simple" cst-studio-suite-link
python -c "import cst.interface; import cst.results; print(cst.interface.__file__)"
```

仓库根目录不要创建 `cst/`，否则可能遮蔽官方库。运行前检查 YAML 的
`output.dir`。非空输出目录会自动生成 `run_<UTC时间戳>/` 子目录，
以控制台打印的本次路径为准；旧日志、快照和 CST 工程不会被覆盖。当前不支持断点续跑。

## 1. 小规模验收

```bash
python scripts/run_coupler_adjoint_levelset.py --iterations 2 --output-dir results/coupler_smoke
python scripts/run_divider_adjoint_levelset.py --iterations 2 --output-dir results/divider_smoke
python scripts/run_antenna_adjoint_levelset.py --iterations 2 --output-dir results/antenna_smoke
python scripts/run_divider_multifreq.py --iterations 2 --output-dir results/divider_multifreq_smoke
```

四个入口都通过 `eaopt.runner` 调用 `adjoint_levelset.py`。默认创建无界面实例，
工程缺失时自动初始化。`--attach` 用于附接已运行的 GUI 实例。

检查模型 History List 非空，保存并关闭后重新打开 `.cst`，确认几何与端口仍在。
CST 工程包含 `.cst` 文件及其同名文件夹，复制工程时两者一起复制。
前向激励为目标输入端口，伴随激励为观测端口；天线 S11 使用同端口场。

检查运行目录的形状快照、场图与目标变化。只有目标改善才接受候选更新，
失败候选会减半伪时间步并回溯；CST 评估编号不等于已接受的优化迭代编号。
双频案例应同时包含 4 GHz、6 GHz 的 S 参数记录，目标值是 soft-min dB 余量。
每个 `iter_NNN/` 的 `evaluation.json`、`evaluation_state.npz` 和 `evaluation.png`
记录该次求解，包括被拒绝的候选；`optimization_evaluations.jsonl` 给出全部评估的
目标值、接受状态、回溯编号和时间步。恢复求解也单独记录，不覆盖已接受状态。
`run_config.json` 和 `run.log` 保存本次设置与运行日志。

## 2. 梯度检查

```bash
python scripts/run_divider_adjoint_levelset.py --gradient-check --iterations 1 --output-dir results/divider_gradient
python scripts/run_divider_multifreq.py --gradient-check --iterations 1 --output-dir results/divider_multifreq_gradient
```

该选项会先运行额外的中心有限差分求解，写入 `gradient_check.json`，然后继续优化。
比较伴随斜率与有限差分斜率的方向和量级。单频保留实部灵敏度约定，检查结果可
暴露它与幅值目标的偏差；双频采用 dB 链式因子及 soft-min 权重。
检查未通过时应先调查场、端口归一化、采样面和比例因子，不能仅凭回溯接受就
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

旧 pipeline、过期诊断脚本及 `tests/` 已移除。记录逻辑可用
`python scripts/check_evaluation_recording.py` 离线回归检查；真实计算仍须通过
有限差分检查和 CST 小规模验收。
