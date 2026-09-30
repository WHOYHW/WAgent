# 学习指南 — Agent 编排核心 Orchestrator（任务设计 W2 配套）

> 配套实现：[`backend/app/orchestrator.py`](../../backend/app/orchestrator.py) + [`backend/tests/test_orchestrator.py`](../../backend/tests/test_orchestrator.py)
> 配套设计：[`03-编排核心Orchestrator设计.md`](./03-编排核心Orchestrator设计.md)
> 阅读时长：约 45 分钟 ｜ 实操时长：90 分钟

---

## 一、为什么 Agent 需要"编排层"

阶段一后端已有五块基建，但它们**彼此没有连通**：

```
工具层（6 工具 + dispatch 统一调度）   ┐
会话存储（SessionStore）              ┤
护栏（check_redlines 关键词通道）      ├─→ 各自独立，没人串起来
转人工（handoff_create 工具）          ┤
LLM 配置（config 里只有 api_key）      ┘
```

结果是：`/agent/*` 六个接口是无状态请求-响应——学生不能多轮对话、LLM 不在链路里、红线词靠调用方自觉调用。

**编排层 = 把这五件事串成"对话循环"的发动机。** 学生说一句话，编排层要回答：这是谁（会话身份）？能说吗（红线）？记得什么（历史与压缩）？要查什么（工具路由）？查多久（迭代上限）？怎么答（流式生成）？

一个最小的对话循环伪代码：

```python
async def run(student_id, user_input, session_id):
    session = 会话准备(student_id, session_id)          # F1
    if 命中红线(user_input):                             # F2
        return 固定话术 + 转人工工单
    history = 加载并按需压缩(session)                     # F3
    messages = [system_prompt, *history]
    while True:                                          # F4 Agent Loop
        chunks = llm.stream(messages, tools)
        if 末块带 tool_calls:
            for call in tool_calls:
                result = registry.dispatch(call)          # 查数据
            messages += [assistant(tool_calls), *tool结果]  # 回填
            continue                                      # 再问 LLM
        return 流式输出最终回答                             # F6 收尾
```

---

## 二、本任务交付了什么

```
backend/app/
├── orchestrator.py            ← 核心实现（协议区 + 常量区 + Orchestrator 主类，~700 行）
├── config.py                  ← +4 项编排层配置（工具预算 / 摘要字数 / LLM 超时 / 探测窗口）
└── conversation/store.py      ← P0-1 前置修正：load_history 改"最近 N 条"尾部窗口语义

backend/tests/
├── test_orchestrator.py       ← 新增 17 条用例（覆盖设计文档 §6.2 十五条 AC）
└── test_conversation_store.py ← 两处断言同步修正（P0-1）

backend/demo_orchestrator.py   ← 可运行 demo（内存库 + FakeLLM 驱动完整链路）
```

**一行话概括**：`Orchestrator.run(student_id, user_input, session_id)` 是唯一入口，产出六类事件的异步流，把会话/护栏/工具/LLM/审计全部串起来。

---

## 三、一次对话的完整旅程

以 demo 场景 1（"我明天有什么课"）为例，事件流按序产出：

```
thinking   {phase: session_ready}                ← F1 完成（会话就绪/重建）
tool_call  {tool: timetable_query, call_id, …}   ← LLM 决定查课表
tool_result{ok: true, meta: {source, synced_at, …}} ← 工具执行（含溯源三元组）
token      {delta: "明天有 1 节课："}             ← 最终回答的流式增量
token      {delta: "高等数学 08:00 一教-101。"}
done       {session_id, assistant_message_id, tool_iterations: 1, …}
```

两条特殊路径只改头尾，不改中段：

| 路径 | 头部 | 尾部 |
|---|---|---|
| 触发压缩 | `thinking(session_ready) → thinking(compressing)` | `done(compressed=true)` |
| 红线拦截 | `thinking(session_ready)` 后**直接** `tool_call(handoff_create)` | `done(blocked=true, handoff_ticket_id=…)`，LLM 全程零调用 |
| 熔断 | …`tool_call/tool_result`×6 → `thinking(budget_exhausted)` | `done(budget_exhausted=true)` |

### 3.1 六大功能模块地图

