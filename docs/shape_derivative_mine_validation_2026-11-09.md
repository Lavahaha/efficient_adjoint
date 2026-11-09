# shape_derivative_mine 本地验证（2026-11-09）

本页记录磁场项系数为 `j*omega*mu` 的旧形状核。当前代码已去掉磁场项的 `j*omega`，
磁场项系数为 `mu`；本页仿真结果和回放检查对应旧核，当前核尚未重跑 CST 仿真。

释放内存后，三个单频案例均已完成默认 FD 路径的两次更新和三次前向评估。
新接口已进入真实 CST 场采样、水平集演化和几何重建流程。
默认耦合器出现右侧馈线断开、功分器目标下降，运行完成不能等同于优化质量验证通过。
另完成耦合器 `1e-10` 时间步两轮补测，馈线连接保持，S31 相对初始净增约 0.33%。

## 接口与案例接入

`adjoint_levelset.py` 新增：

```python
shape_derivative_mine(boundary, Ef, Hf, Eb, Hb, omega, eps=EPS0, mu=MU0)
```

返回每个边界点的实数数组：

\[
\delta p_i=\operatorname{Re}\left[
\epsilon E_{\perp,i}\cdot E^{\mathrm{back}}_{\perp,i}
+j\omega\mu_0\mathbf H_{\parallel,i}\cdot\mathbf H^{\mathrm{back}}_{\parallel,i}
\right].
\]

输入 E/H 为 `(3, N)` 复相量；`omega` 为 rad/s。法向量为现有水平集给出的金属外法向，
磁场切向包含平面内切向和 z 分量。两个点积均不对场取共轭。
新旧公式共用投影与场数组校验，原 `shape_derivative_from_fields` 和
`shape_derivative_complex` 接口保留。

`eaopt.runner` 的耦合器、单频功分器和天线在指定边界点取场、网格插值两条路径均使用新接口。
频率取各案例配置（耦合器/功分器 5 GHz，天线 7.5 GHz），介电常数取采样所在材料，
磁导率使用 `MU0`。默认沿用现有 `objective` 伴随源：`conj(S)/|S|` 在取实部前乘入反向场；
`--adjoint-source unit` 可使用原始单位反向场。
新公式没有 `-2j*omega/Pin`；`--prefactor-scale * velocity_sign` 仅整体缩放输出，
天线的最小化方向仍由优化器处理。双频案例继续使用原公式（25）。

## 不调用 CST 的检查

本机解释器为 `.venv\python.exe`，Python 3.10.11。

```powershell
.\.venv\python.exe scripts/check_shape_derivative_mine.py
.\.venv\python.exe scripts/check_design_geometry.py
.\.venv\python.exe -m py_compile adjoint_levelset.py eaopt/runner.py scripts/check_shape_derivative_mine.py
.\.venv\python.exe scripts/run_divider_multifreq.py --help
```

5 项解析场检查、7 项几何检查、语法和双频入口导入检查均通过。解析检查覆盖：
旋转边界法向、电场法向/磁场切向投影、磁场 z 分量、独立电/磁项、角频率系数、
双线性无共轭乘积、伴随源权重在取实部前应用、旧公式结果保持、非法频率/场数组和空边界。

## 三案例真实场回放

执行本地诊断脚本：

```powershell
.\.venv\python.exe results/shape_derivative_mine_2026-11-09/check_saved_fields.py
```

耦合器、功分器输入为 `results/fd_single_2026-11-02/{coupler,divider}_smoke/iter_000/`
保存的真实 CST FD 场；天线使用本次 `results/shape_derivative_mine_2026-11-09/antenna/iter_000/`
新求解的场。三个案例均按当前默认幅值目标施加伴随源权重。
每个案例独立显式投影 E/H，核对新接口，再运行一次速度延拓、HJ 演化、约束投影和金属几何重建。
三个案例的场/水平集均有限，几何重建成功，功分器连通性检查通过。

| 案例 | 活动取场点 | 最大绝对灵敏度 | 预测最大位移 mm | 改变的金属网格点 | 结果 |
|---|---:|---:|---:|---:|---|
| 耦合器 | 240 | 1.3416264e8 | 1.1493875 | 283 | 通过 |
| 功分器 | 795 | 5.4062094e6 | 0.0361223 | 268 | 通过 |
| 天线 | 301 | 5.1738968e6 | 0.0512339 | 286 | 通过 |

结果保存在 `results/shape_derivative_mine_2026-11-09/saved_field_replay.json`，
各案例的 `saved_field_replay/<case>/update.npz` 保存梯度、速度和更新前后水平集。
天线回放得到的完整候选 `phi` 与本次实际运行保存的 `antenna/iter_001/levelset.npz`
逐点一致（最大误差 0），确认新公式已进入实际几何更新路径。
回放没有求解更新后的电磁场，不能据此判断 FoM 改善或梯度的物理精度。
耦合器在默认 `1e-8` 伪时间步下预测最大位移约 1.15 mm，长程运行前需校准时间步。

## 首次 CST 求解尝试（内存不足）

使用当前配置，设置 `--iterations 2`，输出至独立目录
`results/shape_derivative_mine_2026-11-09/`。先串行运行默认 FD；耦合器额外尝试 TD。
运行日志和 `run_config.json` 均标记 `shape_derivative_mine`，没有覆盖历史工程。

| 案例/求解器 | 输出子目录 | 结果 |
|---|---|---|
| 耦合器 FD | `coupler/run_20261109T024126251266Z` | 初始前向端口模计算内存不足，未进入梯度计算 |
| 耦合器 TD | `coupler_td` | 初始前向求解报告预条件器内存不足，未进入梯度计算 |
| 功分器 FD | `divider` | 初始前向端口模计算内存不足，未进入梯度计算 |
| 天线 FD | `antenna` | 初始求解与新梯度计算完成；第一次候选几何求解内存不足，未提交更新 |

