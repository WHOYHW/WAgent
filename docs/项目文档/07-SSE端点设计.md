# 任务设计文档 — 子任务 4：SSE 端点（POST /agent/chat）

> 文档编号：WAgent-IMPL-07
> 版本：v1.0
> 适用阶段：阶段一 · 编排层（M2）收口
> 关联文档：[`03-编排核心Orchestrator设计.md`](./03-编排核心Orchestrator设计.md) §2.3（事件流协议，直接消费）｜ [`05-LLM适配器设计.md`](./05-LLM适配器设计.md) §2.4（异常模型）｜ [`12-阶段一-技术架构与数据契约.md`](../12-阶段一-技术架构与数据契约.md) §7 L4（身份注入约定）

### 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-13 | 初版 |

---

## 一、任务概述

### 1.1 背景：三件套就位，缺一张嘴

会话存储（子任务 1）、编排核心（子任务 2）、真 LLM（子任务 3）已交付并真机验证，
但驱动方式仍是命令行 `demo_orchestrator.py`。`main.py` 现有 6 个 `/agent/*` REST
接口是**调试面**——学生不会 POST `/agent/timetable`，他们只会说话。本任务交付
第一个**产品面接口**：`POST /agent/chat`，把 `Orchestrator.run()` 的六类事件流
经 SSE 推给前端。做完即可接网页/企业微信前端。

### 1.2 目标

| 编号 | 目标 | 验证方式 |
|---|---|---|
| G1 | 六类 Event → SSE 帧的忠实映射（含信封 session_id/trace_id） | AC-2/3 |
| G2 | 依赖装配可测试：LLM 与编排器经 FastAPI 依赖注入，测试零 monkeypatch | AC-8 |
| G3 | 断线清理履行 03 难点 2 约定（端点负责 aclose，编排层 finally 禁 await） | AC-9 |
| G4 | 首个 API 级测试基建（httpx ASGITransport），为后续端点铺路 | AC-1~10 |
| G5 | curl 真机联调一次（真 LLM 打字机效果） | §4.3 联调 |

### 1.3 范围

**做**：`/agent/chat` 端点、SSE 帧序列化、`get_llm`/`get_orchestrator` 依赖、
lifespan 接线（`ArkLLMClient` 单例 + startup 自检 warning）、配置门控 CORS、
API 级测试、07/08 文档。

**不做**：前端页面本身（另任务）；心跳/keep-alive 帧（首事件
`thinking(session_ready)` 立即到达、工具轮事件密集、压缩静默期 ≤15s 由超时兜底，
登记为已知限制）；OAuth/正式身份源（继续 JWT 开发态）；提示词内容工程
（日期注入等遗留项随本任务后单独迭代）。

### 1.4 与上游下游的关系

```
前端（fetch + ReadableStream，EventSource 不支持 POST）
  │ POST /agent/chat  {session_id?, content}  + Bearer JWT
  ▼
get_current_student（既有，student_id 注入）
  ▼
Orchestrator.run()（子任务 2，零改动） ── ArkLLMClient（子任务 3 单例）
  ▼
六类 Event → _sse() 帧序列化 → StreamingResponse(text/event-stream)
```

---

## 二、总体设计

### 2.1 设计原则映射

| 原则 | 落点 |
|---|---|
| 协议冻结（03 §2.3/05 §2.4） | 编排层与适配器零改动，本任务纯增量 |
| L4 身份注入（12 §7） | `student_id` 只来自 JWT（security.py），请求体同名段一律忽略 |
| P2 可测性 | `get_llm`/`get_orchestrator` 都是 FastAPI 依赖 → 测试用 `dependency_overrides`，不碰模块全局 |
| P5 出口完备 | 401/422 在流开始前返回；流一旦开始，一切失败收敛为 `error` 帧（编排层已保证） |

### 2.2 接口契约

**请求**

```
POST /agent/chat
Authorization: Bearer <JWT>            # student_id = sub
Content-Type: application/json

{"session_id": "sess_xxx" | null,     # 续用会话；null/失效/所有者不匹配 → 静默新建
 "content": "我明天有什么课"}           # 1~2000 字
```

**响应**：`200` + `text/event-stream`；响应头 `Cache-Control: no-cache`、
`X-Accel-Buffering: no`（防 nginx 缓冲）。

