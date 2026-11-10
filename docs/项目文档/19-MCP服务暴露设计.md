# 实施设计 — 19：MCP 服务暴露（工具面开放 · 双入口 · 门控）

> 文档编号：WAgent-IMPL-19
> 版本：v1.1
> 适用阶段：阶段一 · 工具面开放横切（把 registry 只读工具按 MCP 协议暴露给外部 MCP 客户端）
> 上游文档：[`../12-阶段一-技术架构与数据契约.md`](../12-阶段一-技术架构与数据契约.md)（工具 Schema R1.1 与安全 L4 约定）、[`../11-阶段一-AI可执行与人工兜底边界矩阵.md`](../11-阶段一-AI可执行与人工兜底边界矩阵.md)（AI / AI+H / H 边界）、[`./18-知识库检索RAG设计.md`](./18-知识库检索RAG设计.md)（`kb_enabled` 门控哲学 D5 与 `kb_search` 门控注册）、[`./16-工程化基线设计.md`](./16-工程化基线设计.md)（分层契约，本任务新增 `app.mcp` 层条目）

### 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-11-09 | 初版（M1 先行）：D1~D4 四裁决（身份传输层 JWT 注入 / 只读工具显式白名单 / Streamable HTTP + stdio 双入口 / `mcp_enabled=False` 门控）、附录 A 暴露词表与排除理由、依赖裁决（官方 SDK `mcp>=1.20` 内置 FastMCP + 测试依赖 `asgi-lifespan`）、M2 实施切片与 AC-19a~d |
| v1.1 | 2026-11-10 | M2 交付回写：D1/D2/D3 落地形态实测修正（主 app 级身份中间件 + Starlette 1.6 Mount 307 坑归一、属性过滤 + 期望集冻结断言收敛、mount lifespan 坑接线）；§三新增 `campus_agent_chat`（3.4，MCP 专属完整护栏通道）；§五实施切片按实际交付物重写 + 分层位置修正（`app.mcp` 位于 `app.main` 之下、`app.orchestrator` 之上，v1.0 的「`app.tools` 之下」作废）；附录 A 补 chat 行；AC-19a~d 验收状态回写 |

---

## 一、范围与动机

**现状**：七工具（六工具 + `kb_search` 门控）只服务自有前端——`/agent/*` REST 面与 SSE `/agent/chat` 面经编排层调度。外部客户端（Claude Desktop、MCP Inspector、IDE Agent 等）想复用这套工具，只能逐个手写 REST 适配。

**MCP（Model Context Protocol）** 是工具暴露的事实标准协议：客户端标准化地发现（`tools/list`）与调用（`tools/call`）服务端工具。本任务把 registry 中**只读**工具按 MCP 暴露——外部客户端零适配复用同一套工具实现、脱敏审计（`audit_log`）与会话身份层；完整编排能力经 §3.4 `campus_agent_chat` 一个工具整体包装开放（事件流对 MCP 客户端收口为单一出参，不外露流式协议）。

**不在范围**：写工具直暴（`library_renew` / `reminder_subscribe`，写路径收敛到 §3.4 `campus_agent_chat` 会话协议）；OAuth2 授权服务器（MCP 规范的 auth 建议，阶段二按校方统一身份认证另行裁决）；MCP resources / prompts / sampling 原语（只暴露 tools 一种）。

---

## 二、决策点裁决

