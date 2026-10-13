# 学习指南 — SSE 端点（任务设计 W4 配套）

> 配套实现：[`backend/app/main.py`](../../backend/app/main.py) §对话入口 + [`backend/tests/test_api_chat.py`](../../backend/tests/test_api_chat.py)
> 配套设计：[`07-SSE端点设计.md`](./07-SSE端点设计.md)
> 阅读时长：约 25 分钟 ｜ 实操时长：45 分钟

---

## 一、为什么这是第一个"产品面"接口

到此为止，系统的所有入口都是**调试面**：6 个 `/agent/*` REST 接口
（"帮我查课表"要先 POST 一个 JSON），命令行 `demo_orchestrator.py`。
学生不会调接口——他们只会**说话**。

```
POST /agent/chat {"content": "我明天有什么课"}   ← 本任务交付
        │ Bearer JWT（student_id 注入，12 §7 L4）
        ▼
   get_orchestrator（依赖装配：db→Services→注册表→SessionStore→Orchestrator）
        ▼
   Orchestrator.run()（子任务 2，零改动）── ArkLLMClient（子任务 3 单例）
        ▼
   六类 Event ──_sse()──> SSE 帧 ──> StreamingResponse(text/event-stream)
```

子任务 1~3 冻结的协议在这里兑现：**编排层一行代码没改**，端点只是把
`run()` 的异步生成器接到 HTTP 流上。做完这个，接任意前端（网页/企微）
只差一个渲染层。

**自检**：如果你发现自己在 `orchestrator.py` 里 import fastapi，说明
层次漏了——编排层不该知道自己在被谁驱动。

## 二、本任务交付了什么

| 交付物 | 位置 | 一句话 |
|---|---|---|
| 对话端点 | `main.py` `/agent/chat` | POST 进、SSE 出 |
| 帧序列化 | `main.py` `_sse_frame` | Event → `event:`+`data:` 两行一帧（纯函数） |
| 断线清理 | `main.py` `_sse` | finally 里 aclose + suppress 兜底 |
| 两个依赖 | `main.py` `get_llm`/`get_orchestrator` | 测试注入点（dependency_overrides） |
| 生命周期 | `main.py` lifespan | startup 自检 warning；shutdown aclose LLM |
| CORS 门控 | `main.py` + `config.py` | `cors_allow_origins` 空 = 不挂中间件 |
| 11 条 API 测试 | `tests/test_api_chat.py` | 首个 HTTP 层测试基建 |

## 三、一次对话请求的完整旅程

对着 `main.py` 的 `chat()` 读：

```
JWT 解码（security.py，401 在这里发生）
  → body 校验（ChatRequest，422 在这里发生）
  → get_orchestrator：请求级装配（每请求新 db 会话/注册表/存储）
  → StreamingResponse 立即返回 200 + 响应头
       │  （此刻响应体还没生成一个字节）
       ▼
  _sse(orch.run(...)) 被 Starlette 逐帧拉取：
     Event ──_sse_frame──> "event: token\ndata: {...}\n\n"
       │ 每帧即写即发（打字机效果的来源）
       ▼
  done / error 帧 → 生成器自然结束 → 连接关闭
```

关键认知：**端点函数返回时对话还没开始**。`orch.run(...)` 只是创建了
生成器对象（一行代码都没执行），真正的红线检查、工具调用、LLM 流式都
发生在 Starlette 消费 `_sse` 的过程中。这就是"401/422 在流开始前、
其余一切在流里"的分界线。

## 四、五个关键设计决策与坑（必读）

### 4.1 流一旦开始，HTTP 恒 200

SSE 的本质是"先答应、再说话"——响应头发出去了就收不回来，中途出错
**不可能**把 200 改成 500。所以约定：401/422 靠 FastAPI 在流前返回；
流内一切失败收敛为 `error` 帧（`llm_unavailable` / `storage_error` /
`internal`）。**前端永远以 done/error 帧为终态**，不能以"连接关闭"
当成功。这不是妥协，是 SSE 协议的正统用法。

### 4.2 POST + SSE：EventSource 用不了

浏览器原生 `EventSource` 只支持 GET，而"发消息"语义 + 2000 字上限的
请求体必须是 POST。决策：前端用 `fetch` + `ReadableStream` 手工解析：
按空行（`\n\n`）切帧，每帧读 `event:`/`data:` 两行。最小解析代码在
07 设计 §4.2，二十行以内。**别在服务端为迁就 EventSource 把消息塞进
URL 查询参数**——长度受限还进访问日志。

