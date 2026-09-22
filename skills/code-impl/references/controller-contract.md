# 控制器命令手册

`scripts/code_impl_controller.py` 的全部子命令，一命令一行。**这是主控的就近发现面**：需要"下一步敲什么"时读这里，不要回读控制器源码（2600+ 行）。

约定：

- `<PYTHON>` 按宿主的解析规则（见 `$SKILLS_ROOT/_shared/prompts/path-convention.md`）。
- `--state` 恒为运行目录里的 `state.json` 绝对路径。
- 每条命令只做一件事，返回值都是有界摘要；完整清单落在 `scope-checks/` 等独立文件里，**不要**把 `state.json` 整份读回上下文。
- 失败一律 `ControllerError`（退出码 2）并打印可行动的原因；退出码 2 意味着"不得绕过"，不是"可以重试到成功"。

新增命令必须同时加进本文件——`test_every_subcommand_has_a_line_in_the_manual` 用反射比对 `build_parser()` 与本文件，漏写会直接失败。

## 目录

- `init` — 冻结输入，建立运行目录
- `validate-contract` — 校验契约文件（input / task-contract / task-dag / failure / handoff）
- `record-agent` — 登记或收口一个 Agent 会话（只登记生命周期，不含结论）
- `record-task` — 登记单个任务及其写入范围
- `record-tasks` — 从 DAG 文件批量登记全部任务
- `update-task` — 按状态机推进任务状态（含 `RUNNING → PENDING` 作废）
- `dispatch-task` — 派发：状态推进 + Implementer 登记，一次写入
- `check-scope` — 任务绑定的范围检查（只诊断，不改变状态）
- `expand-scope` — 带理由扩充任务写入范围
- `settle-task` — 判定：范围检查 + `VALIDATING → PASS|FAILED`
- `record-validation` — 登记一条验证证据（命令、退出码、证据、工作树指纹）
- `record-validations` — 从 JSON 文件批量登记整轮验证（一次状态写入）
- `record-failure` — 登记一次失败归属（根因 + 错误指纹）
- `record-failures` — 从 JSON 文件批量登记多次失败归属（一次状态写入）
- `replan` — 推进计划版本并取代未完成的旧版本任务
- `freeze-review` — 冻结审查快照
- `record-review` — 登记 `codekit:code-review` 的结论
- `render-ledger` — 从 `state.json` 渲染 `task-ledger.md` 托管块
- `show` — 有界视图：当前位置、预算、漂移
- `gate` — 交付门禁，退出码就是"能否宣称编码完成"

## 命令

