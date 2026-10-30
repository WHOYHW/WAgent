# WAgent · 校园智能体（湖南理工大学）

> 面向高校真实业务场景的 Agent 项目。核心不是"做一个聊天机器人"，而是**把高校的业务流程拆开，判断哪些环节可以交给 AI、哪些必须由人兜底，并给出可工程化的实现方案**。

---

## 一、项目缘起

高校信息化建设二十年，沉淀了教务、图书馆、一卡通、学工、后勤等一批**彼此独立的业务系统**。对学生的直接体验是：

- 查课表要开教务系统、查借阅要开图书馆公众号、查成绩要等系统开放 —— **入口分散，重复登录**；
- 补考报名、选课开放、借阅逾期、成绩发布这些**有严格时间窗口的事**，通知躺在各自系统里没人看，错过就是真损失；
- 真正复杂的事（成绩复核、学籍异动、奖助学金评定）流程长、跨部门、必须线下签字盖章，学生**不知道找谁、要带什么材料、走到哪一步了**。

本项目要用 Agent 承接「高频、低风险、规则明确」的信息类与提醒类业务，把复杂业务的「前期准备」做掉，**把最终决策与签章留给人和制度**。

## 二、目录结构

```
WAgent/
├── docs/                     设计文档集（9 篇，含 32 张 Mermaid 流程图）
│   ├── README.md             文档索引与阅读路径
│   ├── 00-行业总览-校园业务全景与AI落地地图.md
│   ├── 01-业务域-教务与学工.md
│   ├── 02-业务域-图书与资源服务.md
│   ├── 03-业务域-后勤生活与迎新离校.md
│   ├── 10-阶段一-校园助手Agent设计.md          ← 阶段一主文档
│   ├── 11-阶段一-AI可执行与人工兜底边界矩阵.md
│   ├── 12-阶段一-技术架构与数据契约.md
│   ├── 13-阶段一-风控合规与评测上线.md
│   └── 14-技术选型-Agent平台后端框架对比.md
├── backend/                  阶段一后端实现（Python / FastAPI 方向）
│   └── app/
│       ├── config.py         全局配置（环境变量优先）
│       ├── cache.py          进程内 TTL 缓存（缓存与降级策略第一道）
│       ├── db.py             SQLite：审计日志 + 转人工工单
│       ├── models.py         工具契约数据模型
│       ├── security.py       会话身份层（student_id 由 JWT 注入）
│       ├── logging_util.py   会话级 trace + 脱敏审计
│       ├── edu_adapter.py    教务数据适配器（三级接入优先级）
│       └── tools/timetable.py  timetable_query 工具
└── main.py
```

## 三、设计主线

整套设计贯穿一条线：**还原真实流程 → 逐环节打标 → 识别可闭合链路 → 设计 Agent → 定义度量与红线**。

1. **业务域拆分** —— 按「服务方 + 学生生命周期」拆成 7 个域（教务教学 / 学工思政 / 图书情报 / 财务缴费 / 后勤生活 / 就业创业 / 一网通办），用「信息确定性 × 后果可逆性」二维模型评估各域 AI 适配度。
2. **三级边界标注** —— 每个环节必须标注 `AI`（可自动）/ `AI+H`（AI 准备 + 人工确认）/ `H`（禁止 AI 介入），**不允许存在说不清谁负责的灰色环节**。
3. **链路选型** —— 阶段一选取「学业信息与提醒链路」，理由是它建的五项基建（身份接入、数据适配、消息通道、提醒引擎、转人工协议）在后续阶段全部复用。
4. **度量与红线** —— 6 条红线（不做终局结论、不做不可逆写、不加工敏感数据、不代表达诉求、生命优先、不确定即转人工）落地为可热更新的规则配置。

## 四、四条设计约定

1. **只读优先。** 阶段一除「图书续借」外不引入任何写操作。凡涉及学籍、成绩、毕业结论的动作，Agent 一律不落库、不裁定。
2. **权威性可追溯。** 所有查询结果强制携带「数据来源系统 + 数据同步时间」，与源系统冲突时以源系统为准，Agent 不做二次推断。
3. **`student_id` 由会话身份层注入**，不作为模型可填参数 —— 从架构上根除「提示词注入导致越权查他人数据」。
4. **不做未授权抓取。** 数据接入优先级为：学校开放接口 / 数据中台只读视图 > 与信息中心共建适配器 > 学生本人授权后的个人数据接口。**不采用绕过统一身份认证的爬取方案。**

