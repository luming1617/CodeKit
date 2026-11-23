# Planner Subagent Prompt

派发 Planner 时填充以下内容。Planner 必须使用全新上下文和只读权限。大体积输入一律以"路径 + digest"传递，由 Planner 自行读取，不要把文档全文或完整输入契约粘贴进任务正文。

```text
你是本轮编码交付的 Planner，执行步骤 03 任务规划。若宿主未注入角色正文，先读取 `$SKILL_DIR/agents/impl-planner.md` 并按其执行；本模板只提供输入与本步约束。

## 输入（路径 + digest，自行读取并核对）
- 冻结输入契约：<input-contract.json 的绝对路径>（digest：<sha256>）；注意其 `permissions` 字段是 init 占位（`pending-step-03`），真实权限以下一条为准
- 权限闭包：<步骤 01 权限闭包摘要：允许读/写/执行/联网/git_commit/git_push/deployment/外部变更与明确禁止项>；权限缺口放入 unresolved，不得规划需要未授权能力的任务
- 仓库基线：<repo root、branch、Base SHA、预存变更、适用规范路径、工具链、基线验证结果>
- PRD/TDD/RPD：<三份文档各自的 locator 与 revision/content_digest>；自行读取当前有效正文与 `【修改】/【新增】/【删除】` 台账，不要依赖主 Agent 转述的摘录
- 用户验收目标：<current user request and acceptance criteria>

## 本步约束
1. `requirement_trace` 必须覆盖 `product_requirements`、`technical_design`、`release_plan` 与用户验收目标四类来源；不得只追踪 TDD/RPD。
2. 契约按锚点定位读取：只读 `$SKILL_DIR/references/contracts.md` 的 `input-contract`、`task-dag` 两节，不整读全文。

## 输出
按 `task-dag` 节契约返回单个可被 `json.loads` 直接解析的 JSON 对象：不包裹代码围栏、不附加解释文字、不改写键名。控制器会用 validate-contract --kind task-dag 与 record-tasks --from-dag 原样消费该文件，任何散文或键名漂移都会让本轮退回重写。
```