"""product-doc 门禁脚本与资产契约的回归测试。

覆盖审查报告中的 P0 缺陷类别：

* P0-1 评审状态与严重度只有契约一个事实源；
* P0-2 来源与追踪台账、4.1 关联验收列可机械校验；
* P0-3 变更历史快照外置且只追加，内嵌围栏必须报错；
* P0-4 结构与轮次由脚本判定，不依赖 Agent 自述。
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import review_state  # noqa: E402
from validate_prd import PrdValidator, check_template_refs  # noqa: E402

SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
TEMPLATE = (SKILL_DIR / "references" / "product-doc-template.md").read_text(encoding="utf-8")
STANDARD = (SKILL_DIR / "references" / "check-standard.md").read_text(encoding="utf-8")
CONTRACT = (SKILL_DIR / "references" / "product-review-contract.md").read_text(encoding="utf-8")
AGENTS = {path.stem: path.read_text(encoding="utf-8") for path in sorted((SKILL_DIR / "agents").glob("*.md"))}

# 9.3 台账必须覆盖的全部编号（SRC 只在台账定义，F 随父功能，PR 属于评审产物）。
LEDGER_IDS = (
    "OBJ-001", "KPI-001", "KPI-002", "SC-001", "FR-001", "BR-001", "ST-001", "AF-001",
    "UI-001", "EVT-001", "AC-001", "AC-002", "DEP-001", "RISK-001", "Q-001", "DEC-001",
)

PRD_FIXTURE = """# 下单页挽留产品需求说明书

# 1. 文档信息（必填）

## 1.1 基本信息

| 项目 | 内容 |
|------|------|
| 需求名称 / PRD 编号 | 下单页挽留 / PRD-ORDER-2026-001 |
| 文档版本 / 状态 | 0.1 / 评审中 |
| 需求规模 / 优先级 | S / P0 |
| 负责人 | 产品张三、研发李四、测试王五 |
| 关联工单 / 目标版本 | ISSUE-2049 / 2026-10 |
| 创建日期 / 最后更新日期 | 2026-09-01 / 2026-09-09 |

## 1.2 变更记录

| 版本 | 日期 | 修改人 | 变更类型 | 变更内容 | 影响的编号 | 确认人 |
|------|------|--------|----------|----------|------------|--------|
| 0.1 | 2026-09-09 | 张三 | 新增 | 初稿 | 首次创建 | 张三 |

# 2. 背景与目标（必填）

## 2.1 问题与机会

- 现状流程与产品形态：买家进入下单页后直接离开，产品无挽回手段。
- 用户遇到的问题及影响规模（含基线数据）：2026-08 下单页离开率 27.4%，日均 12000 次离开。
- 业务影响与现在处理的原因：按 2026-08 客单价折算，可挽回空间为月 GMV 的 3.1%。
- 证据链接：BI 报表 checkout-funnel-2026-08、客服工单 ISSUE-2049。

## 2.2 目标与衡量指标

| 目标编号 | 产品目标 | 目标用户 | 预期结果 | 优先级 |
|----------|----------|----------|----------|--------|
| OBJ-001 | 降低下单页无因离开 | 已登录买家 | 下单页离开率由 27.4% 降至 24.0% 以下 | P0 |

| 指标编号 | 类型 | 指标名称 | 计算口径（分子/分母/去重/排除） | 基线及日期 | 目标值 | 统计周期 | 数据源 | 负责人 |
|----------|------|----------|--------------------------------|------------|--------|----------|--------|--------|
| KPI-001 | 结果指标 | 下单页挽回率 | 弹窗后 30 分钟内支付订单数除以弹窗曝光次数，按订单去重，排除内部账号 | 2026-08 基线 0 | 不低于 4.0% | 连续 30 天 | 见 6.1 EVT-001 | 张三 |
| KPI-002 | 护栏指标 | 下单页投诉率 | 下单页相关投诉工单数除以下单页访问次数，按工单去重 | 2026-08 基线 0.05% | 不得高于 0.07% | 连续 30 天 | 客服工单系统 | 张三 |

## 2.3 范围与非目标

| 类型 | 内容 | 说明 |
|------|------|------|
| 本次范围 | App 下单页与 H5 下单页 | 已登录买家，中国大陆 |
| 非目标 | 购物车挽留与未登录用户挽留 | 本次不做，排入 2026-11 评估 |

## 2.4 前提与依赖

| 类型 | 内容 | 当前状态/依据 | 不成立时的影响 | 编号 | 负责人 | 确认期限 |
|------|------|---------------|----------------|------|--------|----------|
| 业务/政策前提 | 可展示近 30 天未支付订单 | 已确认 | 无可挽回订单，功能不生效 | 不涉及 | 张三 | 2026-09-15 |
| 外部团队/系统依赖 | 消息中心提供站内下发通道 | 进行中 | 无法在离开后触达用户 | DEP-001 | 李四 | 2026-09-20 |

# 3. 用户与使用场景（必填）

## 3.1 角色与场景

| 场景编号 | 角色 | 使用场景与动机 | 触发条件 | 用户目标 | 期望结果 | 频次 | 失败影响 |
|----------|------|----------------|----------|----------|----------|------|----------|
| SC-001 | 已登录买家 | 比价后准备离开下单页 | 停留满 20 秒且点击返回 | 确认是否更划算 | 看到可用优惠并决定是否继续 | 日均 12000 次 | 用户流失，无挽回记录 |

## 3.2 角色与操作权限

| 角色 | 可查看数据 | 可执行操作 | 明确不允许 | 数据范围 | 授权来源 |
|------|------------|------------|------------|----------|----------|
| 已登录买家 | 本人未支付订单与可用优惠 | 继续下单、关闭弹窗 | 查看他人订单 | 仅本人 | 登录态与账号体系 |

