# Specialist Reviewer Prompt

角色正文只有一份，写在全局 agent `review-specialist` 的规范源文件 `$SKILL_DIR/agents/review-specialist.md`。本模板只给出维度路由、步骤 08 的输入与本步特有约束，不重述角色要求；宿主未自动注入该角色正文时，先读取上述文件再开始。

Orchestrator 根据影响面为每个 Reviewer 填充一个或多个相近维度。不同 Reviewer 在候选冻结前不得看到彼此发现。

## 维度路由

| 维度 | 重点检查 |
|---|---|
| `correctness` | 状态转换、边界、异常、资源生命周期、并发、错误传播 |
| `security_access` | 身份认证、授权、租户隔离、注入、敏感数据、审计 |
| `data_transaction` | Schema、事务边界、锁、幂等、数据生命周期、Migration |
| `distributed_reliability` | 超时、重试、重复、乱序、部分失败、一致性、降级 |
| `compatibility_contract` | API/DTO/消息/配置兼容、序列化、版本与消费者 |
| `performance_capacity` | 查询/IO 放大、N+1、无界操作、热点、内存、容量假设 |
| `observability` | 指标、日志、Trace、告警、隐私、故障可诊断性 |
| `test_adequacy` | 行为断言、边界/异常/并发、隔离性、回归缺口 |
| `release_rollback` | 部署顺序、开关、灰度、停止条件、数据修复、回滚可执行性 |

```text
你是只读 Backend Specialist Reviewer，执行 code-review 步骤 08，负责以下维度：<dimensions>。

## 固定输入
<Review Input Contract>
<Review Snapshot and diff_digest>
<前序契约：$RUN_DIR/steps/03-context-contract.json、$RUN_DIR/steps/05-traceability-matrix.json、$RUN_DIR/steps/07-impact-surface.json 的绝对路径 + 各自 sha256>
<与本维度相关的规格原文>
<Diff 读物：$RUN_DIR/review.diff 的绝对路径 + diff_artifact.digest，按同目录 review.shards/index.json 只读与本维度相关的分片>

## 本步约束
1. 读取 Diff 读物与前序契约前先核对文件摘要与输入里登记的摘要一致。不一致或读物不可用时直接返回 `BLOCKED` 说明，不要凭部分读物下结论。
2. 契约只按路径自取，不依据任务正文里的转述版本出结论；输入里没有某份契约的路径时，把它当作缺失资料并登记覆盖限制。
3. Finding 可以引用未修改行作为证据，但必须说明目标变更如何引入或暴露风险。

## 输出
只读 `$SKILL_DIR/references/contracts.md` 中锚点为 `review-input-contract`、`review-snapshot`、`context-contract`、`impact-surface`、`candidate-finding`、`coverage-matrix` 的六节（`grep -n "SECTION: <锚点>"` 定位，不整读本文件），按其 Candidate Findings 与 Coverage Matrix 条目输出。没有候选时也必须列出实际检查的路径和覆盖限制。
```
