# 开源 level set 几何更新调研：2026-11-15

## 结论与范围

当前实验 CAD 分支是显式多边形边界追踪：顶点速度由边界形状导数的正定度量得到，随后直接移动顶点，phi 从新多边形重新采样。它可以作为固定拓扑形状优化的离散方法，但并不等同于下面核查的 Hamilton–Jacobi 水平集更新。当前实现缺少可靠的拓扑事件处理、边界质量维护以及面向有限更新的目标控制，不能仅靠正定速度映射证明天线优化有效。

本次只调研与比对，没有替换生产更新算法，没有启动新的 CST 求解。已下载核查的 OpenLSTO、GridapTopOpt 源码及提交号存于 [manifest](../results/levelset_research_2026-11-15/manifest.json)。MMG 与 SPINS 部分依据官方文档/公开源码阅读，未在本机运行这些项目。

## 1. OpenLSTO：隐式场推进，再重建边界线段

仓库：[M2DOLab/OpenLSTO](https://github.com/M2DOLab/OpenLSTO)。核查提交 `853b9cc433b84c60cd1318a36f9a1d0e6cf65877`。

- [level_set.cpp](https://github.com/M2DOLab/OpenLSTO/blob/853b9cc433b84c60cd1318a36f9a1d0e6cf65877/M2DO_LSM/src/level_set.cpp)：`computeVelocities` 把边界速度传到网格并使用 fast marching；`computeGradients` 计算迎风梯度；`update` 在窄带节点更新 signedDistance；`reinitialise` 用 fast marching 重建距离场和窄带。
- [boundary.cpp](https://github.com/M2DOLab/OpenLSTO/blob/853b9cc433b84c60cd1318a36f9a1d0e6cf65877/M2DO_LSM/src/boundary.cpp)：`discretise` 清空旧点/线段，再按单元边上的 phi 值插值出交点，重建连接；四条边都被切割的歧义单元按中心符号选连接。`computeAreaFractions` 用切割几何计算面积分数。
- [optimise.cpp](https://github.com/M2DOLab/OpenLSTO/blob/853b9cc433b84c60cd1318a36f9a1d0e6cf65877/M2DO_LSM/src/optimise.cpp)：所查 Newton–Raphson 例程包含灵敏度缩放、域边界距离和 moveLimit 处理。普通例程计算最大速度时没有对所有速度取绝对值，另一 stress 例程有取绝对值；因此不能把源码注释当作所有分支均严格满足移动界的证明。

边界点是当前隐式场的派生表示，不是永久追踪的材料点。这个机制避免必须维持上一轮每条边的身份，但仍受网格分辨率、零值容差和重初始化误差影响。该实现的符号约定也不能直接照搬：其 boundary.cpp 将负 phi 标为 OUTSIDE，与本项目负 phi 表示金属的约定不同。

## 2. GridapTopOpt：Hilbert 延拓、HJ 演化和有限更新控制

仓库：[zjwegert/GridapTopOpt.jl](https://github.com/zjwegert/GridapTopOpt.jl)。核查提交 `513c6bc2a4e4f171f323542e5ed63e183050a22b`。

- [VelocityExtension.jl](https://github.com/zjwegert/GridapTopOpt.jl/blob/513c6bc2a4e4f171f323542e5ed63e183050a22b/src/VelocityExtension.jl) 使用 Hilbert 空间内积定义延拓/正则化速度，兼顾域内速度和无穷小下降方向。
- [FiniteDifferenceEvolver.jl](https://github.com/zjwegert/GridapTopOpt.jl/blob/513c6bc2a4e4f171f323542e5ed63e183050a22b/src/LevelSetEvolution/Evolver/FiniteDifferenceEvolver.jl) 推进 HJ 方程。单个数值时间步为 `gamma * min(grid_spacing)/(eps(T)^2 + max(abs(velocity)))`，并执行配置的多个演化子步。单子步尺度不能直接当作整轮总位移界。
- [FiniteDifferenceReinitialiser.jl](https://github.com/zjwegert/GridapTopOpt.jl/blob/513c6bc2a4e4f171f323542e5ed63e183050a22b/src/LevelSetEvolution/Reinitialiser/FiniteDifferenceReinitialiser.jl) 单独求解重初始化。
- [HilbertianProjection.jl](https://github.com/zjwegert/GridapTopOpt.jl/blob/513c6bc2a4e4f171f323542e5ed63e183050a22b/src/Optimisers/HilbertianProjection.jl) 包含方向归一化、约束投影、可选线搜索与拒绝后恢复 phi。其默认接受条件有容差、启用轮次与最小参数例外，并不保证每轮目标严格下降。

对本项目最直接的启发是：Hilbert/正定速度映射与有限步接受机制承担不同任务。当前 CAD 顶点速度映射的数学思路不必全部否定；不足在于后续的几何推进及其接受控制。

## 3. MMG：把隐式界面重新离散为贴体网格

[官方隐式域网格教程](https://mmgtools.org/tutorials/tutorials_mmg3d_implicit.html)说明：输入背景网格及节点上的 level set，`-ls` 离散指定等值面，并优化界面网格。输出是由该界面分隔的网格区域，不是商业 CAD 的 STEP/B-rep。

[官方参数说明](https://mmgtools.org/tutorials/tutorials_options.html)提供 `-hausd` 边界逼近参数、`-hmin/-hmax` 边长参数、`-hgrad` 尺寸渐变控制，以及 level-set 模式的小分量移除选项。它体现的是“重建界面并控制逼近/网格质量”，而不是让旧线段永久保留。参数设置不是对任意薄缝与拓扑事件都成功的保证；这些机制也不能直接替代 CST 的实体导入验证。

## 4. SPINS：参数场与制造多边形导出分离的补充参照

[SPINS parametrization.py](https://github.com/stanfordnqp/spins-b/blob/master/spins/invdes/parametrization/parametrization.py)中的 `CubicParam` 把控制参数插值到细网格，并通过参数映射的 Jacobian 传递梯度；`generate_polygons` 从插值场的 0.5 等值线生成多边形。这一具体类是 cubic 参数化/连续松弛路径，不能冒充经典 HJ 水平集求解器；也不能据此断言其每轮求解都使用导出的多边形。它可借鉴之处是明确区分优化表示和制造输出。

## 5. 当前实现的问题应如何表述

| 项目 | 当前 CAD 分支 | 调研后的判断 |
|---|---|---|
| 主几何变量 | 多边形顶点；phi 是派生量 | 属于显式边界法，应与 HJ 路线区分 |
| 速度平滑 | 边界正定度量，检查有符号方向功 | 无穷小方向有依据，但不是有限步改善证明 |
| 移动过程 | 固定 dt 的直线顶点轨迹 | 需边界质量维护；当前碰撞即停不能承担一般拓扑优化 |
| 角点与短边 | 保留角点、分割长边；缺少完整短边合并/重采样机制 | 切向位移对连续光滑边界的一阶作用可能为零，对离散顶点连接和网格质量仍有影响；角点尤其需单独处理 |
| 制造约束 | 顶点更新后再进行几何过滤和其他修补 | 最终位移不一定等于求导时指定的位移，须把投影变化纳入检测 |
| phi/CAD 转换 | 新 CAD 路径避免了旧路径的初始表示切换 | 修复了一个问题，但没有认证 CST 对不同边界离散的响应一致性 |
| 目标接受 | 有效几何均提交，无 FoM 回退 | 不能保证大多数迭代改善 |

旧 HJ 路径已经具有迎风更新、RK2 和 CFL 子步；其失败不能概括成“水平集法不行”。已证实的问题是初始精确 CAD 与后续折线提取采用不同映射，以及此前可能反向的速度处理。恢复隐式更新必须同时解决这些接口问题。

## 6. 建议的路线与验证门槛

建议把真正的 Eulerian 水平集推进作为拓扑优化主路线，把显式 CAD 顶点方法保留为固定拓扑与方向导数检查参照。这是根据现有缺陷和源码比较提出的工程建议，尚未由新实验验证。

推荐链条：统一的初始隐式几何 → 有下降方向保证的速度延拓 → HJ 推进 → 受控重初始化 → 统一等值线提取/边界重建 → 带误差控制的 CAD 导出 → 实际求解。每轮边界点允许重建；固定端口连接、孔洞和区域标签必须显式保留。

不能仅删除初始精确 CAD 分支后继续：这样可能悄悄改变起点。必须明确离散几何及其分辨率误差，确保 F0、场和导数针对同一个实际导出几何。若原角点不能被现有网格保留，应比较局部细化、贴体/切割表示或角点约束的代价，而不是声称节点距离采样能无损保存任意 CAD。

实施前先通过以下门槛：

1. 同一几何无操作重建、导出、再次读回的一致性；区分连续 CAD、提取折线、CST 实体和求解网格。
2. 面积/周长等已知导数目标的双向 Taylor 检查；覆盖角点、孔洞、固定连接和多个分量。拓扑事件处不强求不存在的经典导数。
3. 记录重初始化和约束投影引入的额外边界位移，检查其相对目标更新的量级。
4. 检查实际天线小扰动响应的稳定性；顶点数从 8 变 336 是待排查线索，并非已证实根因。
5. 用同一轮最终几何检查方向和实际目标趋势，再回归已有案例。

几何演化的稳定子步、整次优化移动限制、目标线搜索是三个不同问题。原先暂缓的时间步/接受策略调整未在本次调研中实施；若后续采用，需要单独给出数学依据与实验验证。
