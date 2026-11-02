# 单频频域求解与边界点直接采样：实现和验证

日期：2026-11-02。范围为 coupler、divider、antenna 三个单频入口；多频配置与原有默认路径保持不变。本次暂不讨论额外复数源系数，所有真实优化使用 `--adjoint-source unit`，保留公式（25）的取实部及固定 `dt=1e-8`，没有回溯减步。

**调整已实现，真实 CST 计算流程通过；空间网格精度及灵敏度正确性尚未通过验收。** coupler 一步改善，divider、antenna 一步退化。直接点导出消除了仓库中体网格到高度平面、再到边界点的外部插值链，但不能消除 CST 内部有限元场求值误差，也不能据此宣布优化差异已解决。

## 1. 实际代码调整

| 位置 | 调整 |
| --- | --- |
| `eaopt/solver/cst_setup.py`、三个 `cst_model*.py` | 增加单频 FD 模板，显式选择 `HF Frequency Domain`、四面体二阶、单个离散目标频点及指定端口/模式 1；两个 CPU，线性求解精度 `1e-6`。TD 模板也显式选择自己的求解器。 |
| `eaopt/runner.py` | 单频默认 `--solver fd --boundary-sampling cst-points`；多频仍默认 TD/grid，明确拒绝多频 FD。新单频路径仅对活动边界取场，直接送入原场核。 |
| `eaopt/solver/cst_results.py` | 使用 CST `ASCIIExport.SetPointFile` 查询指定 XYZ 复数 E/H；按坐标恢复输入顺序、合并重复点并检查一一对应；按频率和激励端口唯一选中场结果，缺失或歧义立即报错。FD 的 S 参数和入射功率要求精确目标频点，不用邻频插值。 |
| `eaopt/solver/cst.py` | 求解前读取实际求解器和 FD 激励端口/模式；求解后读取真实入射功率并核对 0.5 W。重建几何后禁止取旧场；保存求解日志及四面体文件，正反向几何/拓扑/边界文件哈希不同则终止梯度计算。 |
| `configs/{coupler,divider,antenna}.yaml` | PEC 外侧正偏移 `.05 mm`；coupler/divider 高度 `.0175 mm`，antenna 高度 `.009 mm`，对应各自金属侧壁中高处的空气侧候选采样位置。 |
| `scripts/cst_init_fwd.py`、`cst_init_bwd.py` | 手工初始化增加显式 `--solver fd`，保持其历史默认 TD；FD 单频读数、功率检查及场源选择同步调整。 |

FD 的全局频率范围先设置为 `0..2f`，随后设置唯一目标离散样点 `f`。首次尝试因新工程默认范围不覆盖 5 GHz 而在配置阶段报错；已修正顺序，并保留失败状态在 `coupler_probe/status.json`，没有计为成功求解。

当前优化默认关闭独立自适应网格，以便检查两个工程实际使用相同离散几何。**这保证正反向的一致性，不保证网格已足够细。** 线性求解器的 `1e-6` 同样不等于空间离散误差小于 `1e-6`。

## 2. 点位、材料侧和产物

活动边界点使用 `p_sample = (p_boundary + δ n, z_sample)`，坐标单位为 mm。对每次实际送入 CST 的 CAD（包含固定金属和孔洞）逐点检查：

- `δ` 必须有限且大于零；`z` 必须位于基板或空气域内部，拒绝 `z=0` 界面。
- 点必须在基板平面范围内；位于金属厚度范围时，XY 点不能落入任何实际 PEC。
- 空气侧用 `ε0`，基板侧用 `ε0 εr`；不再把空气侧场和基板介电常数混用。
- 无效点保存诊断并终止；不静默删点、缩偏移或补零。非活动边界的速度为零，不要求其采样点有效。
- 反向场在同一组坐标求值；antenna 的反射目标沿用现有同端口前向场复用方式。

每次需要梯度的 `iter_NNN/boundary_fields.npz` 保存实际 XYZ、边界索引/法向、原始正反向复数 E/H、材料介电常数、高度和偏移；`cst_evaluations.jsonl` 保存材料、源系数及求解器信息，`meta.json` 保存真实功率和网格哈希。仅做最后一次目标评估的状态不再生成伴随场。

绘图另外导出指定高度的二维点阵，保存为 `fields_*_{e,h}.npz`，形状为 `(nx,ny,1,3)`，与梯度点集合分离。这样避免仅为绘图导出整块三维空气域。绘图仍会在 PEC 覆盖位置显示其求解器场值，不能将整幅图当作“全部点都在空气侧”的证据。

这些默认高度及 `.05 mm` 偏移已通过位置/材料检查，**仍是候选值**；没有证明它们已进入侧壁外侧场迹的偏移收敛区间。

## 3. 真实 CST 验证结果

结果根目录：[fd_single_2026-11-02](../results/fd_single_2026-11-02/)。下表都是实际求解和一次实际几何更新，没有模拟 CST 返回值。

| 运行目录 | 目标 | 更新前 → 更新后 | 活动点数 | 原生求解次数 | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| `coupler_planar_smoke` | 最大化 \|S31\| | 0.09227970 → 0.15972476 | 240 | 3 | 改善 |
| `divider_smoke` | 最大化 \|S21\| | 0.66424568 → 0.64876370 | 795 | 3 | 退化 |
| `antenna_smoke` | 最小化 \|S11\| | 0.89306296 → 0.94007890 | 301 | 2 | 退化，反射变强 |

