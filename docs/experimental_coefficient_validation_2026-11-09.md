# 实验复数系数与本地 3 轮仿真（2026-11-09）

本页保留出射功率归一化系数的历史结果。当前代码已恢复使用 `-j*omega*conj(S)/|S|`。
本页仿真使用磁场项系数为 `j*omega*mu` 的旧形状核；当前磁场项系数已改为 `mu`，尚未重跑仿真。
该系数此前的 3 轮实验见
[-jω 系数验证记录](experimental_minus_jomega_validation_2026-11-09.md)。

实验系数已改为

\[
c_{\mathrm{exp}}=\frac{1}{4\sqrt{P_m}}\frac{\overline{S_{mn}}}{|S_{mn}|},
\qquad P_m=|S_{mn}|^2P_{\mathrm{in}}.
\]

`Pm` 为正向激励下目标端口的出射波功率，单位 W；对天线 S11 目标即反射波功率。
`Pin` 使用当前正向 `Solution.pin`，本次 CST 实测均为 0.5 W。
每次前向求解后重新计算 `Pm`，不把端口激励功率 0.5 W 直接当成 `Pm`。

`eaopt/runner.py` 的直接取场和网格插值两条单频路径共用 `_source_amplitude`，
将该系数乘入反向 E/H 后再调用 `shape_derivative_mine` 取实部。
目标函数仍是原来的 S 参数幅值；`--adjoint-source unit` 和双频链式权重保持各自原有计算。
`Pm <= 0` 或非有限时显式报错，避免未定义系数进入梯度。
`cst_evaluations.jsonl` 记录入射功率、出射功率和实际复数系数。

## 验证范围

使用 `.venv/python.exe`，单频 FD、`cst-points` 采样、`objective` 实验系数。
三个案例均提交 **3 次更新**、完成 **4 次前向评估**，终止原因为 `max_iterations`。
每个案例均保存初始状态及三轮更新的场图、水平集、S 参数和最终形状。

本地配置位于 `results/experimental_coefficient_2026-11-09/{case}.yaml`，
其中 `optimizer.max_iterations=3`；命令行也显式传入 `--iterations 3`。
耦合器沿用此前小步长验证的 `pseudo_time_step=1e-10`；功分器和天线为 `1e-8`。
仓库默认优化配置未调整。

耦合器首次双线程运行在第一轮候选结构的前向求解中发生 CST 预条件器内存不足，
尚未提交更新。日志最低可用物理内存约 0.97 GiB，失败工程保存在 `coupler_live/`。
随后用结果目录中的 `run_coupler_serial.py` 将本次 CST 最大 CPU 线程数设为 1，
以原网格和求解精度完成重试；设置记录于 `coupler_serial_retry/local_solver_settings.json`。
功分器和天线使用默认双线程。

## 逐轮结果

下表均为线性幅值。

| 案例 / 目标 | 初始 | 第 1 轮后 | 第 2 轮后 | 第 3 轮后 | 最终相对变化 |
|---|---:|---:|---:|---:|---:|
| 耦合器，最大化 S31 幅值 | 0.09227972 | 0.09249504 | 0.08714458 | 0.08570140 | -7.13% |
| 功分器，最大化 S21 幅值 | 0.66424572 | 0.67507978 | 0.64554243 | 0.64971864 | -2.19% |
| 天线，最小化 S11 幅值 | 0.89306296 | 0.86518409 | 0.86518409 | 0.86518413 | -3.12% |

最终 dB 值分别为 -21.34024、-3.74549、-1.25783 dB。
耦合器与功分器第一轮改善，三轮后的净结果均未改善。
天线后两轮完整 `phi` 数组与前一轮逐点相同；约 `4.2e-8` 的幅值变化来自重复求解差异，
不能视为新的几何优化进展。

四个状态的完整金属连通分量数：耦合器始终为 2（独立直通线与耦合臂/馈线），
功分器和天线始终为 1。耦合臂与左右固定馈线的距离始终为 0，未出现此前大步长的断接。

## 产物核对与复现

运行检查脚本：

```powershell
.\.venv\python.exe scripts/check_shape_derivative_mine.py
.\.venv\python.exe results/experimental_coefficient_2026-11-09/check_live_runs.py
```

9 项解析检查通过，覆盖原形状核、投影、复数相位、出射功率缩放、非法功率、单位源，
并验证网格插值路径在取实部前应用新系数。
保存产物检查确认每例 3 次提交、4 次前向评估、4 组状态/场图，以及最终水平集一致。
逐轮核对 `Pm=|S|^2*Pin`、实际复数系数和独立显式投影的边界场公式；
公式比较均通过 `rtol=1e-10, atol=1e-7`，最大绝对浮点差约 `1.19e-7`。
语法、双频入口导入和 `git diff --check` 也通过。

成功输出目录均位于 `results/experimental_coefficient_2026-11-09/`：

- `coupler_serial_retry/`
- `divider_live/`
- `antenna_live/`

机器可读结果为 `live_validation.json` 和 `local_smoke_status.json`，包含逐轮功率、复数系数、
目标值、预测位移和几何检查结果。复现命令如下；非空输出目录会自动创建新子目录。

```powershell
.\.venv\python.exe results/experimental_coefficient_2026-11-09/run_coupler_serial.py results/experimental_coefficient_2026-11-09/coupler.yaml --iterations 3 --output-dir results/experimental_coefficient_2026-11-09/coupler_serial_retry
.\.venv\python.exe scripts/run_divider_adjoint_levelset.py results/experimental_coefficient_2026-11-09/divider.yaml --iterations 3 --output-dir results/experimental_coefficient_2026-11-09/divider_live
.\.venv\python.exe scripts/run_antenna_adjoint_levelset.py results/experimental_coefficient_2026-11-09/antenna.yaml --iterations 3 --output-dir results/experimental_coefficient_2026-11-09/antenna_live
```

以上确认新系数进入实际 CST 场采样和几何更新流程；未进行有限差分梯度校准与长程收敛验证。
