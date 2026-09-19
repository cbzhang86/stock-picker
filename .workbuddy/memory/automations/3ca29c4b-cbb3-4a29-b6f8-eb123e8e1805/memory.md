# 自动化执行记录

## 2026-09-07 08:30 周度因子健康检查
- 命令：`python scripts/evaluate_all.py --full`（Python 3.11 系统路径）执行成功，退出码 0。
- 结果：门禁 10 通过 / 0 失败，全部通过 ✅
- 关键指标：OOS 面板 43820 行，hot_theme hold1d IC = +0.0447（有效）。
- 注意事项：bash 中反斜杠路径会被吞，需用正斜杠；stderr 有 numpy std=0 的 RuntimeWarning（除零告警，属相关性计算中的常数列，不影响门禁结果）。
- 无失败项，无需修复。

## 2026-09-14 08:30 全量 OOS 因子健康诊断（自动化）
- 门禁 `evaluate_all.py --full`：10/10 通过 ✅，面板 44309 行，hot_theme 全样本 IC=+0.0453（门禁口径）。
- 深度逐因子 OOS（RET_HOLD1D，全因子面板，988 只/48 日）：检出异常——momentum/technical/volume_price OOS IC 为负（反向，权重方向与数据相悖）；capital_flow、dragon_tiger 训练/测试 IC 正负反转（不稳定）；hot_theme 全样本+0.0453 但 OOS 测试 IC 仅 +0.0101（噪声）；valuation_fundamental 无历史数据（样本不足）；event_catalyst +0.0041（噪声）。
- 结论：只读诊断，**维持现权重**，未改权重、未下单。
- 已推送微信 ClawBot（含门禁通过+异常因子清单+关键 IC/ICIR，默认维持现权重）。
- 报告：`reports/oos_health_2026-09-14.md`。
