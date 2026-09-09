# Release Plan Review Contract

本契约只适用于 `release-doc` 的独立发布方案评审，不替代 `code-review`，也不允许评审者审查代码实现。

## 输入封套

Release Plan Reviewer 必须收到并记录以下冻结输入：

```text
review_id
feature_directory
prd:              <feature-directory>/product-doc.md
tdd:              <feature-directory>/technical-doc.md
rpd_draft:        <feature-directory>/release-doc.md 或 Runbook Refiner 修订版
release_template: references/release-doc-template.md
check_standard:   references/check-standard.md
input_digests:    每份输入的路径、更新时间/版本和内容摘要
```

缺少 PRD、TDD、RPD 草案、模板或本契约时，评审必须返回 `BLOCKED`，不得按经验补齐。

## 循环边界

主控维护 `review_round`，从 1 开始，每次独立 Reviewer 重跑前递增；完整的“评审 → Refiner 修改 → 主控复核”循环最多 5 轮。第 5 轮后仍有阻塞 Finding、状态为 `BLOCKED`、输入快照不一致或 Reviewer 不可用时，最终状态必须为 `BLOCKED`，不得继续调度新的 Reviewer。

## 评审范围

只检查以下发布方案属性：

1. 模板章节、表头、状态值和条件章节是否完整且顺序一致；
2. 服务、产物、接口、基础设施、依赖和业务影响是否有来源证据；
3. 基础设施前置操作、服务发布顺序、灰度/滚动策略是否可执行且前后一致；
4. 发布前检查、发布后验证、观察指标、基线、阈值、时长和责任人是否覆盖；
5. 失败停止、隔离、降级、恢复、应用回滚、配置/数据/消息/任务回滚是否可执行；
6. 未确认信息是否被错误写成通过、正常、成功、已发布或不涉及；
7. 高风险业务、不可逆变更和无标准回滚路径是否被正确阻塞或升级。

明确不在范围内：代码正确性、代码 Diff、测试实现质量、架构设计重审、真实发布操作、外部系统修改和 `codekit:code-review` 调用。

## 输出契约

输出必须是 JSON 或等价结构化 Markdown，并包含以下字段：

```text
review_id: <与输入一致>
contract_version: release-review-v1
status: APPROVE | APPROVE_WITH_FOLLOW_UPS | REQUEST_CHANGES | BLOCKED
scope: release_plan_only
input_digests: <与输入封套一致>
summary: <评审范围、证据覆盖和结论>
findings:
  - id: RPR-001
    severity: Blocker | High | Medium | Low
    category: TEMPLATE | EVIDENCE | ORDER | VERIFICATION | OBSERVATION | ROLLBACK | RISK
    location: <章节、表格、步骤或字段>
    evidence: <输入中的事实依据；无依据时明确写未发现>
    impact: <对发布、人、数据、可用性、安全或成本的影响>
    remediation: <可执行修复或人工补充>
    blocking: true | false
    verification: <修复后的验证方式>
open_questions:
  - id: RQ-001
    question: <待人工确认事项>
    impact: <影响的放行判断>
    owner: <未知时写需人工指定>
release_gate:
  blockers: <未关闭 Blocker/High 数量>
  unresolved: <未关闭 Finding/Open Question 数量>
  can_finalize: true | false
limitations: <缺失资料、无法访问的系统和未覆盖范围>
```

## 判定规则

- `APPROVE`：无未关闭 Blocker/High，模板、事实、顺序、验证和回滚均满足契约。
- `APPROVE_WITH_FOLLOW_UPS`：没有阻塞问题，但存在明确登记的 Medium/Low 或非放行性跟进项。
- `REQUEST_CHANGES`：存在可通过修改发布方案解决的 Blocker/High/Medium 问题。
- `BLOCKED`：输入缺失、快照不一致、契约无法验证、独立评审无法完成，或需要外部事实/权限才能判断。

评审者不得把“无法访问”转换为“通过”，不得删除问题来降低严重度，不得输出代码审查结论。