# 4. 功能需求（必填）

## 4.1 功能清单

| 编号 | 功能名称 | 一句话说明（用户获得什么能力） | 优先级 | 本次范围 | 关联目标 | 关联验收 | 负责人 |
|------|----------|--------------------------------|--------|----------|----------|----------|--------|
| FR-001 | 下单页挽留弹窗 | 放弃下单时看到本人可用的真实优惠 | P0 | 是 | OBJ-001 | AC-001、AC-002 | 李四 |

## 4.2 业务流程

```mermaid
flowchart TD
    start([买家进入下单页]) --> permission{有可展示优惠}
    permission -- 否 --> plain[按原流程下单]
    permission -- 是 --> input[停留满 20 秒并点击返回]
    input --> result{优惠校验结果}
    result -- 成功 --> success[展示弹窗与继续下单入口]
    result -- 可重试失败 --> retry[保留输入并允许重试]
    result -- 不可重试失败 --> support[说明原因与客服入口]
```

| 环节 | 角色 | 输入 | 关键规则 | 输出与反馈 | 异常分支 | 关联编号 |
|------|------|------|----------|------------|----------|----------|
| 触发挽留 | 已登录买家 | 停留时长与返回动作 | BR-001 | 展示弹窗 | AF-001 | FR-001 |

## 4.3 功能详细说明

### FR-001：下单页挽留弹窗

| 项目 | 内容 |
|------|------|
| 解决的问题 | 对应 SC-001 与 OBJ-001 |
| 操作角色 | 引用 3.2 的已登录买家 |
| 前置条件 | 存在近 30 天未支付订单且账号有可用优惠 |
| 触发方式 | 用户点击返回 |
| 完成条件 | 用户继续下单或明确关闭弹窗 |
| 不可逆影响 | 无，仅展示与埋点 |
| 关联页面 / 原型 | UI-001，Figma 版本 2026-09-05 |
| 关联埋点 / 验收 | EVT-001 / AC-001、AC-002 |

**业务规则**

| 编号 | 名称 | 适用条件 | 规则内容（必须/禁止/应该/可以） | 冲突时的优先级 | 示例 |
|------|------|----------|-----------------------------------|----------------|------|
| BR-001 | 挽留触发条件 | 已登录且停留满 20 秒 | 必须同时满足订单与优惠条件才弹窗；同一订单每日最多 1 次 | 高 | 停留 25 秒且点击返回，弹窗 1 次 |

**字段与内容**

| 编号 | 字段 | 类型 | 必填 | 默认值 | 长度/范围/格式 | 空值语义 | 可编辑条件 | 脱敏与权限 | 错误提示 |
|------|------|------|------|--------|----------------|----------|------------|------------|----------|
| F-001 | 优惠文案 | 文本 | 是 | 空 | 不超过 30 字 | 空时不展示文案区 | 只读 | 禁止展示手机号与地址 | 无 |

**业务状态**

| 编号 | 状态 | 进入条件 | 允许操作 | 触发动作 → 目标状态 | 是否可逆 | 用户反馈 |
|------|------|----------|----------|----------------------|----------|----------|
| ST-001 | 已展示待决策 | BR-001 命中 | 继续下单、关闭 | 关闭 → 已放弃 | 是 | 关闭后不再弹出 |

**异常与边界**

| 编号 | 触发条件 | 产品行为 | 用户提示 | 用户下一步 | 重复/并发结果 |
|------|----------|----------|----------|------------|----------------|
| AF-001 | 优惠服务超时 | 不弹窗，保持原下单流程 | 不提示打扰用户 | 继续下单 | 重试不重复计数 |

## 4.4 页面与交互（涉及界面时必填）

| 页面编号 | 页面/弹窗 | 入口与返回 | 核心任务 | 原型链接与版本 | 关联需求 |
|----------|-----------|------------|----------|----------------|----------|
| UI-001 | 挽留弹窗 | 下单页点击返回后出现，关闭回到下单页 | 决策是否继续下单 | Figma P-03，2026-09-05 | FR-001 |

**页面状态**

| 页面/状态 | 触发条件 | 展示与可用操作 | 输入与返回行为 | 文案与提示 |
|-----------|----------|----------------|----------------|------------|
| UI-001-LOADING | 校验优惠中 | 不展示弹窗 | 保留下单页状态 | 无 |
| UI-001-SUBMITTING | 点击继续下单 | 禁用重复点击 | 跳转支付 | 处理中 |

# 5. 非功能要求（必填）

| 类别 | 要求 | 度量口径与目标值 | 不适用说明 |
|------|------|-------------------|------------|
| 性能与容量 | 弹窗决策链路时延 | 服务端校验 P95 不超过 300 毫秒；日均 12000 次，峰值系数 3 | 无 |
| 安全与隐私 | 最小采集与展示 | 埋点不得上报手机号、地址与优惠金额明细；文案不展示他人数据 | 无 |
| 兼容与终端 | App 与 H5 行为一致 | App 8.12 及以上、H5 主流内核；低于 8.12 不弹窗 | 无 |

# 6. 数据埋点与监控（必填）

## 6.1 事件定义

| 编号 | 事件名 | 业务含义 | 触发时机 | 必填属性（含类型、枚举、空值语义） | 去重依据 | 上报端 | 敏感信息边界 | 负责人 |
|------|--------|----------|----------|--------------------------------------|----------|--------|--------------|--------|
| EVT-001 | retain_popup_show | 挽留弹窗成功展示 | 弹窗渲染完成 | order_id 文本、discount_flag 枚举 yes/no、unknown 允许 | event_id | Server | 禁止上报手机号与地址 | 张三 |