### 4.3 断线清理：谁 close 谁

客户端断开 → Starlette 关闭 `_sse` 生成器 → GeneratorExit 在 yield 点
抛出 → finally 执行 `await agen.aclose()`。两个容易踩的坑：

- **编排层的 finally 禁止 await**（03 难点 2）——被关闭的生成器里再
  await 一个慢操作会挂死清理路径。所以编排层 finally 只做同步日志，
  收尾责任在端点；
- **清理本身可能炸**——所以 `suppress(Exception)` 兜底。清理失败的
  最坏结果是连接池晚点回收，不值得为它再抛一个异常。

正确性兜底是"宁缺半截"：中间消息每轮即时落库，断线最多丢最终
assistant 消息一条；用户带 `session_id` 重发，上下文还在。AC-9 专项
测试就是模拟"消费一帧后 aclose"，断言内层生成器被闭合且无异常外泄。

### 4.4 两个响应头不是装饰

- `Cache-Control: no-cache`：防中间层缓存（SSE 是会话级实时流）；
- `X-Accel-Buffering: no`：**nginx 默认缓冲响应体**，不加这个头，
  打字机会变成"卡 10 秒一次性蹦完"。反向代理是流式接口最常见的
  "本地好好的、上线就坏"元凶（07 风险 ①）。

### 4.5 CORS 配置门控：默认什么都不挂

`cors_allow_origins` 为空 = 不注册 CORSMiddleware，生产请求路径与
改动前完全一致；配了才挂，且只放行最小面（POST/OPTIONS + Authorization/
Content-Type，不带凭证）。**宁可默认关闭也不要 `"*"`**——`*` 配合
凭证头是经典安全事故组合，而本项目身份就是 Bearer 头。

## 五、测试怎么组织（首个 API 级测试基建）

```python
httpx.AsyncClient(transport=ASGITransport(app=app))
  + app.dependency_overrides[get_orchestrator] = 测试装配
       （内存 SQLite + make_services 桩适配器 + FakeLLM，全复用 conftest）
```

三个分层认知：

- **ASGITransport 不跑 lifespan**——恰好规避 `init_db()` 触碰 PG，
  测试零外部依赖（这是选它而不是真起端口的原因之一）；
- **分层只测自己新增的**：HTTP 层测装配、帧序列化、鉴权、CORS 门控；
  编排逻辑本身已被 test_orchestrator 的 23 例覆盖，不重复；
- **AC-10 的例外**：CORS 中间件在 import 期由配置决定挂不挂，测"配置后"
  分支需要 `importlib.reload(config/main)` 重建应用——这是全文件唯一
  用 monkeypatch 的地方（改的是环境变量，不是模块全局；对话管线的
  注入仍然零 monkeypatch，AC-8 的约定不破）。

## 六、动手实验（建议 45 分钟）

### 实验 1：跑 API 测试，对照 AC 表（10 分钟）

```bash
cd backend
python -m pytest tests/test_api_chat.py -v
```

挑 AC-9 读断言：`await g.aclose()` 之后 `closed` 为什么一定是 True？
GeneratorExit 在哪一层被谁捕获？

### 实验 2：真机联调，肉眼看打字机（10 分钟，消耗少量 token）

```bash
cd backend
PYTHONIOENCODING=utf-8 python -m uvicorn app.main:app --port 8000
# 另开终端：
python -c "from app.security import encode_token; print(encode_token('20230001'))"
```

⚠️ Windows 坑：curl `-d` 里的中文会被控制台按 GBK 转码，服务端直接
422 "error parsing the body"。正确姿势——写 UTF-8 文件再传：

```bash
python -c "open('b.json','w',encoding='utf-8').write('{\"content\": \"我明天有什么课\"}')"
curl -N -X POST http://127.0.0.1:8000/agent/chat \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  --data-binary @b.json
```

观察四件事：① 首帧 `thinking(session_ready)` 几乎立即到达；②
`tool_call` 帧里学号已脱敏（`2023**01`）；③ token 帧逐个到达（不是
一坨）；④ `done` 帧的 `session_id`——记下来，实验 3 要用。

### 实验 3：会话续用（10 分钟）

第二轮请求体带上 `{"session_id": "<上一步的>", "content": "那后天呢"}`。
观察：没有新的 session_ready 重建痕迹？错——`session_id` 有效时同样发
`thinking(session_ready)`，但**done 帧回传同一个 session_id**，且第二轮
模型知道你在说什么（上下文从库里加载了）。