| # | 裁决 | 备选与理由 |
|---|---|---|
| D1 | **身份从传输层 JWT 注入，与 REST 面同源** | HTTP 档：复用 `security.decode_token` 的同一会话令牌（`Authorization: Bearer <JWT>`）。**v1.1 落地形态**：`StudentIdentityMiddleware` 是**主 app 级 ASGI 中间件**（`add_middleware`），按 `/mcp` 前缀裁剪——前缀内强制身份（无令牌「缺少会话令牌」/ 坏令牌「会话令牌无效或已过期」/ 缺主体「令牌缺少主体身份」统一 401，`www-authenticate: Bearer`，**不进 MCP 机制**），前缀外（含 REST `/agent/*`）纯透传；`finally` 复位 contextvar。身份载体用 **ContextVar**（`app/mcp/identity.py` `current_student`）而非 SDK Context 对象——刻意与 SDK 内部 API 解耦，SDK 版本升级不动这块；工具参数层永远不出现 `student_id`（chat 入参 Schema 断言锁死）。**两个实测坑回写**：① Starlette 1.6 `Mount` 编译为 `path + "/{path:path}"`（前缀后必须至少一个字符），客户端端点无尾斜杠 `/mcp` 不命中 → 外层 `redirect_slashes` 307——归一必须在路由前完成（中间件内把精确 `/mcp` 改写为 `/mcp/`），不能依赖客户端跟随重定向；② 不选「包裹子应用」形态：中间件在 mount 外层才能同时拦 401 与归一路径。**stdio 档无传输层**：仅限本地演示/联调（MCP Inspector / Claude Desktop），身份取环境变量 `MCP_DEMO_STUDENT_ID` 注入 contextvar（未配置拒绝启动），生产部署明示禁用 stdio 入口；`current_student.set` 必须先于 `mcp.run()`（anyio 任务继承调用方 context） |
| D2 | **只直暴 `write=False` 且 `boundary="AI"` 的读工具——属性过滤 + 期望集冻结断言** | **v1.1 实施收敛**（v1.0 的「显式白名单点名、不按属性过滤」修正）：暴露面 = 属性过滤（`spec.write is False and spec.boundary == "AI"`）+ `_EXPECTED_BASE` 期望集冻结断言——装配时实际词表 ≠ 期望集即 `RuntimeError` **fail fast**。防护语义与显式白名单等价且更强：新读工具注册时装配**直接失败逼人工评审**，暴露面不随注册静默扩大；同时免去手工维护白名单副本与属性标注的双份漂移。实施中发现的既有瑕疵佐证排除面必须硬性：`reminder_subscribe` 语义是写（订阅库幂等 upsert）但 spec 未标 `write=True`（默认 False）——属性过滤会把它错放行；M2 顺手修正标注（`write=True`），修正后它被属性过滤正确排除。期望集 = `{timetable_query, grade_query, library_loans}` ∪（`{kb_search}` if `kb_enabled`）；`kb_search` 受 `kb_enabled` 门控：False 时它根本不在 registry，两层门控叠加零冲突。漂移即失败已有自动化断言（`test_vocabulary_drift_fails_fast`） |
| D3 | **Streamable HTTP 挂现有 FastAPI + stdio 演示双入口，共享同一装配** | HTTP 档：官方 SDK（`mcp>=1.20` 内置 `mcp.server.fastmcp` 的 FastMCP，**不引独立的 fastmcp 2.x 包**——同名不同物，2.x 是另一社区框架，API 与官方 SDK 不兼容）构建。**v1.1 落地形态**：内层 `mcp.settings.streamable_http_path = "/"` + 外层 `app.mount("/mcp", mcp.streamable_http_app())`；**mount 不执行子应用 lifespan（python-sdk#1367）**，session manager 必须在 main.py lifespan 内经 `AsyncExitStack` enter/exit `mcp_lifespan`（yield 前 enter、后 exit）——不启动则所有 `/mcp` 请求静默失败。stdio 档：`python -m app.mcp` 独立入口（`__main__.py`），与 HTTP 档共享同一 `build_mcp_server()` 装配函数（`channel` 参数化：HTTP 档 `"mcp"`、stdio 档 `"mcp-stdio"`），工具行为零分叉；双门控拒绝启动（`mcp_enabled=False` 与 `MCP_DEMO_STUDENT_ID` 未配置各一道，exit 2）。不选：独立端口独立进程（部署面翻倍、身份层复用变复杂）；SSE 旧传输（MCP 2025-03-26 起被 streamable HTTP 取代，SDK 已弃用） |
| D4 | **`mcp_enabled` 默认 `false` 关门控（镜像 18 D5 哲学）** | False 时 `/mcp` 挂载**根本不发生**（不是挂上后报错），stdio 入口拒绝启动——「能力上线是运营决策，不是代码合并的副作用」（18 §二 D5 原文）。三重理由镜像 18 D5：① 默认零行为变化——现有部署合并后 `/mcp` 仍是 404，`mcp` 依赖装了等于没装；② MCP 是新的**外部**暴露面（REST 面只对自有前端），默认关符合最小暴露原则；③ 测试/联调显式开启，门控即天然隔离，CI 不触网纪律（16 §五）不受影响 |

