# 任务设计文档 — 子任务 3：LLM 适配器（ArkLLMClient）

> 文档编号：WAgent-IMPL-05
> 版本：v1.0
> 适用阶段：阶段一 · 编排层（M2）
> 关联文档：[`03-编排核心Orchestrator设计.md`](./03-编排核心Orchestrator设计.md) §2.4（冻结协议，本任务的实现契约）｜ [`10-阶段一-校园助手Agent设计.md`](../10-阶段一-校园助手Agent设计.md) §3.1 ｜ [`12-阶段一-技术架构与数据契约.md`](../12-阶段一-技术架构与数据契约.md) ｜ [`15-阶段一-核心设计任务实施规范.md`](../15-阶段一-核心设计任务实施规范.md) §4.3

### 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-09 | 初版。实现火山方舟（豆包）OpenAI 兼容流式适配器；含 `--live` 联调实测数据（§4.3）与两条联调发现（§6.3 遗留项） |
| v1.1 | 2026-10-13 | thinking 参数三态化：`None`=不发该参数（代码默认，厂商中立）/ `True`/`False`=显式开/关；方舟既有行为经 `.env` 的 `LLM_THINKING_ENABLED=false` 保持。消除换用严格 OpenAI 端点时未知参数 400 的风险（§F4、§3.8） |

---

## 一、任务概述

### 1.1 背景：发动机已就位，缺一台真引擎

子任务 2 交付的编排核心 `Orchestrator`（842 行）已实现完整对话循环——红线门、
压缩、工具路由、熔断、写门禁——但驱动它的始终是测试与 demo 里的
`FakeLLM` 脚本桩。整条链路是"没有大脑的躯干"：**所有编排逻辑都只被
脚本验证过，从未与真实模型交互**。这是当前最大的"已设计但未验证"风险面。

本任务实现 03 文档 §2.4 冻结的 `LLMClient` Protocol，接入火山引擎方舟
（豆包系列，OpenAI 兼容端点）。协议已冻结意味着：**本任务零改编排层**，
是纯增量工作。

### 1.2 目标

| 编号 | 目标 | 验证方式 |
|---|---|---|
| G1 | `ArkLLMClient` 结构化匹配 `LLMClient` Protocol：OpenAI 兼容流式分片 → `LLMChunk` 块序列 | AC-1~8、AC-14 |
| G2 | tool_calls 分片聚合正确（六类边界全覆盖） | AC-4~8 |
| G3 | 失败语义与编排层兜底闭环（F6 `llm_unavailable` / F3 压缩降级），不引入双重重试 | AC-9/10/16 |
| G4 | 思考模型（豆包 seed）的 reasoning 不透传、thinking 默认关 | AC-2/12/15 |
| G5 | 全链路真机联调一次（`demo_orchestrator --live`），留存 TTFT 实测 | §4.3 联调记录 |

### 1.3 范围

**做**：`app/llm/` 适配器包；`config.py` 三个新配置；依赖清单修复；
`--live` 联调开关；16 条 AC 单测。

**不做**（明确排除）：
- ❌ SSE 端点 / FastAPI lifespan 接线 / 适配器单例的挂接 → **子任务 4**；
- ❌ system prompt 内容工程（本任务只消费 03 给定的初版）→ 3/4 持续迭代；
- ❌ usage 计量落地（token 用量入库）→ 阶段二审计扩展，本任务只留开关；
- ❌ 语义红线通道 → 后续阶段。

### 1.4 与上游下游的关系

```
Orchestrator（子任务 2，请求级）
    │ 只认 LLMClient Protocol（已冻结）
    ▼
ArkLLMClient（本任务，应用级单例·子任务 4 挂 lifespan）
    │ openai SDK（AsyncOpenAI）
    ▼
火山方舟 https://ark.cn-beijing.volces.com/api/v3
模型 doubao-seed-2-1-turbo-260628（思考模型，默认关 thinking）
```

依赖倒置（P2）的完整闭环在本任务落地：编排层与子任务 4 **永不 import
`openai` 的任何符号**（含异常类型）——厂商切换只改 `.env` 与适配器内部。

---

## 二、总体设计与核心契约

### 2.1 设计原则映射