| 模块 | 职责 | 关键机制 |
|---|---|---|
| F1 会话准备 | 新建/续用/过期重建/**所有者校验** | 不匹配视同不存在（静默新建 + `owner_mismatch` 审计） |
| F2 红线拦截 | 命中即中断 + 转人工工单 | 检查点先于一切 LLM 调用；固定话术不经 LLM |
| F3 会话压缩 | 活跃 > 20 条 → LLM 摘要 ≤200 字 → `store.compress` | 探测窗口 60 条；失败降级以 probe 为上下文 |
| F4 工具路由 | tool_calls → dispatch → 结果回填 → 再问 LLM | 参数净化（P6）；失败回填不中断循环 |
| F4b 写门禁 | `library_renew` 的 confirmed/幂等键服务端可信化 | 三重校验：预览痕迹 + 确认词 + 条码绑定 |
| F5 熔断 | 单轮 ≤6 次工具调用，超限软熔断 | 批级原子拒绝 + "拒绝即 fused" + 收尾请求至多一次 |
| F6 收尾 | 消息落库 + done/error 终态 | 中间消息**每轮即时落库** |

---

## 四、三个冻结的协议（子任务 3/4 的对接契约）

### 4.1 事件流协议（子任务 4 SSE 端点消费）

```python
class EventType(str, Enum):
    THINKING = "thinking"        # 阶段信号
    TOOL_CALL = "tool_call"      # LLM 决定调用某工具
    TOOL_RESULT = "tool_result"  # 工具执行结果摘要（含 meta 溯源）
    TOKEN = "token"              # LLM 文本增量
    DONE = "done"                # 终态：完成
    ERROR = "error"              # 终态：编排层自身故障

@dataclass
class Event:
    type: EventType
    data: dict[str, Any]
    session_id: str | None = None   # 必回传：新会话/重建时前端需替换
    trace_id: str | None = None
```

子任务 4 只需把 `Event` 序列化为 `event: <type>\ndata: <json>` 的 SSE 帧。注意两条约定：**工具失败不是 error**（那是 `tool_result(ok=false)`，对话继续）；**error 是迭代器的最后事件**（done 不再发出）。`error` 事件的 `data.code` 有三个取值：`storage_error`（会话竞态过期）、`llm_unavailable`（主循环首请求即失败）、`internal`（兜底）。

### 4.2 LLMClient Protocol（子任务 3 适配器实现）

```python
class LLMClient(Protocol):
    def stream(self, messages: list[dict],
               tools: list[dict] | None = None) -> AsyncIterator[LLMChunk]: ...
```

约定：`delta` 与 `tool_calls` 不同时出现；`tool_calls`（聚合完成态）只在流的**最后一个块**。OpenAI SDK 的增量分片拼接、重试、超时全部封装在子任务 3 内——编排层只见"token 块 + 末块聚合结果"。

### 4.3 消息组装顺序

```
[system: DEFAULT_SYSTEM_PROMPT]   ← 固定系统提示词，永远第一
[system: 摘要]                     ← is_summary=True 的行，紧随其后（至多 1 条活跃）
[...其余历史消息按 seq 原序]        ← user/assistant/tool
```

---

## 五、关键设计决策与坑（必读）

### 5.1 为什么 LLM 是 AsyncIterator 而不是"回调 + 聚合返回"

**坑**：Python 的 async generator 无法在 `await llm.complete(..., on_token=fn)` 期间 `yield` 事件——回调发生在另一个栈上，你拿到 token 时没有地方"吐"给消费者。生产者-消费者队列能解，但引入任务编排复杂度。

**方案**：把 LLM 定义为异步流之后，token 透传退化为一行：

```python
async for chunk in self.llm.stream(messages, tools=tools):
    if chunk.delta:
        yield self._emit(EventType.TOKEN, {"delta": chunk.delta}, session)
```

这是**依赖倒置的典型落点**：流式复杂度（增量拼接/重试/超时）下沉给适配器，编排层保持简单；单测注入 `FakeLLM`（普通类，`Protocol` 结构化子类型无需显式继承）即可离线跑通全链路。

### 5.2 GeneratorExit：断线清理禁止 await

**坑**：客户端断开 SSE 时，async generator 被 `aclose()`，注入 `GeneratorExit`——**此时 finally 内禁止 await**（否则抛 `RuntimeError: async generator ignored GeneratorExit`）。你不能在 finally 里"把没落库的消息补写进数据库"。

**方案**：中间消息**每轮即时落库**（而不是攒到结尾一次写）：

```python
# 每轮工具执行完立刻持久化
await self.store.append_many(session.session_id, [assistant_in, *tool_msgs])
```

断线时最多丢"最终 assistant 消息"一条（宁可少一条，不留半截话术）。代价是写放大（单轮 ≤13 条消息），换来三点：断线安全、GeneratorExit 约束、**F4b 跨轮确认有据可查**（预览轮的 tool_calls 必然已落库）。

### 5.3 OpenAI tool_calls 配对协议

**坑**：协议要求 assistant 消息里的每个 `tool_call` 必须有配对的 `role=tool` 消息（按 `tool_call_id` 对应），缺配对下一轮请求直接 400。**四条路径都会产生"没执行"的 call**：批级拒绝、JSON 解析失败、未知工具名、工具异常。

**方案**：任何"没执行"的 call 都回填统一格式的 tool 消息：

```python
MessageIn(role="tool", content="未执行：已达单轮工具调用上限（6 次）",
          tool_call_id=c.id)
# 失败路径：content="调用失败：<原因>"
```

### 5.4 "拒绝即熔断"：v1.0 的死循环窗口

**坑**（设计评审发现的原缺陷）：v1.0 的熔断是"批级拒绝后 `continue`"——若 used=4、当前批 3 个，拒绝后 `used < budget` 仍成立，下一轮**仍携带完整工具表**请求 LLM，LLM 大概率再给一批 3 个 → 再拒绝 → 在"LLM↔拒绝"之间无限打转。预算只限制工具执行，不限制 LLM 调用次数。

**方案**：拒绝时**立即置位 `fused` 标志（永不清零）**，此后所有 LLM 请求不带 `tools`：

```python
if used + len(batch) > self.settings.orchestrator_tool_budget:
    new_msgs = await self._reject_batch(session, batch, trace_id)
    ...
    fused = True          # 拒绝即熔断
    continue              # 下一轮进收尾分支（不再带 tools）
```

收尾请求"至多一次"，且**防御性忽略**幻觉 tool_calls（只取 delta 文本）；收尾本身失败还有硬兜底固定文案。LLM 调用次数上界 = 执行轮 + 1 拒绝轮 + 1 收尾轮，可被单测锁定（`len(FakeLLM.calls) == 4`）。

### 5.5 参数不可信（P6）：三个字段只能由服务端注入

LLM 生成的工具参数是**不可信输入**。身份（`student_id`）、授权（`confirmed`）、幂等锚（`idempotency_key`）在 `_dispatch_one` 里统一处理：

```python
args.pop("student_id", None)          # 所有工具：剥离伪造身份
args["student_id"] = ctx.student_id   # 服务端注入（JWT sub）

if call.name == RENEW_TOOL_NAME:      # 写工具：授权字段同样服务端决定
    args.pop("confirmed", None)
    args.pop("idempotency_key", None)
    args["confirmed"] = self._renew_gate(user_input, history, barcodes)
    args["idempotency_key"] = make_idempotency_key(
        ctx.session_id, barcodes, date.today().isoformat())
```

为什么 `confirmed` 不能信 LLM：它是"用户已知情并同意"的**授权断言**——信任模型自填等于让模型替用户做授权决定。两轮确认协议把授权锚定在三类**落库可查证的确定性事实**上：

| # | 校验 | 防什么 |
|---|---|---|
| 1 | 会话尾部存在落库的 `library_renew` tool_calls（预览轮证据） | 无中生有的确认（首轮就执行） |
| 2 | 本轮 user_input 命中确认关键词（"确认续借"等） | 模型代替用户表达意愿 |
| 3 | 本轮 barcodes 与预览轮完全一致 | "确认 A 却执行 B"的条码偷换 |

> 关键词误报的取舍：用户语境中说"确认"但并非续借确认 → 门禁放行。误报落在安全方向（续借可逆 + 幂等键绑定"当日+该批条码"），阶段一接受；语义级确认理解留给阶段二。

### 5.6 探测窗口 60 > 阈值 20：为什么压缩曾"永不触发"

**坑**（P0-1，子任务 1 交付物缺陷）：`load_history` 原实现是 `order_by(seq.asc()).limit(n)`——返回**最早** n 条。编排层若按默认 `limit=20` 加载后判 `len(history) > 20`，返回长度恒 ≤ 20，**条件永远为假**；即便显式传大 limit，"最早 N 条"语义会把刚落库的本轮 user 消息截在窗口外。

**方案**：两步——
1. `load_history` 改尾部窗口（倒序取最近 n 条，内存反转为正序）；
2. 编排层显式传 `orchestrator_history_fetch_limit=60` 探测，一次查询三用：触发判定（60 > 20 才看得见超阈值）/ 摘要输入（`probe[:-6]` 全量在握）/ 降级上下文（失败时直接用 probe，最新消息必在窗口内）。

### 5.7 所有者校验与存在性泄露

`session_id` 是**路由标识，不是授权凭证**。持有/猜到他人会话 ID 即可读写他人历史（水平越权）。F1 的处理有个精妙点：**所有者不匹配与"会话不存在"走完全相同的路径**（静默新建）——客户端无法从响应差异区分两种情况，天然抵抗枚举探测；但 `owner_mismatch` 审计已留痕（越权是安全事件，必须可追查）。

### 5.8 终评修复的四条加固（评审驱动）

独立代码评审揪出了四个隐蔽缺陷，修复方式本身就是设计课：

1. **混合红线命中的路由降级（Critical）**。guardrail 按 R1→R3→R4→R5 字典序收集命中，`rules[0]` 恒为低危规则。用户说"我挂科了，不想活了"同时命中 R1+R5：话术走了 R5 生命通道，工单却按 `rules[0]=R1` 被路由成 **P2/教务办**——危机工单静默降级。修复：`rule = "R5_生命优先" if gr.life_priority else gr.rules[0]`，命中列表 `hit_rules` 仍全量留痕。教训：**"取第一个命中"这类顺序依赖，要问一句顺序是谁定义的**。
2. **handoff_create 不进 LLM 工具表**。它本来只应由 F2 确定性路径调用；若出现在 `openai_tools()` 暴露给 LLM，模型可以伪造 `hit_rules`/`priority` 生成 P0 工单，污染人工坐席队列。修复：`_llm_tools()` 按 `LLM_EXCLUDED_TOOLS` 过滤。对照 P6 的推广：**不可信的不仅是工具参数，还有"谁来发起调用"本身**。
3. **落库先于工单**。红线路径原顺序是"先派工单再落库"——工单库故障时，危机输入既不持久也不可见。修复：user+话术先 `append_many`（被拦截的对话仍是完整会话，可审计可回放），工单失败只影响 `done.handoff_ticket_id` 回传，不阻断话术输出（P5：每个分支都有用户可见出口）。
4. **失败路径的协议补全**。主循环首请求失败改为专用 `LLMStreamFailure` → `error(llm_unavailable)` 终态（原来是笼统的 `internal`）；工具调用失败路径的 `tool_call` 事件同样携带 `args_digest`（不可解析时只放 `{"parse": "failed"}` 标记——原文可能含模型伪造的他人身份字段，不进事件流）。

---

## 六、测试组织方式

`test_orchestrator.py` 共 17 条用例，按设计文档 §6.2 AC 表分组：

```
AC-IMPL03-1  压缩触发           → test_compression_triggers_and_rebuilds_context
AC-IMPL03-2  摘要 ≤200 字       → test_summary_truncated_to_max_chars
AC-IMPL03-3  压缩失败降级       → test_summary_failure_degrades_gracefully
AC-IMPL03-3b 边界 20 不触发     → test_boundary_exactly_20_active_no_compression
AC-IMPL03-4  迭代上限熔断       → test_budget_fuse_rejects_and_terminates
AC-IMPL03-5  拒绝批配对+不重试  → test_rejected_batch_pairing_and_no_tool_retry
AC-IMPL03-6  红线拦截           → test_redline_r1_blocks_before_llm
AC-IMPL03-7  R5 生命优先        → test_redline_r5_lifeline_channel
AC-IMPL03-8  student_id 防越权  → test_forged_student_id_stripped
AC-IMPL03-9  工具失败不中断     → test_tool_failure_feeds_back_to_llm
AC-IMPL03-10 正常事件序列       → test_normal_one_tool_call_event_sequence
AC-IMPL03-11 过期自动重建       → test_expired_session_rebuilds
AC-IMPL03-13 所有者校验         → test_owner_mismatch_silently_rebuilds
AC-IMPL03-14 写门禁四场景       → test_write_gate_{forged,confirm,without,barcode_swap}
AC-IMPL03-15 load_history 回归  → （子任务 1 测试文件内修正后随全量回归）
```

### 6.1 两个测试桩的设计

**FakeLLM**（脚本化）：`script` 是"每次 stream() 弹出的 chunk 序列"的列表，同时记录每次收到的 `messages`/`tools`——断言"LLM 是否被调、看到了什么"全靠它。脚本元素还可以是 `Exception` 实例（失败注入）：

```python
async def stream(self, messages, tools=None):
    self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
    for chunk in self.script.pop(0):
        if isinstance(chunk, Exception):
            raise chunk      # 脚本化的失败注入
        yield chunk
```

**LooperLLM**（无限工具调用）：tools 非 None 时永远返回一批 tool_calls（每次新 id），tools is None（收尾请求）时返回文本——专门用来验证熔断状态机：如果熔断实现有缺陷，测试会**挂起而不是失败**（死循环的真实表现）。

### 6.2 一个反直觉的计数选择

T2 断言"6 次 dispatch"用的是 **audit_log 行数**而不是桩适配器的 `calls` 计数——因为课表工具命中缓存时不触达适配器，桩计数不可靠。审计行是"真正发生了什么"的事实来源。

---

## 七、动手实验（建议 90 分钟）

### 实验 1：跑通测试，对照 AC 表

```bash
cd E:\Agent\backend
python -m pytest tests/test_orchestrator.py -v
```

### 实验 2：跑 demo，肉眼看事件流

```bash
python -m demo_orchestrator
```

**关键观察**：
- 场景 1 中 `tool_call` 事件的 `args_digest.student_id = "2023**01"`——demo 里 LLM 传的是伪造的 `99999999`，被剥离后注入了真实身份（脱敏展示）；
- 场景 2 中红线路径**没有任何 LLM 调用**（`LLM 是否被调用：True` 表示"零调用"断言通过），事件流直接从 `thinking` 跳到 `tool_call(handoff_create)`。

### 实验 3：把预算调到 2，观察熔断

修改 demo 里 `Settings(orchestrator_tool_budget=2)`，让 FakeLLM 无限返回工具调用（参考 `LooperLLM`），观察：执行 2 次 → `thinking(budget_exhausted)` → 收尾 `token` → `done(budget_exhausted=true)`。再故意让收尾请求抛异常，观察硬兜底固定文案。

### 实验 4：手工走一遍两轮续借确认

用 `test_write_gate_confirm_round_trips` 作参考：第一轮让 FakeLLM 调 `library_renew`（伪造 `confirmed=true`），观察它被门禁置 False 并收到 PreconditionError 回填；第二轮传 `user_input="确认续借"` + 相同 barcodes，观察放行。再试试换 barcodes（T8d：条码偷换被拒）。

---

## 八、常见问题（FAQ）

**Q1：为什么红线检查放在消息落库之前、LLM 之前？**
A：位置即保证。"先于一切 LLM 交互"意味着 LLM 未被调用就不存在绕过——这是"召回率 100%"的工程表达（物理保证，而非概率保证）。被拦截的 user 消息与话术**仍然落库**（在拦截路径内 append_many），保证可审计、可回放。

**Q2：拦截话术为什么是固定模板而不是让 LLM 生成柔性回复？**
A：拦截要求"立刻中断"——固定模板可单测逐字断言、可审计、零延迟、零二次风险。语义级柔性拒答留给阶段二 L2 通道。

**Q3：摘要失败后为什么不重试？**
A：压缩是**优化**不是正确性依赖。降级路径以 probe（最近 60 条）为上下文继续对话——尾部窗口语义保证本轮 user 消息必在窗口内，正确性无损，只是 token 消耗增大。可用性优先。

**Q4：`done` 事件为什么必带 `session_id`？**
A：三种情况会产生**新** session_id：首次对话、过期重建、所有者不匹配重建。前端需要用它静默替换本地会话标识，下一轮才能续上上下文。

**Q5：为什么编排层自己不实现 LLM 调用（OpenAI SDK 直连）？**
A：依赖倒置（P2）——接口形状由编排层需求倒推定义（`LLMClient` Protocol），实现归子任务 3。好处：编排层单测零网络依赖；子任务 3 换供应商/加重试不影响编排层；两者可并行开发。

**Q6：预算计数为什么按 tool_call 条目而不是 LLM 轮次？**
A：一轮可并行多工具。按条目计数才能严格满足"最多 6 次工具调用"；解析失败的条目也计数（防"反复产出坏 JSON"死循环）。

---

## 九、自测题

请尝试独立回答，再对照答案：

1. 客户端在工具执行到一半时断开 SSE 连接，哪些消息会丢？为什么不能在 finally 里补写？
2. LLM 在 `arguments` 里自填 `"confirmed": true`，这条续借会执行吗？写出整条判定链。
3. `used=4`，LLM 返回一批 3 个 tool_calls——描述此后每一次 LLM 请求（带不带 tools）直到对话结束。
4. 会话活跃消息恰好 20 条时压缩触发吗？21 条呢？探测窗口为什么是 60 而不是 20？
5. 学生 B 携带学生 A 的 session_id 调用 `run()`，A 的历史会进 B 的 LLM 上下文吗？系统如何表现？留下了什么痕迹？
6. 为什么 `tool_result` 事件只放 `{count: 5}` 这样的摘要，完整结果去哪了？

<details>
<summary>参考答案</summary>

1. 只丢"最终 assistant 消息"一条（中间消息每轮即时落库）。async generator 被 `aclose()` 时注入 `GeneratorExit`，**finally 内禁止 await**——补写数据库需要 await，会抛 `RuntimeError`。所以清理只能做同步日志，落库必须在断线发生前完成。

2. 不会执行。`_dispatch_one` 剥离 `confirmed`/`idempotency_key` → `_renew_gate` 三重校验：① 扫历史尾部找落库的 `library_renew` 预览轮证据（首轮没有 → False）；② user_input 是否命中确认关键词；③ barcodes 是否与预览轮一致。任一不满足 → 注入 `confirmed=False` → `LibraryRenewTool.validate` 抛 `PreconditionError` → 回填失败 tool 消息，LLM 转述"需要确认"。

3. 拒绝该整批（4+3 > 6，批级原子性：绝不截断执行一半），每个 call 回填"未执行"tool 消息并落库 → 写 `budget_exhausted` 审计 → **立即置位 fused** → 下一次 LLM 请求（收尾）**不带 tools**，幻觉 tool_calls 一律忽略只取文本 → done(budget_exhausted=true)。若收尾也失败 → 硬兜底固定文案。共 2 次额外 LLM 调用（1 拒绝轮 + 1 收尾轮）。

4. 恰好 20 条不触发（`len(probe) > 20` 为假）；21 条触发。窗口取 60 是因为 `load_history` 默认 limit=20，用它探测永远看不到"长度 > 20"；60 > 20 才能检测超阈值，同时兜住摘要输入与降级上下文（一次查询三用）。

5. 不会。F1 所有者校验发现 `conv.student_id != student_id`，视同不存在——静默新建会话，B 的 LLM 上下文只有 system prompt + B 的 user 消息；A 的消息表无新增行。留下 `owner_mismatch` 审计行（action 记录被拒的 session_id）。

6. 事件流是给前端渲染的"进度信号"，搬运大 payload 会浪费带宽且明细无需前端理解；完整结果已作为 `role=tool` 消息回填给 LLM，由 LLM 在最终回答里用自然语言转述（P3：事实与语言分离——LLM 组织语言，事实来自工具返回值）。

</details>

---

## 十、下一步

完成本任务后，两个下游的接口契约已冻结，**可并行开发**：

1. **子任务 3（LLM 适配器）**：实现 `LLMClient.stream()` 一个方法——OpenAI SDK 流式增量聚合为 `LLMChunk` 序列（含 `orchestrator_llm_timeout_seconds` 超时与重试）；
2. **子任务 4（SSE 端点）**：`async for event in orch.run(...)` → `event: <type>\ndata: <json>` 帧转发，`Orchestrator` 按请求级构造（registry 内含请求级 db 会话）。

建议动手顺序：先写一个真的 OpenAI 兼容适配器替换 `FakeLLM` 跑通 demo，再写 SSE 端点。

---

*学习指南 v1.0 ｜ 阶段一编排层核心配套 ｜ 实现版本对应设计文档 v1.1（评审修订版）｜ 配套实现：[`backend/app/orchestrator.py`](../../backend/app/orchestrator.py) + [`backend/tests/test_orchestrator.py`](../../backend/tests/test_orchestrator.py) + [`backend/demo_orchestrator.py`](../../backend/demo_orchestrator.py)*