耦合器 FD 的 `cst/coupler_fwd/Result/Model.log` 记录物理内存总量 13.69 GB，
可用内存仅 432.02 MB，报错 `Error during construction of pre-conditioner. Not enough memory.`，
随后 `The port mode calculation failed.`。TD 重试也报告相同预条件器内存错误。
功分器 FD 的同类日志记录可用内存 941.21 MB，报相同端口模错误。
天线初始 `|S11| = 0.8930629633`，301 个边界点的场已导出，新公式成功生成候选几何；
候选求解报 `Error during construction of pre-conditioner. Not enough memory.` 和
`Could not compute preconditioner.`。因此没有新的更新后目标值或完整两轮记录。

释放内存后重跑命令（非空输出目录会自动生成新子目录）：

```powershell
.\.venv\python.exe scripts/run_coupler_adjoint_levelset.py --iterations 2 --output-dir results/shape_derivative_mine_2026-11-09/coupler
.\.venv\python.exe scripts/run_divider_adjoint_levelset.py --iterations 2 --output-dir results/shape_derivative_mine_2026-11-09/divider
.\.venv\python.exe scripts/run_antenna_adjoint_levelset.py --iterations 2 --output-dir results/shape_derivative_mine_2026-11-09/antenna
```

## 释放内存后的两轮复测

复测开始前可用物理内存约 3.46 GB。三个案例串行运行，均使用默认
`--solver fd --boundary-sampling cst-points --adjoint-source objective`，
`pseudo_time_step=1e-8`，`--prefactor-scale=1`，各提交两次更新，
保存初始状态和两次更新后的三组前向评估、场图、几何与 S 参数。
三次进程均正常退出，终止原因为 `max_iterations`。

| 案例/目标 | 初始值 | 第一轮后 | 第二轮后 | 净变化 |
|---|---:|---:|---:|---:|
| 耦合器，最大化 \|S31\| | 0.09227972 | 0.04681738 | 0.15786978 | +71.08% |
| 功分器，最大化 \|S21\| | 0.66424569 | 0.65585023 | 0.64502662 | −2.89% |
| 天线，最小化 \|S11\| | 0.89306296 | 0.86518411 | 0.86518411 | −3.12% |

对应 dB 值：耦合器 −20.69788 → −26.59186 → −16.03402；
功分器 −3.55343 → −3.66391 → −3.80845；天线 −0.98236 → −1.25783 → −1.25783。

结果目录均位于 `results/shape_derivative_mine_2026-11-09/`：

- `coupler_memory_retry/`
- `divider_memory_retry/`
- `antenna_memory_retry/`

### 观察到的限制

耦合器两轮预测最大位移分别约 1.149 mm、1.684 mm。末态耦合臂与右固定馈线间隙约
0.68874 mm，完整金属（含独立直通线）的连通分量从初始 2 个增加到 3 个。
最终 S31 数值上升发生在连接已经改变的结构上，不能据此判定原器件优化成功。

功分器两轮的 S21 均下降，虽然运行无报错，也不能判定新公式给出了该目标的上升方向。

天线第二轮与第一轮的完整 `phi` 数组逐点相同，金属网格点变化数为 0；
约 `1.66e-9` 的目标差别属于无几何变化的重复求解差异，不能视为新的优化进展。

### 耦合器小步长补测

使用独立本地配置 `results/shape_derivative_mine_2026-11-09/coupler_small_step.yaml`，
仅将伪时间步改为 `1e-10`，再运行两轮，检查默认大步长发现的连接问题。
仓库 `configs/coupler.yaml` 的默认步长仍为 `1e-8`。

补测正常退出，保存于 `results/shape_derivative_mine_2026-11-09/coupler_small_step/`。
S31 幅值为 `0.09227970 → 0.09406489 → 0.09258227`，相对初始净增约 0.33%，
仍不是单调改善。两轮预测最大位移分别为 0.01149 mm、0.00618 mm。
三种状态的完整金属连通分量均为 2（独立直通线 + 耦合臂及左右馈线），
耦合臂到左右固定馈线的距离均为 0，未复现默认大步长的馈线断开。
这只是两轮小范围结果，不能据此把该时间步视为已完成标定。

```powershell
.\.venv\python.exe scripts/run_coupler_adjoint_levelset.py results/shape_derivative_mine_2026-11-09/coupler_small_step.yaml --iterations 2 --output-dir results/shape_derivative_mine_2026-11-09/coupler_small_step
```

### 保存产物核对

```powershell
.\.venv\python.exe results/shape_derivative_mine_2026-11-09/check_live_runs.py
```

四次成功运行均确认：两次提交、三次前向评估、三组状态/场图齐全，目标值、
速度与水平集均有限，最终水平集与状态 2 逐点一致。逐轮将保存的边界 E/H
按法向/切向独立显式投影，与 `shape_derivative_mine` 输出对比，
最大绝对差约 `2.98e-8`（灵敏度量级最高为 `1e8`）。
功分器、天线和小步长耦合器的完整金属连通分量均保持初始数量，
默认大步长耦合器被记录为 `runtime_passed_geometry_warning`。

机器可读结果为 `results/shape_derivative_mine_2026-11-09/live_validation.json`；
`local_smoke_status.json` 保留首次内存失败记录，并更新为三个默认案例的两轮运行已完成。
综合状态为 `passed_with_geometry_warning`，含义是运行与公式核对通过，但默认耦合器存在断连，
且功分器目标未改善。

尚未做有限差分梯度校准和长程优化收敛测试。