## 6.2 指标与漏斗

| 指标/分析 | 关联目标 | 计算口径 | 维度 | 所需事件 | 时间窗口 | 排除项 |
|-----------|----------|----------|------|----------|----------|--------|
| 弹窗曝光到支付转化 | OBJ-001 | 见 2.2 KPI-001 | 渠道、终端、用户类型 | EVT-001 | 单次会话 | 机器人、内部账号 |
| 弹窗关闭率 | KPI-002 | 关闭次数除以曝光次数 | 终端 | EVT-001 | 单日 | 内部账号 |

## 6.3 看板、告警与数据质量

| 项目 | 内容 | 触发条件/目标 | 周期 | 负责人 |
|------|------|----------------|------|--------|
| 业务看板 | 曝光、点击、支付转化 | 下钻维度：终端、错误类型 | 日 | 张三 |
| 业务告警 | 曝光事件缺失率 | 缺失率高于 1% 持续 30 分钟且样本量不低于 1000，级别 P2 | 5 分钟 | 李四 |
| 数据质量 | 必填属性有效率 | 不低于 99.9% | 日 | 张三 |

# 7. 验收标准（必填）

## 7.1 验收清单

| 编号 | 关联需求 | 场景 | 前置条件 | 操作 | 预期结果 | 验收方式 | 负责人 |
|------|----------|------|----------|------|----------|----------|--------|
| AC-001 | FR-001 | 正常展示 | 已登录且有可用优惠 | 停留 25 秒后点击返回 | 弹窗展示真实优惠 | 人工验收 | 王五 |
| AC-002 | FR-001 | 并发重复触发 | 同一订单已弹窗 | 双端同时点击返回 | 仅一次弹窗且埋点不重复 | 线上指标 | 王五 |

## 7.2 验收与上线确认

| 项目 | 内容 |
|------|------|
| 验收安排 | 产品张三主导，预发环境与非敏感测试数据 |
| 未通过项处理 | 记入 Q-001，由李四在 2026-09-18 前确认后重跑验收 |
| 上线前必须完成 | AC-001 与 AC-002 通过，EVT-001 口径已验证，遗留项有负责人和期限 |

# 8. 发布、依赖与风险（推荐）

## 8.1 发布与放量

| 阶段 | 面向用户与范围 | 观察指标 | 放量/回滚条件 | 时间 | 负责人 |
|------|----------------|----------|----------------|------|--------|
| 小流量 | 5% 已登录买家 | 见 6.2 指标与 KPI-002 | 挽回率不低于 3% 且投诉率不升则继续；否则回滚 | 2026-09-25 | 李四 |
| 全量 | 全部已登录买家 | KPI-001 | 连续 7 天达标 | 2026-10-15 | 李四 |

## 8.2 依赖与风险

| 类型 | 编号 | 事项 | 影响与阻塞范围 | 预防或应对 | 触发信号 | 状态 | 负责人 |
|------|------|------|----------------|------------|----------|------|--------|
| 依赖 | DEP-001 | 消息中心下发通道 | 阻塞 FR-001 的离页触达 | 提前排期并保留降级为不弹窗 | 接口联调延期 | 进行中 | 李四 |
| 风险 | RISK-001 | 优惠数据不一致引发投诉 | 影响 AC-001 与 KPI-002 | 弹窗前二次校验并在失败时不弹窗 | 投诉工单增加 | 开放 | 张三 |

# 9. 附录（按需）

## 9.1 术语与参考文档

| 术语/文档 | 定义或用途 | 链接/来源 |
|-----------|------------|-----------|
| 挽回 | 用户放弃下单后被引导回支付 | 下单页调研纪要 2026-08 |

## 9.2 决策与待决问题

| 类型 | 编号 | 问题或决策 | 结论或备选 | 依据 | 影响范围 | 负责人 | 截止日期 | 当前状态 |
|------|------|------------|------------|------|----------|--------|----------|----------|
| 已决策 | DEC-001 | 同一订单每日弹窗上限 | 1 次 | 客服投诉量与转化平衡 | FR-001、BR-001 | 张三 | - | 已关闭 |
| 待决 | Q-001 | 离页后的站内触达频次上限 | 备选 1 次或 2 次 | 依赖消息中心配额 | DEP-001、FR-001 | 李四 | 2026-09-18 | 待确认 |

## 9.3 来源与追踪台账（必填）