---

## 三、暴露面设计

### 3.1 工具映射（M2 落地形态）

FastMCP server 逐工具注册：暴露词表（D2 属性过滤）内每个 `spec.name` → 一个 MCP tool。**描述逐字一致**：读工具 description 取 `spec.description`（防漂移有冒烟断言）；`campus_agent_chat` docstring 手写（registry 无此 spec，§3.4）。registry 是词表唯一真源，MCP 面零复制。

调用路径（读工具与编排层 `_dispatch_one` 同一入口，白拿三件套——脱敏审计、前置条件校验、工具失败通道）：

```
MCP tools/call → 属性过滤词表校验（D2）→ contextvar 取 student_id（D1，
  传输层中间件已写入）→ ToolContext(student_id, channel="mcp")
  → registry.dispatch(name, ctx, params) → Pydantic result model
  → model_dump(mode="json") → MCP tool result
```

每调用独立 `async with async_session_factory() as db` 会话 + 请求级 `Services`——审计免费复用 `registry.dispatch` 统一审计，`channel="mcp"` 可区分来源（stdio 档 `"mcp-stdio"`）。

MCP 暴露的是**单工具直调**原语：无 LLM 路由、无工具预算、无 SSE 事件流——调用方（MCP 客户端）自己当编排者。这正是 Claude Desktop / IDE Agent 的既有使用方式。需要完整编排能力的场景走 §3.4 的 `campus_agent_chat`。

### 3.2 出参与错误映射

- 出参：`registry.dispatch` 返回的 Pydantic 模型 `model_dump(mode="json")` 序列化为 MCP tool result 文本（JSON 字符串）；`is_stale` / 降级文案（`degrade.yaml`）原样透传，MCP 面不做二次包装。
- 错误映射：

| 源 | MCP 表现 |
|---|---|
| 无令牌 / 令牌无效 / 缺主体（HTTP 档） | 传输层 401，不进工具调用（与 REST 面 `get_current_student` 同语义） |
| `ToolError` / `PreconditionError` | `isError=True` + 既有文案（降级模板原样回填，调用方可读） |
| 白名单外 / 未知工具 | `isError=True`「未知工具」——不泄露词表构成 |

### 3.3 不暴露的 HTTP 调试面

`/agent/reminders/scan`、`/agent/reminders/stats`、`/agent/reminders/feedback` 是全局对账/埋点调试面，不是 registry 工具，天然不在暴露词表——无需裁决。

### 3.4 `campus_agent_chat`：MCP 专属完整护栏通道（v1.1 新增）

单工具直调（§3.1）不含 LLM 路由与编排护栏，只覆盖读场景。**写操作永不直暴**（`library_renew` 需会话级确认门禁）——外部客户端获得完整护栏体验（红线 F2、写门禁 F4b、熔断 F5 全数生效）的唯一通道是一个 MCP 专属工具 `campus_agent_chat`，**整体包装 `Orchestrator.run()`**：

- **入参**：`(question, session_id=None)`——与编排层 `run()` 签名同构；身份同 contextvar（D1），入参 Schema 永不出现 `student_id`；
- **事件流收口**：消费六类事件流，`TOKEN` 增量拼接为 `reply`、`DONE` 取 `session_id` / `blocked`、`ERROR` 的 `message` 文案并入 `reply`（编排层自身故障对 MCP 客户端是「一条诚实文案」而非协议错误）；
- **出参**：`{session_id, reply, blocked}`——`session_id` 续话时原样回传（会话协议）；`blocked` 原样透传（护栏语义不二次加工）；
- **会话协议即写路径**：想续借必须走 chat——确认门禁（F4b 的预览轮/确认轮）在编排层会话上下文内落地，MCP 直调绕过确认即不可接受，chat 是唯一合法入口；
- **审计**：身份仍走 contextvar，无身份（stdio 漏配场景的纵深防御）→ `raise ToolError`（`isError=true` 错误通道）——MCP 客户端对 isError 有专门处理路径，isError=false 的正常返回会被 LLM host 当真值转述，错误语义必须走错误通道而非伪成功 JSON。