| 原则 | 本任务的落点 |
|---|---|
| P2 依赖倒置 | 适配器实现 Protocol（结构化匹配，不继承）；SDK 异常统一包装为 `LLMStreamError`，`openai` 不外泄 |
| P3 可测性 | 构造接受 `client` 注入，单测用 `FakeOpenAI`（SimpleNamespace 树），全程不触网络 |
| P5 每个分支有用户可见出口 | 懒校验：LLM 未配置不阻止应用启动（红线拦截等 F2 路径不受影响），首条 LLM 消息才显性失败 |
| P6 参数不可信 | 日志只记元数据（model/块数/finish/耗时），不记内容与参数；Key 永不入日志 |

### 2.2 SDK chunk → LLMChunk 映射表

| OpenAI 流式分片形态 | 适配器行为 | 产出 |
|---|---|---|
| `choices[0].delta.content` 非空 | 透传 | `LLMChunk(delta=...)` |
| `delta.content` 为 `None`/`""`（role-only 首块、心跳） | 跳过 | 无 |
| `delta.reasoning_content`（豆包思考，SDK extra 属性） | **有意不读取** | 无（R3：思考过程用户不可见） |
| `delta.tool_calls[i]`（id/name/arguments 任一分片） | 按 `index` 分桶累积 | （延迟到终块） |
| `choices` 为空（usage 尾块 / keep-alive） | 跳过 | 无 |
| 流结束且有聚合 | 恰好一个终块 | `LLMChunk(tool_calls=[...], finish_reason="tool_calls")` |
| 流结束且无聚合 | 恰好一个终块 | `LLMChunk(finish_reason="stop")` |
| `create()` 或迭代中抛 SDK 异常 | 包装上抛 | `LLMStreamError`（`__cause__` 保留） |

### 2.3 与冻结协议的对齐声明

03 §2.4 的三条约定逐条兑现：

1. **"delta 与 tool_calls 不同时出现"**——终块设计保证：聚合态 tool_calls
   只在流结束后的最后一个块产出，此前只 yield 纯 delta 块；
2. **"tool_calls（聚合完成态）只出现在流的最后一个块"**——聚合器在流关闭
   后统一产出，天然满足；
3. **"非流式场景用同一接口"**——摘要路径（F3）忽略 delta 只拼接文本，
   已在编排层实现，适配器无需特判。

### 2.4 异常模型

| 异常 | 语义 | 编排层映射 |
|---|---|---|
| `LLMNotConfiguredError` | 配置缺失（懒抛，镜像 `adapters.SourceNotConfiguredError`） | 首请求失败 → F6 → `error(llm_unavailable)` |
| `LLMStreamError` | SDK 异常统一包装，消息含原始类名，不含内容/凭证 | 同上；压缩场景 → F3 降级 `False` |

命名刻意区别于编排层自身的 `LLMStreamFailure`（F6 语义）：
`LLMStreamError`（适配器故障）→ 编排层捕获后置 `LLMStreamFailure` →
`error(llm_unavailable)`。`CancelledError`/`GeneratorExit` 是
BaseException，`except Exception` 天然放行（消费者弃流语义不受影响）。

---

## 三、功能详细设计

### F1 配置与懒创建

- 构造零副作用（不触网、不校验）——import 与实例化在任何环境下安全；
- `is_configured` 属性：三项配置（base/key/model）齐全性，供子任务 4
  startup 自检打 warning；
- 首次 `stream()` 时：`_require_model()` 校验 model（**请求参数**，注入
  client 的测试也必须配）→ `_get_client()` 懒创建 `AsyncOpenAI`：

```python
AsyncOpenAI(
    base_url=s.llm_api_base,
    api_key=s.llm_api_key,
    timeout=Timeout(s.orchestrator_llm_timeout_seconds, connect=5.0),
    max_retries=0,
)
```

### F2 文本流透传

`async with stream:`（任意退出路径含消费者弃流都关底层连接）内逐块：
`getattr(delta, "content", None)` 真值才 yield。`getattr` 而非属性直取——
兼容桩与不同 SDK 版本的字段缺省。

### F3 tool_calls 分片聚合（六边界裁决表）

OpenAI 流式协议中工具调用按 `index` 分片到达：首片带 `id`/`name`，
后续片通常只有 `arguments` 增量。聚合器 `_ToolCallAcc` 按 index 分桶
（`dict` 保插入序），流结束后 `sorted` 输出。