**状态码语义**（重要约定）：401（无/坏 token）与 422（body 校验失败）发生在
流开始前，由 FastAPI 正常返回；**流一旦开始，HTTP 层恒为 200**，一切失败以
`error` 帧表达（`llm_unavailable` / `storage_error` / `internal`）——SSE 无法
中途改状态码，前端以 `done`/`error` 帧为终态。

### 2.3 SSE 帧格式映射表

每帧 = 命名事件 + 单行 JSON data：

```
event: token
data: {"delta": "明天有", "session_id": "sess_x", "trace_id": "a1b2"}

```

`data` = `Event.data` 原样 + 信封字段（`session_id`/`trace_id` 非空时合入），
`json.dumps(ensure_ascii=False)`。六类事件逐一映射：

| Event | event: 名 | data 关键字段（03 §2.3 原约定） |
|---|---|---|
| thinking | `thinking` | `phase`：session_ready / compressing / budget_exhausted |
| tool_call | `tool_call` | `tool` / `call_id` / `args_digest`（已脱敏）/ `seq` |
| tool_result | `tool_result` | `tool` / `call_id` / `ok` / `error?` / `meta`（溯源）/ `result_digest` |
| token | `token` | `delta` |
| done | `done` | `session_id`（必回传，前端替换用）/ `blocked` / `compressed` / `tool_iterations` / `budget_exhausted` / `handoff_ticket_id?` |
| error | `error` | `code` / `message` |

**前端状态机依据**（03 §2.3 路径事件序列原文照录）：

```
正常（1 次工具调用）：
  thinking(session_ready) → tool_call → tool_result → token×N → done

触发压缩时，头部多一段：
  thinking(session_ready) → thinking(compressing) → tool_call → … → done(compressed=true)

红线拦截（LLM 全程不参与）：
  thinking(session_ready) → tool_call(handoff_create) → tool_result → token×N → done(blocked=true, handoff_ticket_id=…)

熔断（含批级拒绝路径）：
  …tool_call/tool_result×6 → thinking(budget_exhausted) → token×N → done(budget_exhausted=true)
  注：被拒批不产生 tool_call / tool_result 事件——那些调用从未执行；
      过程可见性由回填的"未执行"tool 消息保证（LLM 会在收尾文本中说明）
```

### 2.4 依赖装配（进程级 / 请求级）

```
进程级（模块单例）：_llm_client = ArkLLMClient()        ← lifespan shutdown aclose()
                    _shared_cache / _shared_services     ← 既有
请求级（每请求新建）：db(get_db) → Services → build_registry
                    → SessionStore → Orchestrator(store, registry, llm)
注入点（测试可替换）：get_llm() -> _llm_client
                    get_orchestrator(db, llm) -> Orchestrator
```

`get_orchestrator` 镜像既有 `_services(db)`（main.py）的装配哲学，但挂成
FastAPI 依赖——这是本任务对可测试性的唯一结构性要求。

---

## 三、功能详细设计

### F1 请求模型

`ChatRequest(BaseModel)`：`session_id: str | None = None`、
`content: str = Field(min_length=1, max_length=2000)`。与既有 6 个请求体模型
同放 `main.py`（对齐惯例，不新建 schemas 文件）。

### F2 帧序列化（纯函数）

```python
def _sse_frame(ev: Event) -> str:
    payload = dict(ev.data)
    if ev.session_id:
        payload["session_id"] = ev.session_id
    if ev.trace_id:
        payload["trace_id"] = ev.trace_id
    return f"event: {ev.type.value}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
```

### F3 SSE 包装生成器（断线清理核心）

```python
async def _sse(agen) -> AsyncIterator[str]:
    try:
        async for ev in agen:
            yield _sse_frame(ev)
    finally:
        # 03 难点 2：端点负责收尾。断线路径 finally 内 await 受限，
        # suppress 兜底防止清理本身炸裂；中间消息已即时落库（F4），
        # 断线最多丢最终 assistant 消息一条——"宁缺半截"。
        with contextlib.suppress(Exception):
            await agen.aclose()
```

### F4 端点