- `init --repo-root <path> --feature-directory <path> --run-id <id> [--allow-lossy-decoding]` — 校验三份文档、记录 Base SHA/分支/digest/预存快照，建立 `.codekit/code-impl/<run-id>/`；返回 `state.json` 路径。运行目录已存在时拒绝（resume 而非覆盖）；文档非 UTF-8 时给出检测到的编码。
- `validate-contract --kind <kind> <path>` — 机械校验契约文件；`--kind` 取 `input|task-contract|task-dag|failure|handoff`。退出码非 0 不得继续。
- `record-agent --state <s> --role <r> --name <n> --session-id <id> --input-digest <d> --status <st>` — 登记/收口 Agent；`--status` 只接受 `RUNNING|DONE|ARCHIVED|FAILED|BLOCKED`，**不接受结论值**（`APPROVE` 之类走 `record-review`）。同一 session 的角色名与 digest 不可变更。
- `record-task --state <s> --task-id <id> --allowed-file <rel> [--plan-version <n>]` — 登记任务与写入范围（`--allowed-file` 可重复）；`--plan-version` 默认当前版本，重规划后必须显式传新版本。
- `record-tasks --state <s> --from-dag <path>` — 校验并一次登记整份 DAG，返回 `registered/already_present/tasks_total`；幂等，任何一条不合法则整份不写入。
- `update-task --state <s> --task-id <id> --status <st> [--reason <why>]` — 按 `TASK_STATE_TRANSITIONS` 推进；仅 `RUNNING → PENDING`（派发作废）要求 `--reason`：撤销本次派发的预算记账（`attempts` 不增长），`attempts_log` 该条标 `abandoned` 与原因，任务累计 `abandoned_dispatches`；同一任务作废上限 `MAX_TASK_ABANDONS=3` 次，第 4 次被拒——只能 `settle-task FAILED`（带指纹）或 `BLOCKED`。作废后须补 `record-agent --status ARCHIVED` 收口死会话，否则 `gate` 以 `dispatched-agents-closed` 拒绝交付。**不得用于回避失败**——已判定失败的任务只能 `settle-task FAILED` + `record-failure`。
- `dispatch-task --state <s> --task-id <id> --session-id <id> --input-digest <d> [--retry-policy gated|prose]` — `PENDING → READY → RUNNING`（或 `FAILED → RUNNING` 重试）并原子登记 Implementer；`gated`（默认）要求存在不早于本次失败的 `record-failure` 且未升级人工介入；`prose` 是回退开关，会在返回体标注、必须在报告披露。绝对上限 `MAX_TASK_ATTEMPTS=6`。
- `check-scope --state <s> --task-id <id>` — 任务绑定范围检查，授权集合由控制器推导（本任务范围 ∪ 已 PASS 任务范围），不接受调用方声明文件；只诊断、不改状态，返回有界摘要。
- `expand-scope --state <s> --task-id <id> --add-file <rel> --reason <why>` — 扩充非终态任务的写入范围（`--add-file` 可重复）；理由进台账与报告。终态任务不可扩权。
- `settle-task --state <s> --task-id <id> --status PASS|FAILED [--error-fingerprint <fp>] [--note <t>]` — 先推进到 `VALIDATING`，执行一次范围检查并落盘，再落定结论。`PASS` 在范围 BLOCKED 时被拒（停在 `VALIDATING`）；`FAILED` 必须带指纹（重试预算的唯一键）。
- `record-validation --state <s> --scope task|overall --name <n> --command <cmd> --cwd <dir> --result PASS|FAIL|NOT_RUN [--task-id <id>] [--exit-code <n>] [--evidence <t>|--evidence-file <path>] [--not-run-reason <t>]` — 登记一条证据并绑定当前工作树指纹。`PASS` 须 `--exit-code 0` 且有证据；`FAIL` 须非 0；`NOT_RUN` 须理由且不带退出码。整体验证不接受 `--task-id`。
- `record-validations --state <s> --from-file <path>` — 批量面：一次登记整轮验证（步骤 12 的全量测试/编译/Lint/契约/Migration/构建）。文件为 `{"validations": [...]}` 或裸数组，字段与 `record-validation` 同名（`scope`/`name`/`command`/`cwd`/`exit_code`/`result`/`task_id`/`evidence`/`evidence_file`/`not_run_reason`/`started_at`/`finished_at`）。逐条仍走同一套规则；任一条不合法则**整份拒绝**并点名 `validations[N]`，状态文件不变。整批共用一次工作树指纹。
- `record-failure --state <s> --failure-scope task|overall --root-cause <rc> --error-fingerprint <fp> [--impacted-task-id <id>]` — 登记失败归属；整体失败无 `--impacted-task-id` 时记录 `BLOCKED_MISSING_TASK_MAPPING`，多任务影响记 `REPLAN_MULTI_TASK_IMPACT`（须 `replan`）。同一指纹按归一化形式计数，第 5 次出现强制 `human_intervention`。
- `record-failures --state <s> --from-file <path>` — 批量面：文件为 `{"failures": [...]}` 或裸数组，字段 `failure_scope`/`impacted_task_ids`/`root_cause`/`error_fingerprint`。逐条走同一套规则，坏条目点名 `failures[N]` 并整份拒绝；批内同指纹按先后顺序正确累计出现次数（预算不会因批量而失效）。
- `replan --state <s> --new-plan-version <n>` — 递增计划版本，把旧版本中 `PENDING|READY|FAILED|BLOCKED` 的任务标为 `SUPERSEDED`（`RUNNING`/`VALIDATING` 须先处理完），重算 `authorized_files` 只保留 PASS 任务范围；返回 `superseded_tasks` 与单独列出的 `superseded_blocked`。
- `freeze-review --state <s>` — 冻结审查快照（写 `review-snapshot.json`）；最多 `MAX_REVIEW_FREEZES=3` 次（首次 + 2 轮审查修复），第 4 次被拒并须进入步骤 17。
- `record-review --state <s> --verdict <v> --report-path <p> --validator-exit-code <n> [--summary <t>]` — 登记审查结论，绑定最后一次冻结的 Diff 指纹；`--verdict` 取 `APPROVE|APPROVE_WITH_FOLLOW_UPS|REQUEST_CHANGES|BLOCKED`。未冻结或报告文件不存在时拒绝。
- `render-ledger --state <s> [--output <path>] [--print]` — 从 `state.json` 渲染 `task-ledger.md` 托管块（幂等，保留块外手写内容，块内不得手写）；脚注含验证/失败/范围检查/冻结/作废计数。
- `show --state <s> [--tasks]` — 有界视图：阶段、任务状态分布、未完成/未收口（`open_agents` 列出仍为 `RUNNING` 的 `role@session`）、预算（冻结次数、prose 重试数、作废派发数）、验证与失败计数、三份文档是否漂移（`drifted_materials`）。**会话中断后恢复以此为准**；`--tasks` 追加每行一条台账行。
- `gate --state <s> [--mode enforce|warn-only]` — 逐条核对交付条件（当前计划任务全 PASS、任务级/整体验证齐备且与冻结指纹一致、审查结论可交付且绑定最后一次冻结、报告文件在位、**所有派发已收口**、最终范围干净），写 `gate-report.json`；退出码 2 = 不得宣称编码完成。`warn-only` 仅供排障，返回体仍标 `BLOCKED`。

## 预算（机械执行，不由主控自评）

| 预算 | 上限 | 由谁计数 |
|---|---|---|
| 同一错误指纹重试 | 3 次（第 5 次出现强制人工介入） | `record-failure` 归一化计数 |
| 单个任务累计派发 | 6 次（`MAX_TASK_ATTEMPTS`） | `dispatch-task`；作废不计数 |
| 审查冻结 | 3 次（`MAX_REVIEW_FREEZES`） | `freeze-review` |
| 派发作废 | 每任务 3 次（`MAX_TASK_ABANDONS`），不占派发预算 | `update-task --status PENDING`；超限只能 FAILED 或 BLOCKED |