---

## 四、配置与依赖

### 4.1 配置（`config.py`，M1 交付）

```python
# ---- MCP 服务暴露（文档 19 §二 D4）----
# 总门控：False 时 /mcp 不挂载、stdio 入口拒绝启动（镜像 kb_enabled 的 D5 哲学：
# 能力上线是运营决策，不是代码合并的副作用）。
mcp_enabled: bool = False
```

stdio 演示身份 `MCP_DEMO_STUDENT_ID` 环境变量是否提升为 config 字段，由 M2 按实现形态定（未配置拒绝启动是硬约束，形态自由）。

### 4.2 依赖（M1 交付）

| 文件 | 依赖 | 理由 |
|---|---|---|
| `backend/requirements.txt` | `mcp>=1.20` | 官方 SDK（WAgent 环境实测 1.20.0，Python 3.13 兼容），内置 FastMCP（`mcp.server.fastmcp`）；**不引独立的 fastmcp 2.x 包**（同名不同物，API 与官方 SDK 不兼容，混装必炸） |
| `backend/requirements-dev.txt` | `asgi-lifespan>=2.1` | httpx `ASGITransport` **不执行 lifespan**，而 MCP Streamable HTTP 会话管理器的任务组在 lifespan 中启动——不跑 lifespan 的测试请求直接挂起/失败，MCP 测试必需（M2 详述）。`LifespanManager` 是 ASGI 测试的标配补件，仅测试依赖，运行时零新增 |

---

## 五、M2 实施切片（已交付，按实际形态回写）

| 交付物 | 内容 |
|---|---|
| `app/mcp/identity.py` | `current_student` ContextVar + `StudentIdentityMiddleware`（主 app 级 ASGI 中间件，按 `/mcp` 前缀裁剪：前缀内强制 Bearer JWT、无/坏令牌 401 不进 MCP 机制、精确 `/mcp` 改写 `/mcp/` 消除 307、finally 复位 contextvar；前缀外纯透传） |
| `app/mcp/bridge.py` | `build_mcp_server(settings, registry, session_factory, services_factory, orchestrator_factory, channel="mcp")` 纯装配：属性过滤 + `_EXPECTED_BASE` 期望集冻结断言（漂移即 RuntimeError fail fast）；读工具 typed wrapper（签名手写 + params_model 校验，每调用独立 db 会话 → registry.dispatch 统一审计）；`campus_agent_chat` 整体包装 `Orchestrator.run()`（§3.4） |
| `app/mcp/server.py` | `mount_mcp(app, ...)`（streamable_http_path="/" + add_middleware + mount "/mcp"）+ `mcp_lifespan`（session manager.run()，由 main.py lifespan 经 AsyncExitStack enter/exit——python-sdk#1367） |
| `app/mcp/__main__.py` | stdio 独立入口 `python -m app.mcp`：双门控（`mcp_enabled=False` D4 / `MCP_DEMO_STUDENT_ID` 未配置 D1）拒绝启动 exit 2；`current_student.set` 先于 `mcp.run()`；审计 `channel="mcp-stdio"`（dev-only 演示通道） |
| `app/main.py` | `mcp_enabled=True` 时 mount /mcp（orchestrator_factory 镜像 `get_orchestrator` 依赖）+ lifespan 内 enter/exit `mcp_lifespan` |
| `tests/test_mcp_server.py` | 12 项零网络测试：门控（默认无挂载 / stdio 双门控 subprocess exit 2）、词表（kb 门控跟随 / 漂移 fail fast / 描述逐字一致）、身份（Schema 无 student_id / contextvar 注入 / 无身份结构化错误）、chat 收口（blocked 透传 / ERROR 并入 reply）、HTTP 档（LifespanManager 驱动完整 lifespan + 401/握手） |
| 分层契约 | `app.mcp` 进 layers：**位于 `app.main` 之下、`app.orchestrator` 之上**（chat 包装编排层、读工具桥接 registry——v1.0 预告的「`app.tools` 之下」作废）；`app.kb` 仍居 `app.tools` 之下不变；lint-imports 3 kept |