### 实验 4：验证"未配置不崩"（5 分钟）

把 `.env` 的 `LLM_API_KEY` 注释掉，重启 uvicorn：启动日志出现
`llm_not_configured` warning，但服务照常起。发一句红线话术（"我的毕业
资格够吗"）——照样完整走完拦截流程（LLM 零参与路径）。恢复配置。

### 实验 5：亲手破坏帧格式（10 分钟）

把 `_sse_frame` 末尾的 `\n\n` 改成 `\n`，跑 `test_api_chat.py`——
看 AC-1 怎么红（帧切分靠空行，没有空行就没有帧边界）。改回来。

## 七、常见问题（FAQ）

**Q：为什么不用 `sse-starlette`？**
A：它的增量价值是自动心跳与重连提示，而本任务明确不做心跳（首事件
立即到、工具轮事件密集、压缩静默期 ≤15s 由超时兜底——07 §1.3 已登记
为已知限制）。六类帧的序列化一个纯函数足够，不值得为此加一个依赖。

**Q：done 帧的 data 里已有 session_id，信封里又合入一次，重复吗？**
A：重复但各有契约：信封（`_sse_frame` 统一合入）是**所有帧**的通用
字段，前端日志/排障统一打印；`done.data.session_id` 是 03 §2.3 的协议
要求——前端只在终态更新会话号。去掉任何一个都会破坏一层契约。

**Q：断线后用户刷新页面，对话丢了吗？**
A：上下文没丢（中间消息即时落库），丢的最多是最后一条 assistant
回复。前端保存 session_id，重发即可续上——"宁缺半截"设计的意义。

**Q：真机日志里 `llm_stream_done` 两行是什么？**
A：一次带工具的对话 = 两轮流式请求：第一轮拿到 tool_calls（0 个文本
块），工具执行后第二轮拿最终回答（实测 14 块、2.3s）。首轮 7.7s 里
大头是 TTFT——这就是 05 文档坚持默认关 thinking 的原因。

**Q：curl 看到的日期参数是 2025 年， bug 吗？**
A：是 05 §6.3 登记的已知遗留项：system prompt 没注入当前日期，模型
只能猜。属 prompt 内容工程，登记为子任务 4 之后的独立迭代——但它也
演示了脱敏的价值：`args_digest` 里看得见日期错、看不见完整学号。

**Q：`get_orchestrator` 和既有的 `_services` 什么关系？**
A：同一个装配哲学的两个入口：`_services` 是普通函数（REST 端点手动
调用），`get_orchestrator` 挂成 FastAPI 依赖（由框架解析、可被
dependency_overrides 整体替换）。后者是本任务对可测试性的唯一结构性
要求。

## 八、自测题

1. 流式响应已经发出 5 个 token 帧后数据库炸了，客户端会看到什么？
   HTTP 状态码是多少？前端怎么知道这轮失败了？
2. 为什么 `_sse` 的 finally 里要 `suppress(Exception)`？不加会怎样？
   编排层自己的 finally 为什么禁止 await？
3. `EventSource` 为什么用不了？前端解析帧的边界符是什么？
4. `X-Accel-Buffering: no` 防的是什么问题？在哪个部署环节才会暴露？
5. AC-10 测"配置后"分支为什么要 `importlib.reload`，而其他测试都不
   用？CORS 中间件是什么时候决定挂不挂的？
6. `cors_allow_origins` 默认空串而不是 `"*"`，防的是什么组合攻击？

（答案都在 07 文档 §2/§4 和本文 §四。）

## 九、下一步

- **提醒引擎（M5）**：调度、静默期、去重聚合、日频上限、降级链——
  阶段一剩下的最大一块拼图，webhook 端点可直接复用本次的 API 测试
  基建（ASGITransport + dependency_overrides）；
- **prompt 内容工程**：注入当前日期（修复"明天"幻觉——真机联调已
  复现：模型把明天猜成 2025-11-26）；话术模板库；
- **前端页面**：fetch + ReadableStream 解析器（07 §4.2 有最小实现），
  done 帧更新 session_id，error 帧给重试按钮。

建议动手顺序：先做实验 2/3 把打字机效果看进眼睛，再开提醒引擎设计
文档。

---

*学习指南 v1.0 ｜ 阶段一编排层子任务 4（收口）配套 ｜ 实现版本对应设计文档 v1.0 ｜ 联调实测：2026-10-13，真机全链路通（工具轮 7.7s + 文本轮 2.3s / 14 块），打字机效果肉眼确认*