| 边界 | 裁决 | 理由 |
|---|---|---|
| 首分片无 id | 晚到 id 覆盖；全程缺失则合成 `call_{index}` | 保 `tool_call_id` 配对协议（03 难点 3）永不断裂 |
| arguments 空串 | 聚合为空归一 `"{}"` | 合法空参 JSON；透传 `""` 会白耗一轮编排层解析失败回填 |
| finish_reason 在哪个分片 | 全程收集，终块统一携带 | 聚合发生在流结束，免疫分片顺序差异 |
| choices 为空的块 | `continue` | usage 尾块/keep-alive，非流结束非 delta |
| 多 index 交错 | 分桶 + `sorted` | 输出顺序确定，不串桶 |
| `tc.function is None` | 跳过 function 部分 | id-only 分片（真实存在于部分端点） |

### F4 reasoning 剥离与 thinking 开关（双保险）

- **服务端**（v1.1 三态）：`llm_thinking_enabled=None`（代码默认）**不发**
  该参数——任何 OpenAI 兼容端点安全（严格端点如 OpenAI 官方对未知参数
  返回 400）；`True`/`False` 显式开/关（方舟用户在 `.env` 配
  `LLM_THINKING_ENABLED=false`——seed 系列服务端默认可能开思考，关掉省
  计费、降 TTFT）；
- **传输层**：`delta.reasoning_content` 有意不读取不透传——参数失效或
  换模型时的纵深防御。

### F5 错误包装与结构化日志

- `try/except Exception` 只包 `create()` 与迭代段；配置校验在 try 之前
  （`LLMNotConfiguredError` 不被误包装）；
- 流正常结束后 `logger.info("llm_stream_done", model=…, chunks=…,
  tool_calls=…, finish_reason=…, latency_ms=…)`——**只记元数据**
  （R3 纪律），Key 与内容永不入日志。

### F6 生命周期

`aclose()` 幂等关闭底层客户端（镜像 `adapters/base.py` 模式）。适配器
设计为**应用级单例**（连接池复用），与请求级的编排层实例解耦——
lifespan 挂接属子任务 4。

### 3.7 接口原型

```python
class ArkLLMClient:
    def __init__(self, *, client: AsyncOpenAI | None = None,
                 settings: Settings | None = None) -> None: ...
    @property
    def is_configured(self) -> bool: ...
    async def stream(self, messages: list[dict],
                     tools: list[dict] | None = None) -> AsyncIterator[LLMChunk]: ...
    async def aclose(self) -> None: ...
```

完整实现见 [`backend/app/llm/adapter.py`](../../backend/app/llm/adapter.py)（~200 行，含逐决策注释）。

### 3.8 配置项追加（`config.py`）

| 配置 | 默认 | 说明 |
|---|---|---|
| `llm_model` | `None` | 模型名 / 方舟推理接入点 ID |
| `llm_thinking_enabled` | `None` | 三态：None=不发参数（任何端点安全，v1.1 默认）；True/False=显式开/关 |
| `llm_stream_include_usage` | `False` | usage 尾块计量（方舟兼容性待验证，默认关） |

复用既有：`llm_api_base` / `llm_api_key`（config.py 原有）、
`orchestrator_llm_timeout_seconds`（15s，03 预留）。

---

## 四、技术方案

### 4.1 选型说明

| 方案 | 结论 | 理由 |
|---|---|---|
| **openai SDK（AsyncOpenAI）** | ✅ 采用 | 方舟完全兼容 OpenAI 协议；SSE 解析/重连/类型化分片免费获得；`requirements` 早已声明 |
| httpx 裸 SSE | ❌ | 需手写 SSE 帧解析、分片类型建模——重造 SDK 已验证的部分，收益为零 |
| LangChain / LiteLLM | ❌ | 引入重型抽象换"多厂商切换"，而 P2 依赖倒置已用 40 行 Protocol 达成同一目标；违背"最小依赖" |

### 4.2 关键技术难点与解决方案

**难点 1：`read` 超时的真实语义。** `openai.Timeout(15)` 的 read=15s 是
**相邻两次读字节之间的间隔上限**，不是整条流的时长上限——思考模型只要
持续吐分片（哪怕全是被剥离的 reasoning）就不会触发。真正的暴露面是
**TTFT**（思考期若不吐字节，read 计时从等首字节开始）。缓解：默认关
thinking + connect 单独收紧 5s + 超时走环境变量免发版可调。

**难点 2：异步生成器的清理路径。** `yield` 之后消费者可能随时弃流
（SSE 断线，子任务 4 场景）。`async with stream:` 保证任意退出路径关
闭底层连接；`GeneratorExit` 是 BaseException 不被包装，透传给事件循环
正常回收。

