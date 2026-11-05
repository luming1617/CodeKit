# Release Gate

Release Gate 是只读发布准备度评估，不执行部署或回滚。只使用冻结 RPD、目标 Diff、部署资源、项目规范和验证证据；`DOCUMENTS: NOT_AVAILABLE` 时没有冻结 RPD，改用 Diff、部署资源、仓库规范和验证证据评估，不得臆造发布方案；缺少事实时使用 `NOT_VERIFIED`，不得用常见做法补齐。

## 检查项

| Item | 检查内容 |
|---|---|
| `deployment_order` | 服务、Schema、配置、消息、任务和依赖的先后关系 |
| `dependencies` | 内外部依赖版本、SLA、协调与前置条件 |
| `configuration` | 环境差异、默认值、敏感项引用、生效与恢复方式 |
| `feature_flag` | 开关归属、默认状态、启停权限、失败回退 |
| `rollout` | 灰度/滚动批次、流量策略、健康检查与扩大条件 |
| `stop_conditions` | 明确指标、阈值、超时和停止动作 |
| `monitoring` | SLI、日志、Trace、告警、看板、责任人和证据入口 |
| `observation_window` | 观察时长、业务周期覆盖和结束判据 |
| `data_repair` | 部分成功、脏数据、补偿、重跑、幂等与审计 |
| `rollback` | 可回滚产物、顺序、兼容窗口、数据处理和验证 |

## 状态规则

- `PASS`：有可复核证据证明该项可执行。
- `CONDITIONAL`：有明确、可验证且不阻止代码合入的前置条件。
- `FAIL`：发布计划与目标变更矛盾、缺少必要动作或回滚不可执行。
- `NOT_VERIFIED`：需要的系统、环境、凭据或资料不可访问。
- `NOT_APPLICABLE`：该变更确实不涉及此项，并有代码/架构/RPD 证据；`condition_or_gap` 必须写明"为什么不适用"，不得留空。

每项还必须登记 `criticality`：`critical` 表示该缺口未核实就让发布决策不可信（回滚、数据修复、停止条件、部署顺序这类），`non_critical` 表示可作为交付后跟进的条件。

总体状态：

- 任一目标变更导致的关键 `FAIL` → `NOT_READY`。
- 无 `FAIL`，但存在 `critical` 项 `NOT_VERIFIED` → `NOT_VERIFIED`（结论只能是 `BLOCKED`）。
- 无 `FAIL` 且无关键 `NOT_VERIFIED`，但存在可管理的 `CONDITIONAL` 或 `non_critical` 的 `NOT_VERIFIED` → `READY_WITH_CONDITIONS`，条件写进 `conditions`。
- 无 `FAIL/CONDITIONAL/NOT_VERIFIED` 且所有适用项为 `PASS` → `READY`。
- 审查目标不产生可部署或运行时变化，且证据充分 → `NOT_APPLICABLE`，但十项清单仍需逐条以 `NOT_APPLICABLE` + 证据 + 理由登记，不得整体省略。

输出严格使用 contracts.md 的 Release Gate 结构，并保留每个条件、阻塞原因和证据。