| 编号 | 需求文档对象 | 资料编号 | 定位（页码/段落/图片区域） | 可信度 | 推导依据 | 资料状态与版本/摘要 | 负责人 |
|------|--------------|----------|------------------------------|--------|----------|----------------------|--------|
| SRC-001 | 下单页调研纪要 2026-08 | SRC-001 | 全文 | 已确认 | - | 会议纪要 2026-08-31 4b1e9c7a2f60 | 张三 |
| OBJ-001 | 2.2 目标 | SRC-001 | 第 1 节第 2 段 | 已确认 | - | 同上 | 张三 |
| KPI-001 | 2.2 结果指标 | SRC-001 | 第 1 节第 3 段 | 已确认 | - | 同上 | 张三 |
| KPI-002 | 2.2 护栏指标 | SRC-001 | 第 2 节第 1 段 | 可推导 | 由客服工单分类统计推导 | 同上 | 张三 |
| SC-001 | 3.1 场景 | SRC-001 | 第 2 节第 4 段 | 已确认 | - | 同上 | 张三 |
| FR-001 | 4.1 功能清单 | SRC-001 | 第 3 节标题 | 已确认 | - | 同上 | 李四 |
| BR-001 | 4.3 业务规则 | SRC-001 | 第 3 节第 2 段 | 已确认 | - | 同上 | 李四 |
| ST-001 | 4.3 业务状态 | SRC-001 | 第 3 节第 3 段 | 可推导 | 与 BR-001 和原型状态一致 | 同上 | 李四 |
| AF-001 | 4.3 异常与边界 | SRC-001 | 第 4 节第 1 段 | 已确认 | - | 同上 | 李四 |
| UI-001 | 4.4 页面 | SRC-001 | 原型 Figma P-03 | 已确认 | - | 同上 | 王五 |
| EVT-001 | 6.1 事件定义 | SRC-001 | 第 4 节第 2 段 | 可推导 | 由 KPI-001 计算口径反推 | 同上 | 张三 |
| AC-001 | 7.1 验收清单 | SRC-001 | 第 5 节第 1 段 | 已确认 | - | 同上 | 王五 |
| AC-002 | 7.1 验收清单 | SRC-001 | 第 5 节第 2 段 | 已确认 | - | 同上 | 王五 |
| DEP-001 | 2.4 依赖 | SRC-001 | 第 6 节第 1 段 | 待确认 | 需消息中心确认配额，见 Q-001 | 同上 | 李四 |
| RISK-001 | 8.2 风险 | SRC-001 | 第 6 节第 2 段 | 已确认 | - | 同上 | 张三 |
| Q-001 | 9.2 待决问题 | SRC-001 | 第 6 节第 3 段 | 待确认 | 资料未给出触达上限 | 同上 | 李四 |
| DEC-001 | 9.2 已决策 | SRC-001 | 第 7 节第 1 段 | 已确认 | - | 同上 | 张三 |

## 变更历史（只追加，不覆盖；需求变更时必填）

| 版本 | 日期 | 内容摘要（sha256 前 12 位） | 来源摘要 | 变更数量 | 快照路径 | 确认人 |
|------|------|------------------------------|----------|----------|----------|--------|
| 0.1 | 2026-09-09 | 4b1e9c7a2f60 | SRC-001 已确认 | 修改 0/新增 17/删除 0 | 无历史版本 | 张三 |

# PRD 自检与确认（必填）

| 检查项 | 结论 | 证据/说明 |
|--------|------|-----------|
| 问题有证据，目标与指标口径可计算 | 通过 | 见 2.1 与 2.2 |
| 每个 P0 功能都有正常和异常验收标准 | 通过 | 见 AC-001 与 AC-002 |
| 待决问题、依赖和风险都有负责人和期限 | 通过 | 见 8.2 与 9.2 |

| 项目 | 内容 |
|------|------|
| 结论 | 待确认 |
| 阻塞项与遗留项 | 无 |
| 签署 | 产品张三、研发李四、测试王五 2026-09-09 |

## 独立评审意见（按轮次追加，不覆盖；必填）

### R1 · COMPREHENSIVE · PR-R1-CO

| 回执项 | 内容 |
|--------|------|
| 稿件摘要 | 4b1e9c7a2f60 |
| 状态 | REQUEST_CHANGES |
| 摘要 | 覆盖结构、追踪与验收维度，发现 1 项阻塞 |

| Finding | 严重度 | 分类 | 位置 | 证据 | 影响 | 处理结果 |
|---------|--------|------|------|------|------|----------|
| PR-001 | High | ACCEPTANCE | 7.1 AC-002 | 并发场景缺少重复埋点去重校验 | P0 验收不可执行 | 补充口径 |