## 六、验收标准（AC-19）

- **AC-19a 门控默认零变化**：`mcp_enabled=False`（默认）时 `/mcp` 404、stdio 入口拒绝启动；显式置 true 才挂载。
- **AC-19b 暴露词表**：HTTP 档 `tools/list` 恰含 3 个读工具（`timetable_query` / `grade_query` / `library_loans`，`kb_search` 当且仅当 `kb_enabled=true` 时第 4 个出现）+ MCP 专属 `campus_agent_chat`（kb off → 4 工具，kb on → 5 工具）；`library_renew` / `reminder_subscribe` / `handoff_create` 在任何配置组合下永不出现。
- **AC-19c 身份注入**：无 `Authorization` 头 → 401；工具参数中伪造 `student_id` 被静默忽略，资源归属权以令牌主体为准（水平越权返回 404 不泄露存在性，与 REST 面同语义）。
- **AC-19d 测试零网络**：`LifespanManager` 驱动完整 lifespan（含 MCP 会话管理器任务组），httpx `ASGITransport` 进程内调用，不占端口不触网。

### 验收状态（v1.1 回写，2026-11-10）

| AC | 状态 | 证据 |
|---|---|---|
| AC-19a | ✅ | `test_gate_default_no_mount`（默认部署主 app 无 /mcp 路由）+ `test_stdio_gate_refuses_default` / `test_stdio_gate_refuses_without_demo_identity`（subprocess exit 2） |
| AC-19b | ✅ | `test_vocabulary_follows_kb_gate`（off→[campus_agent_chat, grade_query, library_loans, timetable_query]，on→+kb_search）+ `test_vocabulary_drift_fails_fast` + `test_read_tool_descriptions_match_registry` |
| AC-19c | ✅ | `test_chat_schema_never_has_student_id` + `test_chat_identity_from_contextvar` + `test_missing_identity_structured_error` + `test_http_401_and_handshake`（无/坏令牌 401，有效令牌 initialize 握手 200） |
| AC-19d | ✅ | 全套 19 项零网络（`test_mcp_server.py`，四层架构 L1~L3），HTTP 档 LifespanManager + ASGITransport；L4 真机冒烟 SMOKE PASS |

全量门禁：pytest **381 passed + 6 skipped** 零失败；ruff 全绿；lint-imports **3 kept**（`app.mcp` 入 layers 后契约数不变）；黄金集 fake 档 **24/24**。官方 SDK 实测协议握手：stdio_client / streamable HTTP 双通道 protocolVersion 2025-06-18 成功。

### 测试四层架构（v1.1）

| 层 | 手段 | 覆盖 | 网络 |
|---|---|---|---|
| L1 纯单测 | pytest，内存库 + 桩适配器 | 词表过滤（kb 门控两态精确集合）；wrapper 身份注入 / 无身份拒绝 / 参数校验失败（不落审计）；审计行 `channel=="mcp"` 落库（含 mcp-stdio 区分 + student_id 脱敏铁证）；Schema 漂移机器防（wrapper 签名参数集 == `ToolSpec.parameters` 字段集，`student_id` 服务端注入字段除外） | 零 |
| L2 SDK 内存会话 | `mcp.shared.memory.create_connected_server_and_client_session`（同进程内存传输） | 协议形状：tools/list 词表、tools/call 真值链路（桩课表全量断言）、工具内部错误映射为 `isError=True` 协议错误而非连接崩（错误后 tools/list 存活）、无身份结构化 JSON 不炸协议 | 零 |
| L3 HTTP 集成 | httpx ASGITransport + asgi_lifespan LifespanManager（对齐 test_api_* 模式） | 挂载与 lifespan 接线（L2 测不到的正是这块）；initialize → tools/list 全流程（含 mcp-session-id 续传）；401 三态：无令牌 / 签名篡改（错钥重签）/ 合法放行。⚠️ 客户端必须带 `Accept: application/json, text/event-stream`，否则 406 | 零 |
| L4 真机冒烟 | ① `npx @modelcontextprotocol/inspector` 连本地 uvicorn 的 /mcp（Streamable HTTP 传输 + Bearer 手动填令牌）；② stdio 档写进 Claude Desktop `claude_desktop_config.json`：`{"mcpServers": {"wagent-campus": {"command": "<python 绝对路径>", "args": ["-m", "app.mcp"], "env": {"PYTHONPATH": "E:\\Agent\\backend", "MCP_ENABLED": "true", "MCP_DEMO_STUDENT_ID": "20230001"}}}}`；③ `scripts/mcp_smoke.ps1`（起 uvicorn → curl initialize 验 401/200 → 收尾，**已实测 PASS**） | 端到端 + 演示素材 | 真 |

