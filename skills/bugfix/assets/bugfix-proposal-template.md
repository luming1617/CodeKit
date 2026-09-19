# 后端 Bugfix 调查与修复方案

## 1. 问题判定

- **判定**：`CONFIRMED_BUG | NOT_A_BUG | INSUFFICIENT_EVIDENCE`
- **置信度 / 证据等级**：`High | Medium | Low` / `E3 | E2 | E1 | E0`
- **审查快照**：`<repository, branch, HEAD SHA, working-tree digest>`
- **期望行为**：`<PRD/TDD/项目规范的版本、章节和明确约束>`
- **实际行为**：`<可复核现象>`
- **差异**：`<期望与实际的最小差异>`

## 2. 问题原因

### 症状与触发条件

- **症状**：`<用户或系统观察到的结果>`
- **触发条件**：`<前置状态、输入、并发、配置、依赖状态>`

### 根本原因

`<精确说明错误决策或状态如何违反业务/技术不变量>`

### 证据链

1. `<文档/代码/日志/命令/测试证据>`
2. `<从输入到症状的因果步骤>`

### 排除的替代假设

| 假设 | 结论 | 反证 |
|---|---|---|
| `<H01>` | `Rejected/Supported/Insufficient` | `<evidence>` |

<!-- bugfix:proposal-required-start -->
<!-- 上面这个锚点注释由 scripts/validate_bugfix_proposal.py 使用：它之前的章节任何判定都必须填， -->
<!-- 之后的章节只在判定为 CONFIRMED_BUG 时强制校验。删除该注释会让校验器直接报错。 -->

## 3. 修复方案

- **方案 ID**：`<BF-YYYYMMDD-short-name>`
- **方案版本**：`<integer>`
- **状态**：`WAITING_APPROVAL`
- **推荐方案**：`<在正确责任层消除根因的方案>`
- **选择理由**：`<正确性、影响面、成本、兼容性、可验证性>`
- **绑定快照**：`<HEAD SHA + working-tree/document digest>`

### 计划修改

| 文件/符号 | 计划变化 | 原因 |
|---|---|---|
| `<repo-relative-path:symbol>` | `<change>` | `<root-cause relation>` |

- **允许修改文件**：`<exact repo-relative paths>`
- **明确不修改**：`<模块、接口、Schema、配置、依赖、业务行为>`
- **必须保持的行为**：`<business invariants and negative scope>`
- **备选方案及未选择原因**：`<alternatives>`

## 4. 影响面与风险

每一维都要有结论，但结论可以是短的：本维确实不受影响时，`影响` 写 `不适用：` 后接一句话理由，`风险与缓解` 写破折号即可；不要为凑格编造影响，也不要把“无风险”当作理由。

| 维度 | 影响 | 风险与缓解 |
|---|---|---|
| 业务逻辑 | `<...>` | `<...>` |
| API与兼容性 | `<...>` | `<...>` |
| 数据、事务与并发 | `<...>` | `<...>` |
| RPC、消息与缓存 | `<...>` | `<...>` |
| 安全与隐私 | `<...>` | `<...>` |
| 性能与容量 | `<...>` | `<...>` |
| 可观测性 | `<...>` | `<...>` |
| 发布与回滚 | `<...>` | `<...>` |

- **修复成本**：`<implementation/test/release estimate>`
- **剩余风险**：`<residual risks>`

## 5. 验证计划

- **回归测试**：`<修复前失败、修复后通过的场景>`

`freeze-contract` 会把本表按行渲染成 `validation-plan.json`，所以 `命令/方法` 里必须写**一条**可直接 argv 化的命令（如 `py -3 -m unittest discover -s tests`）：不要写 `&&`、`|`、`>`、`$()` 等 shell 组合，那些行会被判 `NOT_AVAILABLE` 而不是执行。需要多步就拆成多行；只能人工做的检查写方法并把 `必需` 填 `No`。

| 检查 | 命令/方法 | 证明内容 | 必需 |
|---|---|---|---|
| `<check>` | `<command or method>` | `<claim>` | `Yes/No` |

- **人工验收**：`<仅列自动化无法证明的行为>`

## 6. 人工审批

- **审批状态**：`WAITING_APPROVAL`
- **审批范围**：仅限本报告列出的方案版本、绑定快照、允许修改文件和验证计划。
- **审批口令**：`批准执行 <proposal_id> v<proposal_version>，按当前绑定快照和允许修改文件实施。`
- **提示**：批准不自动授权 commit、push、部署、真实 Migration、数据修复或外部系统变更。
