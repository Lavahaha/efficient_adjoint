"""Summarize completed S11 directional controls and their certification limits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

import check_s11_cross_case as experiment

ROOT, OUT = experiment.ROOT, experiment.OUT


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    sampling = read(OUT/'sampling_normalization.json')
    report = dict(cases={}, completed_evaluations=0, physical_gradient_certified=False,
        smaller_h_evaluations_started=False, reason='First-batch forward differences are not stable in h; do not fit a correction factor')
    table, slopes, port_table, sampling_table, raw_table, values = [], [], [], [], [], []
    for name, title in [('divider', '功分器'), ('coupler', '耦合器')]:
        data = read(OUT/name/'results.json')
        assert data['completed'] and len(data['evaluations']) == 6
        by_label = {r['label']: r for r in data['evaluations']}
        assert data['repeat_mesh_identical'] and data['repeat_abs_difference'] == 0 and data['repeat_complex_difference'] == 0
        checks = experiment.comparisons(data['evaluations'])
        assert checks == data['comparisons']
        base = by_label['base']
        s0 = complex(*base['s11'])
        chosen = [r for r in checks if r['offset_mm'] == .05]
        d1, d2 = [r['fd_abs_slope_per_mm'] for r in chosen]
        ds1, ds2 = [complex(*r['fd_complex_slope_per_mm']) for r in chosen]
        stability = dict(abs_slope_change_relative_to_max=abs(d2-d1)/max(abs(d1), abs(d2)),
            complex_slope_change_relative_to_max=abs(ds2-ds1)/max(abs(ds1), abs(ds2)),
            signs_agree=d1*d2>0, stable_within_10pct=abs(d2-d1) <= .1*max(abs(d1), abs(d2)))
        assert not stability['stable_within_10pct']
        audit = sampling['cases'][name]
        rows = audit['boundary']['rows']
        def q(ox, oz, delta):
            return next(r for r in rows if (r['order_x'], r['order_z'], r['offset_mm']) == (ox, oz, delta))['abs_slope_per_mm']
        quadrature = dict(height_3_to_15_relative=abs(q(4, 15, .05)-q(4, 3, .05))/abs(q(4, 15, .05)),
            line_4_to_8_relative=abs(q(8, 15, .05)-q(4, 15, .05))/abs(q(8, 15, .05)),
            offset_0_05_to_0_00078125_relative=abs(q(8, 15, .00078125)-q(8, 15, .05))/abs(q(8, 15, .05)))
        mode_records = []
        for row in data['evaluations']:
            path = Path(row['project_path'])
            modepath = path.with_suffix('')/'Result/Port1_Info1(#0000).mmd'
            mode = read(modepath)['port mode meta data']['mode meta data'][0]
            portmesh = path.with_suffix('')/'Result/Port1.slim'
            mode_records.append(dict(label=row['label'], alpha_per_m=mode['gamma real (alpha)'],
                impedance_ohm=[mode['line impedance real'], mode['line impedance imag']],
                mode_type=mode['mode type'], calculation_frequency=mode['calculation frequency'],
                port_mesh_sha256=hashlib.sha256(portmesh.read_bytes()).hexdigest()))
            assert mode['calculation frequency'] == 5e9 and mode['gamma real (alpha)'] == 0
            assert abs(mode['line impedance imag']) < 1e-7
            values.append(f"| {title} | {row['label']} | {row['magnitude']:.12f} | {row['mesh']['tetrahedra']} |")
        baseline_record = mode_records[0]
        port_identical = all(r['port_mesh_sha256'] == baseline_record['port_mesh_sha256'] for r in mode_records)
        residuals = []
        gs = complex(*base['fields']['predictions'][0]['predicted_complex_slope_per_mm'])
        gf = base['fields']['predictions'][0]['predicted_abs_slope_per_mm']
        for row in data['evaluations']:
            h = row['h_mm']
            if h:
                s = complex(*row['s11'])
                rs, rf = s-s0-h*gs, abs(s)-abs(s0)-h*gf
                residuals.append(dict(h_mm=h, complex_residual=[rs.real, rs.imag],
                    abs_residual=rf, complex_residual_over_abs_h=abs(rs)/abs(h),
                    abs_residual_over_abs_h=abs(rf)/abs(h)))
        case_report = dict(stability=stability, quadrature=quadrature, comparisons=checks,
            residuals_at_offset_0_05_mm=residuals,
            port_mode_metadata=mode_records, all_port_meshes_identical=port_identical,
            repeated_base_identical=True, all_3d_meshes_identical=data['all_meshes_identical'],
            port=audit['port'], production_hashes_unchanged={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() == h
                for p, h in data['production_hashes'].items()})
        assert all(case_report['production_hashes_unchanged'].values())
        report['cases'][name] = case_report
        report['completed_evaluations'] += len(data['evaluations'])
        for r in chosen:
            table.append(f"| {title} | {r['h_mm']:g} | {r['predicted_delta_plus']:+.9f} | {r['actual_delta_plus']:+.9f} | {r['actual_over_predicted_plus']:+.3f} |")
            slopes.append(f"| {title} | {r['h_mm']:g} | {r['fd_abs_slope_per_mm']:+.9f} | {r['predicted_abs_slope_per_mm']:+.9f} | {100*r['complex_relative_mismatch']:.2f}% |")
        p = audit['port']
        port_table.append(f"| {title} | {p['rows'][-1]['average_power_w']:.9f} | {100*p['rows'][-1]['peak_power_normalization_relative_error']:.3f}% | {100*p['total_field_projection']['incident_error_from_unit']:.3f}% | {p['total_field_projection']['s11_absolute_error']:.6f} |")
        sampling_table.append(f"| {title} | {100*quadrature['height_3_to_15_relative']:.4f}% | {100*quadrature['line_4_to_8_relative']:.4f}% | {100*quadrature['offset_0_05_to_0_00078125_relative']:.2f}% |")
        raw = base['fields']['predictions'][0]['raw_code_midplane_line_integral']
        raw_table.append(f"| {title} | {raw:+.9g} | {chosen[0]['predicted_abs_slope_per_mm']:+.9f} |")
    assert report['completed_evaluations'] == 12
    experiment.write(OUT/'analysis.json', report)

    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 3, figsize=(13, 7.5), constrained_layout=True)
    for i, name in enumerate(('divider', 'coupler')):
        data = read(OUT/name/'results.json')
        base = next(r for r in data['evaluations'] if r['label'] == 'base')
        points = sorted([r for r in data['evaluations'] if not r['repeat']], key=lambda r:r['h_mm'])
        hs = np.array([r['h_mm'] for r in points])
        actual = np.array([r['magnitude']-base['magnitude'] for r in points])
        pred = base['fields']['predictions'][0]['predicted_abs_slope_per_mm']
        ax = axes[i, 0]
        ax.plot(hs, actual, 'o-', label='Direct CAD forward')
        ax.plot(hs, hs*pred, 's--', label='Conditional surface prediction')
        ax.set(xlabel='Displacement h (mm)', ylabel='Change in |S11|', title=name.capitalize())
        ax.legend(fontsize=8)
        c = [r for r in data['comparisons'] if r['offset_mm'] == .05]
        ax = axes[i, 1]
        ax.plot([r['h_mm'] for r in c], [r['fd_abs_slope_per_mm'] for r in c], 'o-', label='Central difference')
        ax.axhline(pred, linestyle='--', label='Conditional prediction')
        ax.set(xlabel='Difference step h (mm)', ylabel='Slope (1/mm)', title='Derivative stability')
        ax.legend(fontsize=8)
        ax = axes[i, 2]
        rows = sampling['cases'][name]['boundary']['rows']
        for ox, oz in [(4, 3), (4, 15), (8, 15)]:
            rr = [r for r in rows if r['order_x'] == ox and r['order_z'] == oz]
            ax.semilogx([r['offset_mm'] for r in rr], [r['abs_slope_per_mm'] for r in rr], 'o-',
                label=f'Gauss x={ox}, z={oz}')
        ax.set(xlabel='Air-side offset (mm)', ylabel='Predicted slope (1/mm)', title='Same exported FEM solution')
        ax.legend(fontsize=8)
    for ax in axes.ravel():
        ax.grid(alpha=.25)
    fig.savefig(OUT/'comparison.png', dpi=180)
    plt.close(fig)

    document = r'''# S11 形状导数跨案例检查（2026-11-12）

首批 12 次 CST 前向评估完成：功分器和耦合器均以 |S11| 为目标，每例 h=0、±0.10、±0.05 mm 和一次独立重建的零扰动重复。前一轮暂停时已有 8 次，本轮复用并补齐 4 次。中断建模工程单独保留，不计入有效评估。

**本轮没有认证物理形状导数。当前实现的预测没有通过对照，且直接前向差分尚未形成稳定导数参照。** 功分器缩小步长后斜率明显改变，耦合器翻号；因此按条件没有继续 h=±0.025 mm。不能从本轮比例拟合梯度系数。

## 1. 几何与求解控制

- 功分器：5 GHz，输入条带上边 y=1 mm，x∈[10,22] mm；耦合器：5 GHz，耦合臂上边 y=0，x∈[3,9] mm。端口和固定连接处不动。
- 各段用 25 个固定 x 节点，节点速度 u=sin²[π(x-a)/(b-a)]，段间线性插值；y_h=y_0+h u。端点速度为零。所有 h 共用相同初始法向 n=(0,1,0) 和速度函数。
- 精确面积关系 A(h)=A(0)+h∫u dx；两例面积方向导数分别为 6 和 3 mm。厚度 0.035 mm，因此体积方向导数分别为 0.21 和 0.105 mm²。
- 直接生成 CAD；逐例核对原生包围盒、体积、表面积，以及传入求解器轮廓与指定 CAD 的对称差。不经过 HJ/约束/重初始化。
- FEM 二阶、Direct、线性精度 1e-8、单 CPU，关闭自适应；对活动金属实体设置固定局部尺寸 0.5 mm，其余沿用模板。不是全域均匀 0.5 mm，也不是实际网格冻结。
- 两例基准重复的 3D 网格和复数 S11 均完全一致；所有几何扰动后 3D 网格均有变化。12 次有效求解及几何审计全部成功。相同网格控制不保证形状响应差分不受重剖分影响。

| 案例 | 状态 | S11 线性幅值 | 四面体数 |
|---|---|---:|---:|
VALUES

## 2. 被检查的公式与归一化

代码在空气侧中高处的原始线积分为：

\[
G_{code}=\sum_i\operatorname{Re}\left[-j\omega\frac{\overline S_0}{|S_0|}
\{\epsilon_0E_{n,i}^2+\mu_0\mathbf H_{t,i}\cdot\mathbf H_{t,i}\}\right]u_iw_i.
\]

场乘积为双线性，不共轭；幅度目标的链式因子才包含 conj(S0)。本轮直接调用生产 shape_derivative_mine 并用独立显式投影核对，代数实现一致。其调用链默认没有加入完整侧面厚度积分、功率归一化及 SI 长度换算，不能直接把这个原始线积分当成每毫米的物理 FoM 导数。整体速度缩放与物理导数归一化不是同一项验收。

条件性物理预测采用 e^(+jωt)、峰值场、金属指向空气的法向、固定端口及有效 PEC 边界迹假设。对单个损耗可忽略且实模态、单位实入射幅度的端口，设 I_b=∫port(e×h)·n_in dA。端口双线性反应归一化量为 N_p=2I_b；若模态及总场归一化闭合，I_b=2Pin，才得到 N_p=4Pin。

\[
Q=\int_{\Sigma_{mov}}(\epsilon_0E_n^2+\mu_0\mathbf H_t\cdot\mathbf H_t)u\,dA,
\quad G_S=-\frac{j\omega}{N_p}Q,
\quad G_F=\operatorname{Re}\left[\frac{\overline S_0}{|S_0|}G_S\right].
\]

本批数值表暂取 N_p=4Pin、Pin=0.5 W。积分中 mm²→m² 乘 10^-6，再把每米导数转成每毫米乘 10^-3。空气侧偏移 δ>0 的字段不是已认证的真实边界迹，因此表中均标记为“条件性预测”。没有从有限差分拟合任何系数。

\[
\Delta S_{pred}=hG_S,\quad\Delta F_{pred}=hG_F,\quad
D_S(h)=\frac{S(h)-S(-h)}{2h},\quad D_F(h)=\frac{|S(h)|-|S(-h)|}{2h}.
\]

| 案例 | 当前代码原始中高线积分（δ=0.05 mm） | 归一化侧面积分 G_F / mm⁻¹ |
|---|---:|---:|
RAW

## 3. 预测与直接前向变化

下表统一 δ=0.05 mm，沿每一 CAD 小段 4 点 Gauss，厚度 3 点 Gauss。比例定义为实际正扰动变化/预测正扰动变化；不等于中心差分/预测导数。

| 案例 | h / mm | 预测幅度变化 | 实际幅度变化 | 实际/预测 |
|---|---:|---:|---:|---:|
TABLE

| 案例 | h / mm | 前向中心差分 / mm⁻¹ | 预测导数 / mm⁻¹ | 复数导数相对失配 |
|---|---:|---:|---:|---:|
SLOPES

复数失配定义为 |D_S-G_S|/|D_S|。两例均未达到预定 10% 筛选目标，复数结果也不吻合，不能把问题仅归为幅度链式权重。

残差 R_F=F(h)-F(0)-hG_F、R_S=S(h)-S(0)-hG_S 及其除以 |h| 的结果见 analysis.json；基础差分见各例 results.json。本轮有限尺度和重剖分下不能建立 |R|/|h|→0。功分器正扰动的 |R_F|/h 约为 0.19204、0.19076，未出现预期一阶残差减弱。耦合器差分翻号，暂不拟合残差收敛阶。

## 4. 取场与积分分辨率

对每例同一份基准场另外直接导出 31680 点，检查 δ=0.05、0.025、0.0125、0.00625、0.003125、0.00078125 mm，沿边/厚度积分组合 (4,3)、(4,7)、(4,15)、(8,15)。采样点均通过线性四面体包含检查，独立数组布局重现原始 3 组预测。

| 案例 | z 积分 3→15 点相对变化，δ=.05 | x 积分 4→8 点相对变化，δ=.05 | δ=.05→.00078125 的变化，采用 (8,15) |
|---|---:|---:|---:|
SAMPLING

当前导出 FEM 场上的积分加密影响很小；缩小偏移没有消除数量级差距。此结论只针对已导出字段，不证明真实边缘场或 FEM 网格收敛；有限元近边缘误差可以在积分加密后仍然存在。

## 5. 波导端口独立核对

12 份原生 Port1 模态记录均为 5 GHz QTEM，alpha=0、线阻抗虚部近零。基准端口直接导出原生 e1/h1，用按基板/金属材料边界分段的二维 Gauss 面积分；另从总 E/H 场投影入射和反射幅度，不用前向形状差分拟合。

| 案例 | 模态场积分平均功率 / W，目标 .5 | 功率偏差 | 投影入射幅度相对单位值偏差 | 投影重构 S11 的绝对误差 |
|---|---:|---:|---:|---:|
PORT

功分器端口积分到 0.025 mm 面板，最后两档双线性通量变化约 0.0314%；耦合器到 0.05 mm 面板，变化约 0.0435%。积分自身已比较稳定，但功分器功率核对未达到 1%，两例总场投影均未达到入射幅度及 S11 的 1% 目标。耦合器模态功率通过 1% 并不等于整个端口闭合检查通过。

这些偏差可能涉及导出场、端口截面离散或模态/总场一致性，不能单凭投影认定真实源幅度错误。没有用投影值或端口积分值重新缩放预测。端口原生元数据和网格指纹见 analysis.json；“端口几何固定”也不等于“端口离散网格固定”。

## 6. 本轮判断及下一轮入口

1. 当前计算流程对这两个 |S11| 案例没有通过物理导数验证；仅用固定网格尺寸不能获得可信差分参照。
2. 两例重复基准完全一致，排除了本批可观察到的重复运行随机变化；不能据此排除扰动后的网格离散差异、端口离散变化和有限 h 非线性。
3. 单纯增加积分点数或减小空气侧偏移，在本批字段上不足以解释全部差距；原始线积分缺少物理尺度归一化的问题需要单独处理。
4. 下一轮先建立一个端口闭合、几何扰动可解析或实际网格受控的参照，重点验证含电场与磁场贡献的复数 S11 导数；原有短路 TEM 校准只充分覆盖磁项，不能替代完整边界核验证。
5. 针对现有案例，应先改善体网格/端口离散控制并重做 0、±0.10、±0.05 mm 的稳定性检查；稳定后才增加 ±0.025 mm 和检验一阶残差。不能把 43 倍或 28 倍作为经验修正系数。

新增诊断脚本和报告；生产形状导数、模型模板、默认配置及优化步长/接受规则未修改。基准工程的端口/字段提取不增加求解，源 .cst 文件哈希保持一致。CST 主窗口保留，诊断工程关闭，静默状态恢复。

验证：14 个几何/面积/速度积分自检、2 个解析复数及幅度差分自检和二阶残差趋势检查、4 个端口积分自检、9 项既有形状核测试通过；3 个诊断脚本语法检查通过。这些属于实现与诊断逻辑检查，不能代替上面未通过的物理验收。

![S11 前向、导数与取场对照](../results/s11_cross_case_2026-11-12/comparison.png)

## 记录与复现

- [完整分析](../results/s11_cross_case_2026-11-12/analysis.json)
- [功分器结果](../results/s11_cross_case_2026-11-12/divider/results.json)、[耦合器结果](../results/s11_cross_case_2026-11-12/coupler/results.json)
- [独立端口及边界积分核对](../results/s11_cross_case_2026-11-12/sampling_normalization.json)
- [CST 最终状态和生产哈希](../results/s11_cross_case_2026-11-12/final_state.json)
- [直接 CAD 检测脚本](../scripts/check_s11_cross_case.py)、[采样与归一化脚本](../scripts/check_s11_sampling_normalization.py)、[汇总脚本](../scripts/analyze_s11_cross_case.py)

```powershell
.\.venv\python.exe scripts/check_s11_cross_case.py --self-check
.\.venv\python.exe scripts/check_s11_sampling_normalization.py --self-check
.\.venv\python.exe scripts/check_s11_cross_case.py --pid 39084 --resume
.\.venv\python.exe scripts/check_s11_sampling_normalization.py --pid 39084
.\.venv\python.exe scripts/analyze_s11_cross_case.py
```

两类 self-check 分别验证精确 CAD/面积/速度积分，以及独立面积/多项式/复双线性与共轭积分。--resume 复用现有完整结果，不会重新提交已完成的算例；PID 必须取当前实际打开的 CST。所有失败验收均保留，报告完成不代表公式通过。
'''
    for key, rows in [('VALUES', values), ('RAW', raw_table), ('TABLE', table), ('SLOPES', slopes), ('SAMPLING', sampling_table), ('PORT', port_table)]:
        document = document.replace('\n'+key+'\n', '\n'+'\n'.join(rows)+'\n')
    (ROOT/'docs/s11_shape_derivative_cross_case_2026-11-12.md').write_text(document, encoding='utf-8')
    print('12 valid evaluations, 2 failed forward derivative stability gates; report generated')


if __name__ == '__main__':
    main()