#### L4 演示配置样例（可复现）

① MCP Inspector 连 HTTP 档（先 `MCP_ENABLED=true uvicorn app.main:app --port 8000` 起 dev 服务）：

```bash
npx @modelcontextprotocol/inspector
# UI 中：Transport = Streamable HTTP；URL = http://127.0.0.1:8000/mcp；
# Authentication → Bearer Token 填 JWT（python -c "from app.security import encode_token; print(encode_token('20230001'))"）
```

② stdio 档写进 Claude Desktop（`%APPDATA%\Claude\claude_desktop_config.json`，dev-only）：

```json
{
  "mcpServers": {
    "wagent-campus": {
      "command": "D:/Anaconda_envs/envs/WAgent/python.exe",
      "args": ["-m", "app.mcp"],
      "env": {
        "PYTHONPATH": "E:/Agent/backend",
        "MCP_ENABLED": "true",
        "MCP_DEMO_STUDENT_ID": "20230001"
      }
    }
  }
}
```

stdio 档身份来自 `MCP_DEMO_STUDENT_ID`（D1：无传输层，演示专用；未配置拒绝启动 exit 2）。两通道工具行为零分叉（同一 `build_mcp_server`），仅审计 channel 可区分（`mcp` / `mcp-stdio`）。

**L4 已实测（2026-11-10）**：`mcp_smoke.ps1` 输出 `401-check: 401` / `200-check: 200` / `mcp-session-id` 下发 → SMOKE PASS。①② 为手动演示步骤。**工具坑回写**：Windows PowerShell 5 按 ANSI 读取**无 BOM** 的 UTF-8 脚本，中文注释乱码可静默吞行（`$env:X="true"` 赋值失效）——.ps1 含中文必须存 **UTF-8 with BOM**。

---

## 附录 A：暴露词表及排除理由（v1.1）

| 工具 | write | boundary | 暴露 | 理由（每行一条，逐个人工评审点名） |
|---|---|---|---|---|
| `timetable_query` | False | AI | ✅ | 只读课表查询，TTL 缓存语义原样保留 |
| `grade_query` | False | AI | ✅ | 只读成绩查询（`grade_cache_ttl=0` 不缓存语义原样保留；身份注入保证只见本人成绩） |
| `library_loans` | False | AI | ✅ | 只读借阅查询 |
| `kb_search` | False | AI | ✅（门控） | 只读检索；叠加 `kb_enabled` 门控——False 时不在 registry，词表自然缺席 |
| `campus_agent_chat` | —（非 registry 工具） | MCP 专属 | ✅ | 整体包装 `Orchestrator.run()`（§3.4）：外部客户端获得完整护栏（F2/F4b/F5）的唯一通道，会话协议即写路径（续借确认门禁在编排层落地）。出参 `{session_id, reply, blocked}` |
| `library_renew` | True | AI | ❌ | 需会话级确认门禁：R2.4 二次确认态（已展示新到期日并获用户确认）在编排层/REST 面落地，MCP 直调绕过确认即不可接受——想续借走 `campus_agent_chat` 会话协议 |
| `reminder_subscribe` | True | AI | ❌ | 是写：订阅库幂等 upsert，持久状态变更；M2 修正既有标注瑕疵（spec 补 `write=True`），属性过滤正确排除 |
| `handoff_create` | False | AI+H | ❌ | 是编排层专用：转人工工单由编排层在会话上下文内生成（boundary=AI+H），`hit_rules` / `agent_response_log` 等必填上下文外部直调无从构造，属边界矩阵的人工兜底通道不外露 |