**难点 3：配对协议保持。** 编排层要求 assistant.tool_calls 与 tool 回填
的 `tool_call_id` 严格配对（03 难点 3）。适配器侧的威胁是**厂商分片
缺 id**——合成 `call_{index}` 兜底保证 id 永不为空。

**难点 4：双份 requirements 漂移（实施中发现）。** 根目录与 backend 各
一份依赖清单且互不同步——backend 缺 `openai` 甚至缺已 import 的
`structlog`/`tenacity`/`pyjwt`/`pyyaml`/`asyncpg`，环境靠历史残留能跑。
处置：backend 清单补齐（含版本下限对齐环境实装），根清单改**伞形引用**
（`-r backend/...`），backend 成为唯一事实源。

### 4.3 联调实测记录（2026-10-09，`--live`）

| 指标 | 实测 |
|---|---|
| 链路 | "我明天有什么课" → `timetable_query`（真 call_id，参数分片聚合正确）→ 75 token 流式作答 |
| TTFT | 6.38s（首轮 3.5s 含一次工具调用决策） |
| 总耗时 | 7.30s |
| 安全项 | 模型未伪造身份字段本次未触发；student_id 注入链路正常（脱敏摘要 `2023**01`） |
| 日志 | 仅元数据（model/chunks/tool_calls/finish/latency）✓ |

---

## 五、实施计划

### 5.1 任务分解（WBS）

| WBS | 任务 | 输出物 | 验收 |
|---|---|---|---|
| 3.0 | 依赖与配置 | requirements ×2 / config.py / .env | Settings 读到三项；既有测试全绿 |
| 3.1 | 包骨架 + 异常 + 懒创建 | `app/llm/` | AC-11/13 |
| 3.2 | 文本流实现 | adapter.py | AC-1/2/3/8 |
| 3.3 | tool_calls 聚合 | adapter.py | AC-4/5/6/7 |
| 3.4 | 错误包装 + 日志 | adapter.py | AC-9/10 |
| 3.5 | 请求参数与开关 | adapter.py | AC-12/15 |
| 3.6 | E2E 协议兼容 | test_llm_adapter.py | AC-14/16 |
| 3.7 | demo 联调开关 | demo_orchestrator.py | 离线不变；live 跑通 |
| 3.8 | 文档四件套 | 05/06/README/03 导航 | 章节齐、链接可达 |
| 3.9 | 终验 | — | 全量 pytest 绿 + live 记录 |

### 5.2 时间节点

单日内闭环（实际：依赖/代码/测试/联调半天，文档半天）。

---

## 六、质量保障

### 6.1 测试策略（FakeOpenAI 设计）

三层桩，全部不触网络：

- `FakeAsyncStream`：伪 `openai.AsyncStream`（分片序列，Exception 项原样抛）；
- `FakeCompletions`：记录全部 `create()` kwargs + 按脚本弹出分片序列；
- `FakeOpenAI`：`SimpleNamespace` 树拼装伪 chunk（`ns_chunk`/`ns_tc`/
  `ns_usage_chunk` 构造器）——不用真 pydantic 模型：测的是适配器翻译
  逻辑而非 SDK 校验。

**测试分层**：单测（FakeOpenAI，协议形状）／联调（`--live` 真端点，
兼容性与延迟）。E2E 用例复用 `test_orchestrator.make_orch` 的 `llm=`
注入口，零改动。

### 6.2 验收标准（AC-IMPL05-1 ~ 16）

| AC | 用例 | 结果 |
|---|---|---|
| 1 | 纯文本流逐块透传（拼接=原文、终块 stop、delta=None） | ✅ |
| 2 | reasoning_content 剥离（仅-reasoning 分片静默跳过） | ✅ |
| 3 | 空/None 分片跳过（终块仍发出） | ✅ |
| 4 | 单工具调用分片聚合（id/name/arguments 拼接、finish=tool_calls） | ✅ |
| 5 | 多 index 交错聚合（升序、不串桶） | ✅ |
| 6 | 首分片无 id 兜底（晚到覆盖；缺失合成 call_0） | ✅ |
| 7 | arguments 空串归一 "{}" | ✅ |
| 8 | 空 choices 块（usage 尾块）容错 | ✅ |
| 9 | 流中途异常包装（cause 链 + 原始类名） | ✅ |
| 10 | create 阶段异常包装（无块产出） | ✅ |
| 11 | 未配置懒抛（含"仅缺 model 时注入 client 也抛"） | ✅ |
| 12 | 请求参数正确性 + SDK 构造参数（max_retries=0 / Timeout 5+15） | ✅ |
| 13 | aclose 幂等 | ✅ |
| 14 | **E2E**：事件序列 `[THINKING, TOOL_CALL, TOOL_RESULT, TOKEN, DONE]`、落库四消息配对 | ✅ |
| 15 | thinking/usage 开关 | ✅ |
| 16 | **E2E**：首请求失败 → `error(llm_unavailable)` | ✅ |

