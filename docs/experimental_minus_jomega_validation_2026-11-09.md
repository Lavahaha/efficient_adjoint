# -jω 实验复数系数与本地 3 轮仿真（2026-11-09）

当前代码已恢复使用 `-j*omega*conj(S)/|S|`。
本页保留该系数此前完成的 3 轮仿真结果及小步长配置，仿真时磁场项系数为 `j*omega*mu`。
当前代码已将磁场项系数改为 `mu`；此后未重新运行仿真，本页结果对应旧磁场核。

实验复数系数已改为

\[
c_{\mathrm{exp}}=-j\omega\frac{\overline S}{|S|},\qquad \omega=2\pi f.
\]

`omega` 使用 rad/s：耦合器和功分器为 `3.1415926536e10`（5 GHz），
天线为 `4.7123889804e10`（7.5 GHz）。每次梯度计算使用当前正向 S 参数的相位。
`eaopt/runner.py` 的直接取场和网格插值路径共用 `_source_amplitude`，
将系数乘入反向 E/H 后再由 `shape_derivative_mine` 取实部。
该形状核原有的磁场项 `j*omega*mu` 继续保留，实验源系数只在反向场上应用一次。
目标仍为原来的 S 参数幅值；S 为零或非有限时显式报错。
端口功率继续作为诊断数据记录；当前实验系数中没有出射功率因子。

## 本地实验设置

三个案例均运行单频 FD、`cst-points` 采样、`--adjoint-source objective`，
每例 **3 次更新、4 次前向评估、4 组状态和场图**，终止原因为 `max_iterations`。
耦合器使用单 CPU 线程；功分器和天线使用默认双线程。网格和求解精度沿用原设置。

新系数的模为 `omega`，直接沿用旧 `pseudo_time_step=1e-8` 会产生过大的位移与 CFL 子步数。
本次用上一次实验保存的真实初始 CST 边界场，代入新系数并延拓为速度场，
按 `pseudo_time_step = 0.02 / max(abs(V_initial))` 选择局部配置的固定步长。
该过程只选择实验步长，没有额外缩放源系数。

| 案例 | 固定伪时间步 | 预估初始最大速度 | 初始预测位移 |
|---|---:|---:|---:|
| 耦合器 | 9.779533540483913e-21 | 2.045087315995887e18 | 0.02 mm |
| 功分器 | 5.724481038573951e-20 | 3.493766485596095e17 | 0.02 mm |
| 天线 | 9.229327862306646e-20 | 2.167005040711761e17 | 0.02 mm |

三个本地 YAML 的 `max_iterations` 均为 3，命令行也显式传入 `--iterations 3`。
步长配置、选择依据和原场来源保存在
`results/experimental_minus_jomega_2026-11-09/{case}.yaml` 与 `step_selection.json`。
仓库原始 `configs/*.yaml` 的默认步长未修改；运行新系数时应使用本次的小步长配置或重新选择步长。

## 逐轮结果

下表为线性幅值。

| 案例 / 目标 | 初始 | 第 1 轮后 | 第 2 轮后 | 第 3 轮后 | 最终相对变化 |
|---|---:|---:|---:|---:|---:|
| 耦合器，最大化 S31 幅值 | 0.09227972 | 0.09261901 | 0.09152919 | 0.09073151 | -1.68% |
| 功分器，最大化 S21 幅值 | 0.66424563 | 0.68022352 | 0.68870523 | 0.68679143 | +3.39% |
| 天线，最小化 S11 幅值 | 0.89306297 | 0.89190844 | 0.89190842 | 0.89190845 | -0.13% |

最终 dB 值分别为 -20.84484、-3.26350、-0.99359 dB。
耦合器第一轮改善，三轮净结果未改善；功分器净改善但第三轮回落。
天线的改善集中在第一轮，后两轮完整 `phi` 数组逐点相同，幅值约 `1e-8` 的差异来自重复求解。

各轮实际预测最大位移（mm）：

- 耦合器：`0.02000, 0.01476, 0.01894`。
- 功分器：`0.02000, 0.91079, 1.08633`。
- 天线：`0.02000, 0.05369, 0.05369`。

初始场选出的固定步长未限制后续速度增长，功分器后两轮位移明显增大，尚不能认为步长已标定。
四个状态的完整金属连通分量数保持为：耦合器 2（独立直通线与耦合臂/馈线），
功分器和天线 1。耦合器到左右固定馈线的距离始终为 0；功分器及天线的馈接同样保持。

## 验证与产物

9 项解析检查通过，覆盖形状核投影、双线性乘积、频率项，以及新源系数的负虚数符号、
频率线性缩放、S 参数相位、功率独立性、非法 S、单位源和网格插值路径。
语法、双频入口导入检查通过。保存产物核对确认每例 3 次提交、4 次前向评估、
4 组场图/状态、有限场和水平集，以及最终水平集与状态 3 一致。

逐轮记录的复数系数与 `-1j*omega*conj(S)/abs(S)` 在 `rtol=1e-12` 下吻合，
系数模长与角频率一致。保存的边界 E/H 另经显式法向/切向投影核对形状核：
`max(abs(g-g_ref))/max(abs(g_ref))` 最大约 `3.35e-16`。
该相对误差适合本次 `1e17` 至 `1e19` 的灵敏度量级；它只核对公式实现和数值回放。

首次耦合器运行 `coupler_live/` 在初始前向求解时报
`Error during construction of pre-conditioner. Not enough memory.` 与端口模式计算失败，
未提交更新；CST 日志最低可用物理内存约 0.99 GiB。
在确认没有打开工程并重启空闲 CST 会话后，单线程重试 `coupler_retry/` 完成 3 轮。
失败与重试产物均保留。

成功结果均位于 `results/experimental_minus_jomega_2026-11-09/`：

- `coupler_retry/`
- `divider_live/`
- `antenna_live/`

机器可读汇总为 `live_validation.json` 与 `local_smoke_status.json`，包括逐轮系数、
功率、目标值、预测位移、金属网格变化与连通性。每个结果目录含
`fig7_sparams_vs_iteration.png`、`fig6b_iter_000.png` 至 `fig6b_iter_003.png` 和最终几何。

复现命令如下；非空输出目录会自动建立新子目录。

```powershell
.\.venv\python.exe results/experimental_minus_jomega_2026-11-09/run_coupler_serial.py results/experimental_minus_jomega_2026-11-09/coupler.yaml --iterations 3 --output-dir results/experimental_minus_jomega_2026-11-09/coupler_retry
.\.venv\python.exe scripts/run_divider_adjoint_levelset.py results/experimental_minus_jomega_2026-11-09/divider.yaml --iterations 3 --output-dir results/experimental_minus_jomega_2026-11-09/divider_live
.\.venv\python.exe scripts/run_antenna_adjoint_levelset.py results/experimental_minus_jomega_2026-11-09/antenna.yaml --iterations 3 --output-dir results/experimental_minus_jomega_2026-11-09/antenna_live
.\.venv\python.exe scripts/check_shape_derivative_mine.py
.\.venv\python.exe results/experimental_minus_jomega_2026-11-09/check_live_runs.py
```

此前出射功率归一化系数的结果保留在
[历史实验记录](experimental_coefficient_validation_2026-11-09.md)。
本次尚未进行有限差分梯度校准与长程收敛验证，目标改善不构成梯度物理正确性的证明。