```python
@app.post("/agent/chat")
async def chat(
    body: ChatRequest,
    student_id: str = Depends(get_current_student),
    orch: Orchestrator = Depends(get_orchestrator),
):
    return StreamingResponse(
        _sse(orch.run(student_id, body.content, body.session_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

### F5 生命周期接线

lifespan 追加：startup 时 `if not _llm_client.is_configured:
logger.warning(...)`（P5：不阻止启动，红线拦截等 LLM 零参与路径不受影响）；
shutdown 时 `await _llm_client.aclose()`（与 edu/lib 并列）。

### F6 CORS（配置门控）

`config.py` 新增 `cors_allow_origins: str = ""`（逗号分隔；空 = 不挂中间件，
生产行为不变）；`.env` 开发态配前端源。仅 `allow_origins` +
`allow_methods=["POST","OPTIONS"]` + `allow_headers`（Authorization/
Content-Type），不带凭证。

### 3.7 配置追加

| 配置 | 默认 | 说明 |
|---|---|---|
| `cors_allow_origins` | `""` | 逗号分隔前端源；空=不挂 CORS 中间件 |

---

## 四、技术方案

### 4.1 选型：StreamingResponse（标准库路径）

`sse-starlette` 的 `EventSourceResponse` 提供自动心跳与重连提示，但本任务
明确不做心跳（§1.3），六类帧的序列化一个纯函数足够——**不新增依赖**。

### 4.2 关键技术难点与解决方案

**难点 1：POST + SSE。** 浏览器原生 `EventSource` 只支持 GET。决策：保持 POST
（请求体可长、语义是"发消息"），前端用 `fetch` + `ReadableStream` 手工解析帧：
按 `\n\n` 切帧，每帧读 `event:` 与 `data:` 两行。最小前端示例：

```javascript
const resp = await fetch("/agent/chat", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}`,
             "Content-Type": "application/json" },
  body: JSON.stringify({ session_id: currentSessionId, content: input }),
});
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buf = "";
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buf += decoder.decode(value, { stream: true });
  let idx;
  while ((idx = buf.indexOf("\n\n")) >= 0) {
    const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
    const ev = Object.fromEntries(
      frame.split("\n").map(l => [l.slice(0, l.indexOf(":")),
                                  JSON.parse(l.slice(l.indexOf(":") + 2)] )]));
    // ev.event === "token" → 追加渲染 ev.data.delta
    // ev.event === "done"  → currentSessionId = ev.data.session_id
  }
}
```

**难点 2：断线清理的 GeneratorExit 语义（03 难点 2 的履约）。** 客户端断开 →
Starlette 取消响应任务 → `_sse` 在 yield 点收到取消/关闭 → finally 内
`await agen.aclose()` 在 GeneratorExit 路径受限，故 suppress 兜底。正确性由
编排层设计保证：其 finally 只做同步日志；中间消息每轮即时落库，断线最多丢
最终 assistant 消息。**AC-9 专项测试。**

**难点 3：测试注入（首个 API 级测试基建）。** 无既有先例。方案：
`httpx.AsyncClient(transport=ASGITransport(app=app))`（httpx 0.28.1 已验证可用）
+ `app.dependency_overrides`：`get_orchestrator` 覆盖为测试装配（内存 SQLite +
`make_services` 桩适配器 + FakeLLM，全部复用 conftest 既有件）。注意
ASGITransport 不跑 lifespan——恰好规避 `init_db()` 触碰 PG 引擎。

### 4.3 联调方案

```bash
# backend 目录
python -c "from app.security import encode_token; print(encode_token('20230001'))"
curl -N -X POST http://127.0.0.1:8000/agent/chat \
  -H "Authorization: Bearer <上一步的token>" \
  -H "Content-Type: application/json" \
  -d '{"content": "我明天有什么课"}'
```

---

## 五、实施计划（WBS）

| 步 | 任务 | 验收 |
|---|---|---|
| 4.0 | 落盘 07 设计文档 + config/.env（CORS） | 文档齐 |
| 4.1 | `ChatRequest` + `_sse_frame`/`_sse` + 端点 + `get_llm`/`get_orchestrator` | uvicorn 手测通 |
| 4.2 | lifespan 接线 + CORS 中间件 | startup warning 可见 |
| 4.3 | `tests/test_api_chat.py`（首个 API 级测试） | AC-1~10 绿 |
| 4.4 | 08 学习指南 + README + 05 导航 | 链接可达 |
| 4.5 | 终验：全量 pytest + curl 真机联调（真 LLM 一次） | 打字机效果肉眼确认 |

---

## 六、质量保障

### 6.1 测试策略

三层注入全部复用既有件：`db` 夹具（内存 SQLite）+ `make_services` 桩适配器 +
FakeLLM（从 `test_orchestrator` 导入）。覆盖层新增一层——**HTTP 层**
（httpx ASGITransport），验证的是"端点装配 + 帧序列化 + 鉴权"而非编排逻辑本身
（那已被 23+17 个用例覆盖）。分层原则：每层只测自己新增的东西。