"""


def errors_of(text: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "product-doc.md"
        path.write_text(text, encoding="utf-8")
        return PrdValidator(path).validate()


def joined(text: str) -> str:
    return "\n".join(text)


class PrdStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prd = PRD_FIXTURE

    def test_golden_fixture_passes(self) -> None:
        self.assertEqual(errors_of(self.prd), [])

    def test_missing_ledger_chapter_is_reported(self) -> None:
        broken = self.prd.replace("## 9.3 来源与追踪台账（必填）", "## 9.4 其他说明")
        self.assertTrue(any("缺少模板要求的章节" in item for item in errors_of(broken)))

    def test_chapter_heading_must_be_level_one(self) -> None:
        broken = self.prd.replace("# 7. 验收标准（必填）", "## 7. 验收标准（必填）")
        self.assertTrue(any("必须使用一级标题" in item for item in errors_of(broken)))

    def test_unreplaced_placeholder_is_rejected(self) -> None:
        broken = self.prd.replace("| 负责人 | 产品张三、研发李四、测试王五 |", "| 负责人 | 【填写】 |")
        self.assertTrue(any("模板占位符未替换" in item for item in errors_of(broken)))

    def test_allowed_labels_survive_placeholder_check(self) -> None:
        broken = self.prd.replace("| 结论 | 待确认 |", "| 结论 | 待补充 |")
        self.assertEqual(errors_of(broken), [])

    def test_vague_term_is_rejected(self) -> None:
        broken = self.prd.replace(
            "| 业务看板 | 曝光、点击、支付转化 |",
            "| 业务看板 | 体验良好、高性能的看板 |",
        )
        self.assertTrue(any("不可验证的模糊表述" in item for item in errors_of(broken)))


class TraceabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prd = PRD_FIXTURE

    def test_p0_without_acceptance_reference_is_rejected(self) -> None:
        broken = self.prd.replace("AC-001、AC-002 | 李四 |", "待补充 | 李四 |", 1)
        self.assertTrue(any("未引用任何 AC 编号" in item for item in errors_of(broken)))

    def test_acceptance_column_cannot_be_dropped(self) -> None:
        broken = self.prd.replace("| 编号 | 功能名称 | 一句话说明（用户获得什么能力） | 优先级 | 本次范围 | 关联目标 | 关联验收 | 负责人 |",
                                  "| 编号 | 功能名称 | 一句话说明（用户获得什么能力） | 优先级 | 本次范围 | 关联目标 | 负责人 |")
        broken = broken.replace("| FR-001 | 下单页挽留弹窗 | 放弃下单时看到本人可用的真实优惠 | P0 | 是 | OBJ-001 | AC-001、AC-002 | 李四 |",
                               "| FR-001 | 下单页挽留弹窗 | 放弃下单时看到本人可用的真实优惠 | P0 | 是 | OBJ-001 | 李四 |")
        self.assertTrue(any("缺少“优先级”或“关联验收”列" in item for item in errors_of(broken)))

    def test_dangling_reference_is_rejected(self) -> None:
        broken = self.prd.replace("AC-001、AC-002 | 李四 |", "AC-001、AC-009 | 李四 |", 1)
        errors = errors_of(broken)
        self.assertTrue(any("从未在正文定义" in item and "AC-009" in item for item in errors), joined(errors))

    def test_every_ledger_id_is_required(self) -> None:
        lines = self.prd.splitlines()
        ledger_start = lines.index("## 9.3 来源与追踪台账（必填）")
        ledger_end = lines.index("## 变更历史（只追加，不覆盖；需求变更时必填）")
        for identifier in LEDGER_IDS:
            with self.subTest(identifier=identifier):
                # 抹掉台账中所有提到该编号的行（包含作为推导依据被引用的情况）。
                broken = [
                    line for index, line in enumerate(lines)
                    if not (ledger_start < index < ledger_end and identifier in line)
                ]
                errors = errors_of("\n".join(broken))
                self.assertTrue(any("没有资料支撑" in item and identifier in item for item in errors), joined(errors))

    def test_ghost_ledger_row_is_rejected(self) -> None:
        broken = self.prd.replace("| SC-001 | 3.1 场景 |", "| SC-009 | 3.1 场景 |")
        errors = errors_of(broken)
        self.assertTrue(any("正文中不存在的编号" in item and "SC-009" in item for item in errors), joined(errors))

    def test_duplicate_definition_in_same_table_is_rejected(self) -> None:
        row = "| AC-002 | FR-001 | 并发重复触发 | 同一订单已弹窗 | 双端同时点击返回 | 仅一次弹窗且埋点不重复 | 线上指标 | 王五 |"
        broken = self.prd.replace(row, row + "\n" + row)
        self.assertTrue(any("同一张表内重复定义" in item for item in errors_of(broken)))

    def test_field_ids_need_no_ledger_row(self) -> None:
        broken = self.prd.replace("| F-001 | 优惠文案 |", "| F-002 | 优惠文案 |")
        errors = errors_of(broken)
        self.assertFalse(any("F-002" in item and "没有资料支撑" in item for item in errors), joined(errors))


class OpenQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prd = PRD_FIXTURE

    def test_open_question_needs_assignee_and_deadline(self) -> None:
        broken = self.prd.replace(
            "| 待决 | Q-001 | 离页后的站内触达频次上限 | 备选 1 次或 2 次 | 依赖消息中心配额 | DEP-001、FR-001 | 李四 | 2026-09-18 | 待确认 |",
            "| 待决 | Q-001 | 离页后的站内触达频次上限 | 待定 | 依赖消息中心配额 | DEP-001、FR-001 | - | - | 待确认 |",
        )
        errors = errors_of(broken)
        self.assertTrue(any("“负责人”为空" in item for item in errors), joined(errors))
        self.assertTrue(any("截止日期不是具体日期" in item for item in errors), joined(errors))

    def test_decision_rows_are_exempt_from_deadline(self) -> None:
        self.assertEqual(errors_of(self.prd), [])


class ChangeHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prd = PRD_FIXTURE
        self.row = "| 0.1 | 2026-09-09 | 4b1e9c7a2f60 | SRC-001 已确认 | 修改 0/新增 17/删除 0 | 无历史版本 | 张三 |"

    def test_inline_snapshot_fence_is_rejected(self) -> None:
        broken = self.prd.replace(
            self.row,
            self.row + "\n\n```markdown\n# 上一版正文\n\n```",
        )
        self.assertTrue(any("内嵌代码围栏" in item for item in errors_of(broken)))

    def test_snapshot_digest_column_is_enforced(self) -> None:
        broken = self.prd.replace(self.row, self.row.replace("4b1e9c7a2f60", "上一版摘要"))
        self.assertTrue(any("内容摘要不是 sha256 片段" in item for item in errors_of(broken)))

    def test_duplicate_history_row_is_rejected(self) -> None:
        broken = self.prd.replace(self.row, self.row + "\n" + self.row)
        self.assertTrue(any("组合重复追加" in item for item in errors_of(broken)))

    def test_snapshot_must_live_in_history_directory(self) -> None:
        broken = self.prd.replace(self.row, self.row.replace("无历史版本", "product-doc-v0.1.md"))
        errors = errors_of(broken)
        self.assertTrue(any("未指向 history/ 下的独立文件" in item for item in errors), joined(errors))

    def test_empty_history_table_is_rejected(self) -> None:
        broken = self.prd.replace(self.row, "")
        self.assertTrue(any("变更历史台账为空" in item for item in errors_of(broken)))

    def test_change_mode_requires_this_round_summary(self) -> None:
        broken = self.prd.replace(self.row, self.row.replace("无历史版本", "`history/product-doc-v0.1.md`"))
        errors = errors_of(broken)
        self.assertTrue(any("缺少模板要求的章节" in item and "本轮变更摘要" in item for item in errors), joined(errors))

    def test_deleted_id_must_leave_the_current_feature_list(self) -> None:
        broken = self.prd.replace(self.row, self.row.replace("无历史版本", "`history/product-doc-v0.1.md`"))
        broken = broken.replace(
            "| 0.1 | 2026-09-09 | 张三 | 新增 | 初稿 | 首次创建 | 张三 |",
            "| 0.1 | 2026-09-09 | 张三 | 新增 | 初稿 | 首次创建 | 张三 |\n\n"
            "### 本轮变更摘要（需求变更时必填）\n\n"
            "- **【修改】**：无\n"
            "- **【新增】**：无\n"
            "- **【删除】**：FR-001 取消下单页挽留弹窗；原因/来源：方案调整；影响：AC-001 一并下线\n",
        )
        errors = errors_of(broken)
        self.assertTrue(
            any("仍作为 4.1 功能清单的当前有效条目" in item and "FR-001" in item for item in errors),
            joined(errors),
        )


class ReviewAppendixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prd = PRD_FIXTURE

    def test_missing_perspective_is_rejected(self) -> None:
        broken = self.prd.replace("### R1 · COMPREHENSIVE · PR-R1-CO", "### R1 · PR-R1-CO")
        self.assertTrue(any("缺少 perspective" in item for item in errors_of(broken)))

    def test_missing_review_id_is_rejected(self) -> None:
        broken = self.prd.replace("### R1 · COMPREHENSIVE · PR-R1-CO", "### R1 · COMPREHENSIVE · 第一轮")
        self.assertTrue(any("缺少 review_id" in item for item in errors_of(broken)))

    def test_missing_digest_is_rejected(self) -> None:
        broken = self.prd.replace("| 稿件摘要 | 4b1e9c7a2f60 |", "| 稿件摘要 | 待补充 |")
        self.assertTrue(any("缺少 12 位以上的稿件摘要" in item for item in errors_of(broken)))

    def test_unknown_status_vocabulary_is_rejected(self) -> None:
        broken = self.prd.replace("| 状态 | REQUEST_CHANGES |", "| 状态 | 需补充 |")
        self.assertTrue(any("评审状态非法" in item for item in errors_of(broken)))

    def test_scale_routes_required_perspectives(self) -> None:
        broken = self.prd.replace("| 需求规模 / 优先级 | S / P0 |", "| 需求规模 / 优先级 | M / P0 |")
        errors = errors_of(broken)
        self.assertTrue(any("M 级需求的独立评审缺少视角" in item for item in errors), joined(errors))

    def test_ambiguous_scale_is_rejected(self) -> None:
        broken = self.prd.replace("| 需求规模 / 优先级 | S / P0 |", "| 需求规模 / 优先级 | S · M · L / P0 |")
        self.assertTrue(any("需求规模未选定" in item for item in errors_of(broken)))

    def test_signoff_must_not_contradict_findings(self) -> None:
        broken = self.prd.replace("| 结论 | 待确认 |", "| 结论 | 已确认 |")
        broken = broken.replace("| 待决问题、依赖和风险都有负责人和期限 | 通过 | 见 8.2 与 9.2 |",
                                "| 待决问题、依赖和风险都有负责人和期限 | 不通过 | 见 8.2 与 9.2 |")
        errors = errors_of(broken)
        self.assertTrue(any("自检不通过" in item for item in errors), joined(errors))
        self.assertTrue(any("结论不得写“已确认”" in item for item in errors), joined(errors))

    def test_blocked_appendix_is_recognised_but_not_approvable(self) -> None:
        broken = self.prd.replace("| 状态 | REQUEST_CHANGES |", "| 状态 | BLOCKED |")
        broken = broken.replace("| 结论 | 待确认 |", "| 结论 | 已确认 |")
        errors = errors_of(broken)
        self.assertTrue(any("最后一轮独立评审状态为 `BLOCKED`" in item for item in errors), joined(errors))


class TemplateRefDriftTests(unittest.TestCase):
    def test_skill_assets_reference_existing_template_sections(self) -> None:
        targets = [
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "references" / "check-standard.md",
            SKILL_DIR / "references" / "product-review-contract.md",
            *sorted((SKILL_DIR / "agents").glob("*.md")),
        ]
        errors = check_template_refs(targets, SKILL_DIR / "references" / "product-doc-template.md")
        self.assertEqual(errors, [])

    def test_drift_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.md"
            target.write_text("详见 9.5 待决问题与 4.7 排期。\n", encoding="utf-8")
            errors = check_template_refs([target], SKILL_DIR / "references" / "product-doc-template.md")
        self.assertEqual(len(errors), 2, joined(errors))

    def test_numbers_that_are_not_section_refs_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.md"
            target.write_text("P95 不超过 300 毫秒，月度可用性 99.9%，版本 v1.2，日期 2026-09-09。\n", encoding="utf-8")
            errors = check_template_refs([target], SKILL_DIR / "references" / "product-doc-template.md")
        self.assertEqual(errors, [])


class ReviewStateCliTests(unittest.TestCase):
    APPENDIX = "## 独立评审意见（按轮次追加，不覆盖；必填）"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "prd-review.json"
        self.prd = self.root / "product-doc.md"
        self.prd.write_text("# 下单页挽留\n\n## 变更历史\n\n| 版本 | 内容 |\n|---|---|\n| 0.1 | 初稿 |\n", encoding="utf-8")

    def cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["review_state.py", *argv]
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = review_state.main()
        except SystemExit as exc:  # argparse 错误路径
            code = int(exc.code or 0)
        finally:
            sys.argv = old_argv
        return int(code or 0), buffer.getvalue()

    def init(self, scale: str = "S") -> None:
        source = self.root / "SRC-001.md"
        source.write_text("# 调研纪要\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--prd", str(self.prd),
                             "--mode", "new", "--scale", scale, "--input", f"SRC-001={source}")
        self.assertEqual(code, 0, out)

    def report(self, review_id: str, perspective: str, status: str, findings: list[dict], **extra) -> Path:
        digest = review_state.content_digest(self.prd)
        payload = {
            "review_id": review_id,
            "contract_version": review_state.CONTRACT_VERSION,
            "review_round": json.loads(self.state.read_text(encoding="utf-8"))["current_round"],
            "perspective": perspective,
            "scale": json.loads(self.state.read_text(encoding="utf-8"))["scale"],
            "prd_digest": digest,
            "status": status,
            "summary": "覆盖结构与追踪维度",
            "findings": findings,
            "prd_appendix_markdown": f"### {review_id}\n\n评审意见正文",
        }
        payload.update(extra)
        path = self.root / f"{review_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def finding(identifier: str = "PR-001", severity: str = "High", **extra) -> dict:
        item = {
            "id": identifier,
            "severity": severity,
            "category": "TRACEABILITY",
            "location": "4.1 功能清单",
            "evidence": "AC-001 未覆盖并发场景",
            "impact": "P0 验收不可执行",
            "remediation": "补充 AC 编号",
            "verification": "重跑结构门禁",
        }
        item.update(extra)
        return item

    def append_appendix(self, review_id: str) -> None:
        """把回执归档到文末评审区——它不得改变被评审正文的指纹。"""
        lines = self.prd.read_text(encoding="utf-8").rstrip().splitlines()
        if self.APPENDIX not in lines:
            lines += ["", self.APPENDIX]
        lines += ["", f"### {review_id}", "", f"{review_id} 的评审意见已原样归档。"]
        self.prd.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def edit_body(self, note: str) -> None:
        """一次实质修订：修改正文而非追加回执。"""
        lines = self.prd.read_text(encoding="utf-8").rstrip().splitlines()
        marker = self.APPENDIX
        if marker in lines:
            head, tail = lines[: lines.index(marker)], lines[lines.index(marker):]
        else:
            head, tail = lines, []
        self.prd.write_text("\n".join(head + [note] + tail) + "\n", encoding="utf-8")

    def test_init_requires_at_least_one_input(self) -> None:
        code, out = self.cli("init", "--state", str(self.state), "--prd", str(self.prd),
                             "--mode", "new", "--scale", "S")
        self.assertEqual(code, 1)
        self.assertIn("必须至少登记一份输入资料", out)

    def test_init_rejects_missing_prd(self) -> None:
        code, out = self.cli("init", "--state", str(self.state), "--prd", str(self.root / "nope.md"),
                             "--mode", "new", "--scale", "S", "--input", "notes.md")
        self.assertEqual(code, 1)
        self.assertIn("PRD 不存在", out)

    def test_record_rejects_stale_digest(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES", [self.finding()])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["prd_digest"] = "0" * 64
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("与当前稿件摘要不一致", out)

    def test_record_rejects_perspective_outside_scale(self) -> None:
        self.init()
        path = self.report("PR-R1-NF", "NON_FUNCTIONAL", "APPROVE", [])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("级需求不接受视角", out)

    def test_record_rejects_non_blocking_high(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES",
                           [self.finding(severity="High", blocking=False)])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("不得标记 blocking=false", out)

    def test_record_rejects_approve_while_blocker_open(self) -> None:
        self.init()
        first = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(first))[0], 0)
        self.append_appendix("PR-R1-CO")
        self.edit_body("按 PR-001 补充 AC-002 的并发口径")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        second = self.report("PR-R2-CO", "COMPREHENSIVE", "APPROVE", [])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(second))
        self.assertEqual(code, 1)
        self.assertIn("台账中无未闭环项", out)

    def test_advance_requires_all_perspectives(self) -> None:
        self.init("M")
        path = self.report("PR-R1-CO", "COMPLETENESS", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("PR-R1-CO")
        code, out = self.cli("advance", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("缺少 NON_FUNCTIONAL", out)

    def test_advance_requires_edited_draft(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        # 只归档回执、不改正文，不得借此推轮。
        self.append_appendix("PR-R1-CO")
        code, out = self.cli("advance", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("稿件正文与送审时完全一致", out)
        self.edit_body("按 PR-001 修正 4.1 的关联验收列")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)

    def test_archiving_appendix_does_not_change_the_reviewed_fingerprint(self) -> None:
        self.init()
        before = review_state.content_digest(self.prd)
        self.append_appendix("PR-R1-CO")
        self.assertEqual(review_state.content_digest(self.prd), before)

    def test_fixed_requires_a_recheck_round(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "PR-001",
                             "--resolution", "fixed", "--evidence", "已按 SRC-001 修正 4.1 的关联验收列")
        self.assertEqual(code, 1)
        self.assertIn("必须先 advance", out)
        self.append_appendix("PR-R1-CO")
        self.edit_body("按 PR-001 修正 4.1 的关联验收列")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "PR-001",
                             "--resolution", "fixed", "--evidence", "已按 SRC-001 修正 4.1 的关联验收列")
        self.assertEqual(code, 0, out)

    def test_fixed_requires_a_recheck_on_a_different_draft(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("PR-R1-CO")
        self.edit_body("按 PR-001 修正 4.1 的关联验收列")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "PR-001",
                                  "--resolution", "fixed",
                                  "--evidence", "已按 SRC-001 修正 4.1 的关联验收列")[0], 0)
        second = self.report("PR-R2-CO", "COMPREHENSIVE", "APPROVE_WITH_FOLLOW_UPS",
                             [self.finding(identifier="PR-002", severity="Low", blocking=False)])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("PR-R2-CO")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)

    def test_deferred_requires_existing_question_id(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "APPROVE_WITH_FOLLOW_UPS",
                           [self.finding(severity="Medium", blocking=False)])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "PR-001",
                             "--resolution", "deferred", "--evidence", "口径待业务确认，先记入待决问题", "--q-ref", "Q-007")
        self.assertEqual(code, 1)
        self.assertIn("未出现在 PRD", out)

    def test_blocker_cannot_be_rejected(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES", [self.finding(severity="Blocker")])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "PR-001",
                             "--resolution", "rejected", "--evidence", "认为该问题不影响交付，理由充分且已复核两次")
        self.assertEqual(code, 1)
        self.assertIn("不得判定为不采纳", out)

    def test_verify_rejects_unarchived_review_id(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("未追加进 PRD", out)

    def test_clean_two_round_delivery_is_valid(self) -> None:
        self.init()
        first = self.report("PR-R1-CO", "COMPREHENSIVE", "APPROVE_WITH_FOLLOW_UPS",
                            [self.finding(severity="Low", blocking=False)])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(first))[0], 0)
        self.append_appendix("PR-R1-CO")
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "PR-001",
                                  "--resolution", "rejected",
                                  "--evidence", "该措辞已被 2.2 的计算口径替代，不构成文档缺陷")[0], 0)
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        second = self.report("PR-R2-CO", "COMPREHENSIVE", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("PR-R2-CO")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)

    def test_verify_rejects_delivery_after_an_unreviewed_edit(self) -> None:
        self.init()
        first = self.report("PR-R1-CO", "COMPREHENSIVE", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(first))[0], 0)
        self.append_appendix("PR-R1-CO")
        self.edit_body("未经复核的正文改动")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("又被修改", out)

    def test_round_limit_forces_blocked_delivery(self) -> None:
        self.init()
        for round_number in range(1, 6):
            path = self.report(f"PR-R{round_number}-CO", "COMPREHENSIVE", "REQUEST_CHANGES",
                               [self.finding(identifier=f"PR-{round_number:03d}")])
            code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
            self.assertEqual(code, 0, out)
            self.append_appendix(f"PR-R{round_number}-CO")
            if round_number < 5:
                self.edit_body(f"第 {round_number} 轮的修订说明")
            code, out = self.cli("advance", "--state", str(self.state))
            self.assertEqual(code, 0 if round_number < 5 else 1, out)
        self.assertIn("已达 5 轮上限", out)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("仍有未闭环阻塞项", out)
        code, out = self.cli("verify", "--state", str(self.state), "--allow-blocked")
        self.assertEqual(code, 0, out)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "BLOCKED")
        self.assertEqual(state["current_round"], 5)

    def test_show_reports_rounds_and_findings(self) -> None:
        self.init()
        path = self.report("PR-R1-CO", "COMPREHENSIVE", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("show", "--state", str(self.state))
        self.assertEqual(code, 0, out)
        summary = json.loads(out)
        self.assertEqual(summary["current_round"], 1)
        self.assertEqual(summary["rounds"]["1"], ["PR-R1-CO:REQUEST_CHANGES"])
        self.assertIn("PR-001", summary["findings"])


class AssetContractTests(unittest.TestCase):
    """P0-1 与 P1 的资产层不变量：枚举与规模判定只有一个事实源。"""

    def test_contract_is_the_single_status_source(self) -> None:
        for status in review_state.STATUSES:
            self.assertIn(status, CONTRACT)
        for severity in review_state.SEVERITIES:
            self.assertIn(severity, CONTRACT)
        self.assertNotIn("APPROVE / APPROVE_WITH_FOLLOW_UPS / REQUEST_CHANGES / BLOCKED", SKILL_MD)
        self.assertIn("product-review-contract.md", SKILL_MD)
        self.assertIn("product-review-contract.md", STANDARD)

    def test_human_conclusions_map_to_contract(self) -> None:
        self.assertIn("需补充", STANDARD)
        self.assertIn("APPROVE_WITH_FOLLOW_UPS", STANDARD)

    def test_round_semantics_stated_once(self) -> None:
        self.assertEqual(review_state.MAX_ROUNDS, 5)
        self.assertNotIn("review_round < 5", SKILL_MD)
        self.assertNotIn("≤ 5", SKILL_MD)
        self.assertIn("最多 5", SKILL_MD)

    def test_scale_criteria_live_only_in_the_template(self) -> None:
        self.assertIn("无资金/隐私/权限变化", TEMPLATE)
        self.assertNotIn("无资金/隐私/权限变化", SKILL_MD)
        self.assertIn("文档裁剪", SKILL_MD)

    def test_skill_declares_its_gate_scripts(self) -> None:
        for script in ("validate_prd.py", "review_state.py"):
            self.assertIn(f"scripts/{script}", SKILL_MD)
            self.assertTrue((SKILL_DIR / "scripts" / script).is_file(), script)
        self.assertNotIn("本 Skill 不自带脚本", SKILL_MD)

    def test_writer_is_not_a_second_writer(self) -> None:
        self.assertNotIn("Write", AGENTS["product-writer"].splitlines()[4])
        self.assertNotIn("Edit", AGENTS["product-writer"].splitlines()[4])

    def test_read_only_roles_have_no_shell(self) -> None:
        for role in ("product-extractor", "product-reviewer"):
            tools = re.search(r"^tools: (.+)$", AGENTS[role], re.MULTILINE).group(1)
            with self.subTest(role=role):
                self.assertNotIn("Bash", tools)
                self.assertIn("Read", tools)

    def test_template_and_standard_agree_on_check_columns(self) -> None:
        self.assertIn("关联验收", TEMPLATE)
        self.assertIn("关联验收", STANDARD)
        self.assertIn("来源与追踪台账", STANDARD)
        self.assertNotIn("```markdown", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