每个传输案例为初始前向、初始反向、更新后前向共 3 次；antenna 复用前向场，因此为 2 次。`summary.cst_evaluations=2` 只计两次前向目标评估，不是全部原生求解次数。三个运行均使用固定时间步且 `backtracks=0`，没有用减步或筛选目标制造改善。

对初始传输案例检查 `moegeom.tet`、`topot.tet`、`bc3d.tet`：正反向文件逐字节一致。所有实际源均为模式 1，入射功率都是 0.5 W，目标频点和边界场值有效。初始点位以保存的 CAD 重新核验通过。

另保留一次前版本绘图路径的 `coupler_smoke`，它同样使用 FD 和直接边界点，但绘图仍从三维网格导出。与最终二维绘图版本相比：

- 最终 FoM 绝对差 `1.8617e-8`，最终 CAD Hausdorff 距离 `1.0734e-7 mm`；原始点 E/H 相对差约 `0.9e-7..2.4e-7`。
- 最终水平集数组最大差 `.0041623`，不能声称逐元素完全相同；最终物理零等值线/CAD 差异远小于 `.1 mm` 设计网格。
- 因此此次绘图内存调整没有造成可分辨的目标或 CAD 改变。divider 的首次运行保留旧三维绘图产物；最终二维绘图路径已在 coupler 的正反向及 antenna 的同端口路径实际验证。

单独的 FD 初始前向探测成功 1 次；冻结 coupler CAD 的网格诊断另调用求解器 1 次、内部执行 3 级网格。加上上述 4 个短程运行，共 13 次成功求解器调用、15 次含内部细化的电磁求解阶段；配置阶段失败尝试不计入。

## 4. 网格检查发现的限制

冻结 coupler CAD，设置三个自适应 pass、`MaxDeltaS=.001`，在相同 `.05 mm` 外偏移、`.0175 mm` 高度点对照粗网格与加密结果。原生日志与曲线见 [coupler_mesh_sampling](../results/fd_single_2026-11-02/coupler_mesh_sampling/)。

| 项目 | 实际结果 |
| --- | --- |
| 四面体数 | 1886 → 3504 → 5490 |
| 相邻 pass 全 S 参数最大变化 | 第 2 级 `.0344804`，第 3 级 `.0208832` |
| 要求 | `.001` |
| 终止原因/验收 | 达到最大 pass 数；**未达到网格收敛要求** |
| 同一组坐标的 E 相对 L2 变化 | 30.61% |
| 同一组坐标的 H 相对 L2 变化 | 23.44% |

这说明直接点导出成功后，**粗有限元场本身仍明显依赖网格**。上述百分比是两次离散解的差异，不是相对于连续真解的已知误差。诊断还保存 3 个偏移 × 3 个侧壁高度的前向点场，但未据此认证全部偏移/高度或梯度收敛；该诊断只有前向加密场，不是加密后的共同正反向梯度实验。

不能把 divider/antenna 的单步退化全部归因为网格，也不能从 coupler 单步改善推出灵敏度符号已全面正确。本次未新增方向导数有限差分的定量通过证据；前期目标/场核对应问题仍然开放，额外复数系数继续搁置。

## 5. 验收记录与复跑

[validation_summary.json](../results/fd_single_2026-11-02/validation_summary.json) 分别记录流程断言、物理 CAD 对照、目标变化和未通过的网格要求，状态为 `pipeline_passed_accuracy_not_certified`。

已运行：

```powershell
.venv/python.exe tmp/check_single_fd_changes.py
.venv/python.exe tmp/validate_single_fd_results.py
.venv/python.exe scripts/check_design_geometry.py
```

离线检查覆盖三模板的频点/激励、三案例点位和材料、PEC/界面/域外拒绝、单点频谱与精确频率、场源歧义、未求解/过期场拒绝及无效绘图步长；其中谱线/结果树是纯读取器输入样例，不代表仿真。几何检查 7 项通过；修改文件编译和 `git diff --check` 通过。已查看最终 coupler 场图，标题高度为实际 `.0175 mm`。CST 已恢复交互模式，保留原有环境。

实际优化复跑命令（请使用新目录；目录非空时程序另建时间戳子目录）：

```powershell
.venv/python.exe scripts/run_coupler_adjoint_levelset.py --attach --solver fd --boundary-sampling cst-points --adjoint-source unit --iterations 1 --save-fields --output-dir results/fd_single_recheck/coupler
.venv/python.exe scripts/run_divider_adjoint_levelset.py --attach --solver fd --boundary-sampling cst-points --adjoint-source unit --iterations 1 --save-fields --output-dir results/fd_single_recheck/divider
.venv/python.exe scripts/run_antenna_adjoint_levelset.py --attach --solver fd --boundary-sampling cst-points --adjoint-source unit --iterations 1 --save-fields --output-dir results/fd_single_recheck/antenna
```

保留 `--solver td --boundary-sampling grid` 作旧路径排查；要精确回放旧实验，还需使用其 `run_config.json` 冻结的高度、偏移及其他配置。当前 YAML 已调整，不能只加 TD 参数就宣称与历史实验完全相同。旧工程不会自动转换，运行时发现实际求解器不符会报错。

下一阶段应先在冻结 CAD 上获得收敛的侧壁局部网格，再让正反向复用同一最终网格、检查偏移/高度稳定区间，最后在独立形变方向上比较预测导数与真实目标差分。独立开启两个工程的自适应网格可能破坏共同离散，不能直接把当前一致性保护关掉继续计算。短程更新和实际场导出已经完成验证；长程固定步长收敛仍未验证。