### 6.2 验收标准（AC-IMPL07-1 ~ 10）

| AC | 用例 | 断言要点 |
|---|---|---|
| 1 | 正常流全链路 | 200 + text/event-stream；帧序 `thinking→tool_call→tool_result→token×N→done`；每帧可解析 |
| 2 | 帧格式忠实性 | token 帧 delta 拼接 = 脚本文本；done 帧含 session_id |
| 3 | 红线流 | `done(blocked=true)` + handoff_ticket_id；FakeLLM 零调用 |
| 4 | 错误流 | 首请求失败 → `error(code=llm_unavailable)` 帧、无 done 帧 |
| 5 | 会话续用 | 二连请求带 session_id，第二轮 LLM 上下文含第一轮消息 |
| 6 | 所有者不匹配 | 他人 token + 同 session_id → done 回传**新** session_id；owner_mismatch 审计存在 |
| 7 | 鉴权与校验 | 无 token → 401；空 content → 422（均在流开始前） |
| 8 | 依赖注入可替换 | 全程无 monkeypatch，仅 dependency_overrides |
| 9 | 断线清理 | 提前关闭响应 → `_sse` 正常收尾、无未处理异常外泄 |
| 10 | CORS 门控 | 配置后预检 OPTIONS 通过；默认（空）无 CORS 头 |

回归：既有 84 用例零回归。

### 6.3 风险清单与缓解

| # | 风险 | 缓解 |
|---|---|---|
| ① | 代理/网关缓冲导致"打字机"变"一次性输出" | `X-Accel-Buffering: no` + 联调肉眼验证 |
| ② | 压缩静默期（LLM 摘要最多 15s）无帧，网关超时断连 | 已知限制（§1.3）；后续按需加心跳注释帧 `: ping` |
| ③ | 断线后编排层半成品状态 | 中间消息即时落库 + "宁缺半截"设计（03），AC-9 验证 |
| ④ | Windows 控制台 curl 显示乱码 | `PYTHONIOENCODING=utf-8` 启动服务端（显示问题非协议问题） |

---

## 七、预期效益分析

- **产品面就绪**：学生第一次能"说话"而非调接口；接任意前端（网页/企微）只差渲染层；
- **API 测试基建铺路**：首个 httpx ASGI 测试模式，后续提醒引擎的 webhook、
  管理端点全部复用；
- **编排层四件套收口**：会话（1）→ 编排（2）→ 大脑（3）→ 嘴（4），阶段一的
  "对话能力"闭环完成，剩余为提醒引擎与评测两块独立拼图。

---

## 八、附录

### 8.1 与现有约定的一致性核对

| 约定 | 核对 |
|---|---|
| 03 §2.3 事件流协议 | 帧映射逐字段对照（§2.3 表），编排层零改动 |
| 03 难点 2 断线清理 | 端点 finally 负责收尾 + suppress 兜底（F3） |
| 05 §2.4 异常模型 | `error(llm_unavailable)` 帧直达前端，无新异常类型 |
| 12 §7 L4 身份注入 | `student_id` 仅来自 JWT；请求体同名段忽略 |
| main.py 既有装配哲学 | 单例/请求级分层与 `_services`/lifespan 一致 |

### 8.2 术语表

| 术语 | 含义 |
|---|---|
| SSE | Server-Sent Events，服务端单向流式推送协议（`text/event-stream`） |
| 帧（frame） | 一个 `event:` + `data:` + 空行的 SSE 消息单元 |
| 终态帧 | `done` 或 `error`——前端收到即结束本轮渲染 |
| dependency_overrides | FastAPI 依赖覆盖机制，测试替换装配的官方姿势 |

### 8.3 参考实现链接

- 端点实现：[`backend/app/main.py`](../../backend/app/main.py)
- API 测试：[`backend/tests/test_api_chat.py`](../../backend/tests/test_api_chat.py)
- 事件协议源：[`backend/app/orchestrator.py`](../../backend/app/orchestrator.py) §协议区
- SSE 规范：https://html.spec.whatwg.org/multipage/server-sent-events.html

---

← 上一篇：[子任务 3：LLM 适配器设计](./05-LLM适配器设计.md)
下一篇：[08-SSE端点学习指南](./08-SSE端点学习指南.md)

*文档版本 v1.0 ｜ 阶段一编排层子任务 4（收口） ｜ 下一步动作：提醒引擎 M5（调度/静默期/去重聚合/日频上限）*
