# 获批后的实施与验证

只有存在有效 Approval Record 时读取和执行本文件。

本文件里的范围、验证和漂移结论均以 `$SKILL_DIR/scripts/bugfix_state.py` 的退出码与 JSON 输出为准；脚本无法执行时对应阶段为 `BLOCKED`，不得改用自然语言自述。

## 1. 实施前重新确认

用**一条** `verify-gates` 连跑审批绑定与快照漂移两项门禁（它等价于依次执行 `verify-approval` 和 `check-snapshot`，分开跑只会多付一次模型往返）：

```bash
<PYTHON> "$SKILL_DIR/scripts/bugfix_state.py" verify-gates --manifest "$RUN_DIR/state.json" \
  --approval-file "$RUN_DIR/approval.txt" --proposal "$RUN_DIR/proposal.md"
```

在它的 `approval.checks` 与 `snapshot` 字段里核对：

- 当前 `HEAD SHA`、工作树摘要和文档摘要是否仍与提案绑定快照一致；
- 预先存在的用户修改是否变化；
- 所有计划文件都在审批和运行时写权限内；
- 验证工具、依赖和环境是否可用；
- 是否需要尚未授权的联网、安装、Migration、数据修复或外部变更。

任何实质漂移先暂停。只读证明为无关变化后才能继续，否则更新提案并重新审批。只有 `proposal_digest_match` 单项失败时，才按 SKILL.md 步骤 11 的 `--editorial` 规则判定是否属于编辑性修订。

## 2. 实施顺序

1. `verify-gates` 通过后用 `freeze-contract --manifest "$RUN_DIR/state.json"` 冻结 Implementation Contract 与验证计划；允许范围、起始摘要、禁止项和验证命令都从提案取，不手抄。
2. 在不修改生产逻辑的条件允许时，先添加或确认最小回归测试，并证明修复前能暴露问题。
3. 在根因所属层实施最小修复。
4. 用 `bugfix_state.py verify-scope --manifest "$RUN_DIR/state.json" --contract "$RUN_DIR/implementation-contract.json"` 检查实际 Diff 和修改文件范围。
5. 用 `bugfix_state.py run-plan --manifest "$RUN_DIR/state.json"` 一次跑完整份验证计划（回归、模块、契约），只对计划外新增的检查补用 `run-validation`。
6. 独立复核最终快照。

如果无法安全地先运行失败测试，说明原因，并使用代码路径或其他 E2/E3 证据替代，不能伪造 red-green 过程。

## 3. 分层验证

按适用性执行：

- **L0 范围**：实际文件、用户已有修改、调试残留、敏感信息；
- **L1 静态**：格式、编译、类型、Lint、静态分析；
- **L2 回归**：原触发路径修复前失败、修复后通过；
- **L3 模块**：相关单元和集成测试；
- **L4 契约**：API、Schema、消息、配置和混合版本兼容；
- **L5 非功能**：并发、事务、幂等、性能、容量、安全或故障注入；
- **L6 发布**：构建产物、配置、灰度、监控、回滚和数据修复检查。

提案第 5 节列出的必需项由 `run-plan` 顺序执行；计划外的补充项用 `run-validation` 逐条执行。两者都把结果追加到 `RUN_DIR/validation-record.json`，记录 `PASS`、`FAIL`、`TIMEOUT`、`NOT_AVAILABLE` 或 `NOT_RUN`；后三者不得当作通过，并必须说明原因和剩余风险。`proves` 写具体主张，不写“全部通过”。`run-plan` 只在 stdout 回显摘要与退出码，命令输出按 `--tail-lines` 只留尾部进记录，不要为了看日志把记录整份读回。

## 4. 独立复核

复核由宿主已注册的 `codekit:code-review` 执行（审查模式与 Handoff 契约见 contracts.md 第 11 节）。它必须直接读取冻结的最终 Diff、相关实现和测试，不把 Implementer 摘要当事实。至少确认：

- 修复与已证明根因存在直接因果闭环；
- 未使用吞错、无限重试、扩大超时或关闭校验来隐藏问题；
- 实际修改没有越过审批范围；
- `behavior_to_preserve` 仍成立；
- 回归测试真正断言行为而非只执行代码；
- 兼容性、数据和 RPD 风险已处理；
- 没有引入新的阻塞缺陷。

## 5. 重试和重新审批

- 契约内实现或测试错误：根据新证据修复；错误指纹由 `run-validation` 计算，相同指纹连续 3 次时脚本返回退出码 3，必须停止并报告。
- 诊断错误或根因变化：回到只读调查，发布新版本提案并重新审批。
- 新增修改文件、改变 API/Schema/消息/配置语义、增加依赖、数据修复或发布策略：重新审批。
- 环境、权限或已有用户修改冲突：停止并请求最小必要决策。

## 6. 完成条件

同时满足以下条件才能报告修复完成：

1. 实际 Diff 完全处于批准范围；
2. 原问题的回归证据通过；
3. 所有必需验证通过且没有未解释的新失败；
4. 宿主已注册的 `codekit:code-review` 返回当前审查模式下的可接受结论（`FULL`：`APPROVE` 或 `APPROVE_WITH_FOLLOW_UPS`；`LITE`：仅 `APPROVE_WITH_FOLLOW_UPS`），且快照与 Diff digest 一致；`WAIVED` 只能按用户批准豁免上报为未独立复核，不得声称本项已满足；
5. 未运行项和剩余风险已明确披露；
6. 最终报告能让用户通过 SHA、Diff、命令和退出码复核。