## 五、阅读入口

**建议从 [`docs/README.md`](./docs/README.md) 开始**，那里有完整的文档地图与阅读路径。

只想看结论的话：读 `00` 的第 3、5 节，再读 `10` 的第 3、4 节。

## 六、开发环境

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

配置通过环境变量或 `backend/.env` 注入（该文件不入库）：

| 变量 | 说明 |
|---|---|
| `JWT_SECRET` | 会话令牌密钥，生产环境必须替换默认值 |
| `EDU_API_BASE_URL` / `EDU_API_TOKEN` | 教务开放接口地址与凭证，留空则走降级适配器 |
| `LLM_API_KEY` | 大模型调用凭证 |
| `DB_URL` | 默认 `sqlite:///backend/campus_agent.db` |

## 七、当前进度

- [x] 设计文档集：行业总览、业务域流程拆解、阶段一完整设计、边界矩阵、技术架构、风控评测、技术选型
- [x] 后端骨架：配置 / 缓存 / 存储 / 安全 / 适配器 / `timetable_query` 工具
- [x] 后端工具补全：`grade_query`、`borrow_query`、续借写操作（[`backend/app/tools/`](./backend/app/tools/) 6 工具齐备）
- [x] 会话存储层：`SessionStore` + `conversation_session` / `message` 两张表 + 历史压缩（任务设计文档 [`docs/项目文档/01-会话存储层设计.md`](./docs/项目文档/01-会话存储层设计.md)）
- [x] 提醒引擎（调度、静默期、去重聚合、日频上限、降级链；真实通道重推编排属后续阶段，09 §1.3 已声明阶段一只落表结构与状态机设计，`claimed_at`/`is_degraded`/终态集齐备）
  - [x] 总体设计与三决策点裁决：D1 进程内 asyncio 心跳（否决 APScheduler）/ D2 静默期跳过标 skipped_quiet（`:quiet` 命名空间，次晨窗口自然重放）/ D3 唯一约束 claim-first + CAS 认领（否决分布式锁）（任务设计文档 [`docs/项目文档/09-提醒引擎设计.md`](./docs/项目文档/09-提醒引擎设计.md)）
  - [x] R1 到期生成（纯函数）：`(订阅, 数据源快照, now) → 候选`——明天课表（节次锚定）+ 借阅 N 天内到期（按日聚合）、dedup_key 规则（判别位 + `:quiet` 命名空间）、日频计数；零 IO/零调度/零推送，30 项单测（[candidates.py](./backend/app/reminder/candidates.py)｜[test_reminder_candidates.py](./backend/tests/test_reminder_candidates.py)）
  - [x] R2 推送通道适配器：`PushAdapter` Protocol（send → `PushReceipt` 回执）+ 开发桩（degraded 回执，不冒充真实触达）/ 企微 webhook（errcode 语义）+ 配置门控工厂；完全对齐 edu/lib 模式（DI、`PushNotConfiguredError` 惰性校验、aclose 幂等），写路径不重试，降级回执直接映射 `dispatch.status`；15 项单测全桩零网络（[push.py](./backend/app/adapters/push.py)｜[test_push_adapter.py](./backend/tests/test_push_adapter.py)）
  - [x] R3 心跳 + 求值 + 调试面：`ReminderEngine`（start/stop 幂等、±10% 抖动、scan_once 两层异常捕获永不抛出）→ 双求值器接线（新鲜度裁决 stale/unavailable 零候选、CAMPUS_TZ 固定 +8、快照日按订阅 timing 最大提前量）→ `POST /agent/reminders/scan` 手动触发（ScanStats 透传）（实施设计 [`docs/项目文档/10-提醒引擎R3a-心跳骨架与接线设计.md`](./docs/项目文档/10-提醒引擎R3a-心跳骨架与接线设计.md)｜[11-求值器接线](./docs/项目文档/11-提醒引擎R3b-求值器接线设计.md)｜[12-手动触发调试面](./docs/项目文档/12-提醒引擎R3c-手动触发调试面设计.md)；[engine.py](./backend/app/reminder/engine.py)｜[evaluator.py](./backend/app/reminder/evaluator.py)）
  - [x] R4 渲染 + 四闸 + 下发闭环：渲染纯函数（借阅聚合/课程文案，`is_changed` 高亮【调停课】）→ 静默门（`CAMPUS_TZ` 跨午夜，P0 穿透）→ claim（INSERT + SAVEPOINT，IntegrityError 先 CAS 认领自愈再让路）→ 频控（`daily_dispatch_count` 纯函数零第二份逻辑，P0 不计数）→ 聚合（非 P0 合并【提醒汇总】）→ 推送 → 终态单语句回写（实施设计 [`docs/项目文档/13-提醒引擎R4a-渲染纯函数设计.md`](./docs/项目文档/13-提醒引擎R4a-渲染纯函数设计.md)｜[14-四闸全序与下发闭环](./docs/项目文档/14-提醒引擎R4b-四闸全序与下发闭环设计.md)；[render.py](./backend/app/reminder/render.py)｜[gate.py](./backend/app/reminder/gate.py)）
  - [x] R5 效果回流：契约层（`ReminderFeedbackAction` 单向状态机词表 + 无 `student_id` 请求体，L4）→ `POST /agent/reminders/feedback`（学生 JWT + 行归属校验 404；重复/乱序幂等 no-op + 200，否决 409）→ 统计口径（`loss_avoidance_stats` 纯函数：分母 = sent 且非重发行；`GET /agent/reminders/stats` 调试面，G6/AC-12 可演示）→ 4 组 12 项测试（归属/open 幂等/状态机/口径闭环）（实施设计 [`docs/项目文档/15-提醒引擎R5-效果回流设计.md`](./docs/项目文档/15-提醒引擎R5-效果回流设计.md)；[feedback.py](./backend/app/reminder/feedback.py)｜[stats.py](./backend/app/reminder/stats.py)｜[test_api_reminder_feedback.py](./backend/tests/test_api_reminder_feedback.py)）
