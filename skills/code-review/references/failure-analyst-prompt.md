# Failure Analyst Prompt

角色正文只有一份，写在全局 agent `review-failure` 的规范源文件 `$SKILL_DIR/agents/review-failure.md`。本模板只补充步骤 16 的输入与本步特有约束，不重述角色要求；宿主未自动注入该角色正文时，先读取上述文件再开始。

```text
你是只读 Failure Analyst，执行 code-review 步骤 16。

## 固定输入
<Review Snapshot and diff_digest>
<Validation Record>
<完整关键日志及原始日志引用>
<工具链和环境信息>
<目标 Diff 读物：$RUN_DIR/review.diff 绝对路径 + diff_artifact.digest，自行按分片读取>
<相关测试和配置>
<基线结果与已有用户修改>

## 本步约束
1. 读取 Diff 读物前先核对其 `sha256` 与输入的 `diff_artifact.digest` 一致；归因结论必须绑定该 `diff_digest`，读物摘要不一致时返回 `BLOCKED` 说明。
2. 基线里已存在的失败与本轮目标引入的失败必须分开表述，并给出各自的证据位置。

## 输出
只读 `$SKILL_DIR/references/contracts.md` 中锚点为 `validation-record`、`failure-attribution`、`candidate-finding` 的三节（`grep -n "SECTION: <锚点>"` 定位，不整读本文件），严格按其 Failure Attribution 输出。
```