回归：既有 67 用例零回归（全量 84：78 过 + 6 跳过，跳过均为 Redis 联调门控）。

### 6.3 风险清单与缓解

| # | 风险 | 缓解 |
|---|---|---|
| ① | 方舟 SSE 与 OpenAI SDK 兼容性（finish_reason 词表 / tools 空表 / stream_options 接受度） | 终块 `or` 兜底；空表不带 tools；usage 默认关；`--live` 联调实测 ✅ |
| ② | 思考模型 TTFT 击穿超时 | 默认关 thinking；实测 TTFT 6.38s（含工具轮）；超时环境变量可调 |
| ③ | 配置错误（Key/模型 ID） | 统一映射 `llm_unavailable` 用户文案，不泄露细节；demo live 打印异常类名排障 |
| ④ | SDK 3.x 漂移 | 只用最小稳定 API 面（AsyncOpenAI/Timeout/create/stream/extra_body） |

**遗留项（联调发现，非本任务范围）**：

1. **模型无日期感知**：实测中模型把"明天"解析为 2025-11-28（其训练
   截止附近的日期感）——system prompt 未注入当前日期。留给 prompt
   内容工程（3/4 持续项）：在 system prompt 或首条消息注入今日日期。
2. TTFT 6.38s 偏高（网络 + 首轮工具决策），SSE 端点上线后需在真实
   并发下复测。

---

## 七、预期效益分析

- **对话链路首次真实可用**：编排层 842 行逻辑获得真模型验证，项目从
  "组件正确"进入"产品存在"前夜（差一个 SSE 端点）；
- **厂商锁定解除**：P2 闭环落地，切任何 OpenAI 兼容端点只改 `.env`；
- **依赖清单治理**：双 requirements 漂移修复，backend 成为唯一事实源；
- **联调基建**：`--live` 开关成为后续所有真机验证的标准入口。

---

## 八、附录

### 8.1 与现有约定的一致性核对

| 约定 | 核对 |
|---|---|
| 03 §2.4 冻结协议 | 三条约定逐条兑现（§2.3） |
| P2 依赖倒置 | `openai` 符号零外泄（异常经 `LLMStreamError` 包装） |
| P5 出口完备 | 未配置不阻止启动；失败统一映射 `llm_unavailable` |
| R3 敏感信息 | reasoning 不透传；日志只记元数据；Key 不入日志 |
| 适配器家族风格 | 懒创建 / aclose / 未配置异常三模式与 `adapters/base.py` 一致 |

### 8.2 术语表

| 术语 | 含义 |
|---|---|
| TTFT | Time To First Token，首字延迟 |
| 分片（fragment/chunk） | 流式响应的单个 SSE 事件，含部分 delta |
| 聚合末块 | 适配器在流结束后产出的唯一携带 tool_calls 的 `LLMChunk` |
| thinking / reasoning | 豆包思考模型的推理过程文本，计费且对用户不可见 |
| 伞形引用 | 根 requirements 仅 `-r` 引用 backend 清单的组织方式 |

### 8.3 参考实现链接

- 适配器实现：[`backend/app/llm/adapter.py`](../../backend/app/llm/adapter.py)
- 单元测试：[`backend/tests/test_llm_adapter.py`](../../backend/tests/test_llm_adapter.py)
- 联调入口：[`backend/demo_orchestrator.py`](../../backend/demo_orchestrator.py)（`--live`）
- 冻结协议：[`backend/app/orchestrator.py`](../../backend/app/orchestrator.py) §协议区
- 方舟 API 文档：https://www.volcengine.com/docs/82379

---

← 上一篇：[子任务 2：编排核心 Orchestrator](./03-编排核心Orchestrator设计.md)
下一篇：[06-LLM适配器学习指南](./06-LLM适配器学习指南.md)

*文档版本 v1.1（thinking 三态化） ｜ 阶段一编排层子任务 3 ｜ 下一步动作：子任务 4（SSE 端点）按本文 §2.4 异常模型与 03 §2.3 事件协议并行开发*