- [ ] 编排层与 SSE 流式对话接口
  - [x] 编排核心 `Orchestrator`：对话循环（会话准备/红线拦截/历史压缩/工具路由/迭代熔断/写门禁）+ 事件流与 `LLMClient` 协议（任务设计文档 [`docs/项目文档/03-编排核心Orchestrator设计.md`](./docs/项目文档/03-编排核心Orchestrator设计.md)）
  - [x] LLM 适配器 `ArkLLMClient`：火山方舟（豆包）OpenAI 兼容流式接入、tool_calls 分片聚合、thinking 三态可配（默认不发厂商私有参数，任何兼容端点零改动切换）、`--live` 真机联调（任务设计文档 [`docs/项目文档/05-LLM适配器设计.md`](./docs/项目文档/05-LLM适配器设计.md)）
  - [x] SSE 端点 `POST /agent/chat`：六类事件 → SSE 帧流式对话接口、依赖注入装配（`get_llm`/`get_orchestrator`）、断线清理、配置门控 CORS、首个 API 级测试基建（httpx ASGITransport），真机联调打字机效果确认（任务设计文档 [`docs/项目文档/07-SSE端点设计.md`](./docs/项目文档/07-SSE端点设计.md)）
- [x] 工程化基线：lint（ruff，含 TID251 禁用清单）+ 分层导入契约（import-linter 两契约，AC-16b 负向验证通过）+ CI（GitHub Actions）——238 项测试、09 D1 裁决与 12 分层架构由机器强制（任务设计 [`docs/项目文档/16-工程化基线设计.md`](./docs/项目文档/16-工程化基线设计.md)，AC-16a/b 本地达成；AC-16c Actions 首跑 / AC-16d 分支保护待 push 后在仓库设置验证）
- [ ] 评测黄金集与端到端联调

---

*阶段一交付 ｜ 文档版本 v1.0*
