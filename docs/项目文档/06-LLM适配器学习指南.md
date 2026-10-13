# 学习指南 — LLM 适配器（任务设计 W3 配套）

> 配套实现：[`backend/app/llm/`](../../backend/app/llm/) + [`backend/tests/test_llm_adapter.py`](../../backend/tests/test_llm_adapter.py) + [`backend/demo_orchestrator.py`](../../backend/demo_orchestrator.py)（`--live`）
> 配套设计：[`05-LLM适配器设计.md`](./05-LLM适配器设计.md)
> 阅读时长：约 25 分钟 ｜ 实操时长：45 分钟

---

## 一、为什么"接大模型"只许写一个类

新手的直觉是"接大模型 = 在业务代码里到处调 API"。本项目反着来：
**整个系统只有一个地方知道厂商的存在**——`app/llm/adapter.py`。

```
Orchestrator ──只认──> LLMClient Protocol（40 行接口定义）
                            ↑ 结构化匹配
                     ArkLLMClient（唯一 import openai 的模块）
                            ↓
                     火山方舟 / 任何 OpenAI 兼容端点
```

这就是子任务 2 冻结协议的回报：编排层 842 行代码一行未改，适配器插入
即用。**连异常都不例外**——SDK 的 `APITimeoutError` 等被包装成
`LLMStreamError`，编排层 `except Exception` 不需要知道 `openai` 存在。
换厂商 = 改 `.env` 三行。

**自检**：如果哪天你想在 `orchestrator.py` 里 `import openai`，说明
适配器漏职责了。

## 二、本任务交付了什么

| 交付物 | 位置 | 一句话 |
|---|---|---|
| 适配器 | `app/llm/adapter.py` | OpenAI 流式分片 → `LLMChunk` 的翻译器 |
| 两个异常 | 同上 | `LLMNotConfiguredError`（没配）/ `LLMStreamError`（调挂了） |
| 三个配置 | `config.py` | `llm_model` / `llm_thinking_enabled` / `llm_stream_include_usage` |
| 16 条单测 | `tests/test_llm_adapter.py` | 全程 FakeOpenAI，不触网 |
| 联调开关 | `demo_orchestrator.py --live` | 真模型跑通完整链路（留了 TTFT 实测） |
| 依赖治理 | requirements ×2 | backend 成为唯一事实源，根目录伞形引用 |

## 三、一次 stream() 的完整旅程

对着 `adapter.py` 的 `stream()` 读：

```
kwargs 组装 ──> create(stream=True) ──> async with stream:
                                              │
     ┌────────────────────────────────────────┤
     │ for chunk in stream:                   │
     │   choices 为空？ ──跳过（usage 尾块）    │
     │   delta.content 有值？──yield 透传      │
     │   delta.tool_calls？──进 index 分桶     │
     └────────────────────────────────────────┘
                    │ 流结束
                    ▼
     聚合器非空 → yield LLMChunk(tool_calls=[...], finish="tool_calls")
     聚合器为空 → yield LLMChunk(finish_reason="stop")
```

关键认知：**tool_calls 是"攒着最后说"**。厂商协议里工具调用的 arguments
被切成任意多片到达，适配器必须拼完才能交给编排层——因为编排层要拿完整
JSON 去解析参数、落库、配对回填。

## 四、五个关键设计决策与坑（必读）

### 4.1 分片聚合的六个边界

写聚合器时最容易漏的就是边界。逐条过一遍 `test_llm_adapter.py` 的
AC-4~8，问自己"为什么"：

- **id 只在首片出现**——后续片 `tc.id` 是 None，直接赋值会把已攒的 id
  抹掉，所以是 `if tc.id: a.id = tc.id`；
- **id 全程缺失**——合成 `call_{index}`。不是为了好看：编排层的
  `tool_call_id` 配对协议要求 id 非空，缺 id 整轮对话就断裂了；
- **arguments 为空**——归一成 `"{}"`。透传空串会让编排层走一轮
  "arguments 解析失败"回填，白费一次 LLM 轮次；
- **空 choices 块**——开了 `include_usage` 后流尾会多一个只有 usage 的
  块，`choices` 是空列表，不处理就 `IndexError`。

### 4.2 read 超时是"字节间隔"，不是"整流时长"

`Timeout(15)` 的 read=15s 含义：**相邻两次读到字节之间**最多等 15s。
只要模型持续吐分片——哪怕吐的全是被丢弃的思考文本——流跑 10 分钟也
不超时。真正的风险是 **TTFT**：模型思考期间一个字节都不吐，read 计时
从连接建立后就开始了。所以"默认关 thinking"和"15s 超时"是一对组合拳。

### 4.3 为什么 `max_retries=0`

SDK 默认会静默重试 2 次。问题：**已经 yield 出去的 token 收不回来**——
用户屏幕上看到"明天有"，重试后又来一遍"明天有明天有"。所以重试语义
必须上收给编排层：首请求失败 → F6 → `error(llm_unavailable)`（用户
重试）；摘要失败 → F3 → 降级继续。适配器只负责"如实失败"。

### 4.4 懒校验：没配 Key 不阻止应用启动

`ArkLLMClient()` 构造时什么都不检查，首次 `stream()` 才抛
`LLMNotConfiguredError`。为什么：红线拦截（F2）是 **LLM 零参与**路径，
`/health` 和六个工具直调接口也不需要模型——配置缺失不该让这些能力
陪葬（P5：每个分支都要有用户可见出口）。`is_configured` 属性留给
子任务 4 在 startup 打 warning。

### 4.5 思考模型的双保险

豆包 seed 是思考模型：`reasoning_content` 会先于 `content` 流出。两层
防御：① `extra_body` 的 thinking **三态**——`None`（代码默认）不发参数，
任何 OpenAI 兼容端点安全；`False` 方舟显式关（省 token 钱、降 TTFT，
方舟用户在 `.env` 配 `LLM_THINKING_ENABLED=false`）；
② 传输层**有意不读取** `reasoning_content`（`getattr` 都不做）——万一
参数失效或换了不认这个参数的模型，思考文本也不会漏给用户。

## 五、测试怎么组织（三层桩 + 分层验证）

```python
FakeOpenAI                    # 伪 SDK 客户端（close 计数）
 └─ FakeCompletions           # 记录 create() 全部 kwargs，按脚本弹出分片
     └─ FakeAsyncStream       # 伪流：分片序列，Exception 项原样抛
```

- 分片用 `SimpleNamespace` 树伪造（`ns_chunk`/`ns_tc`）——不用真
  pydantic 模型，因为测的是**适配器的翻译逻辑**，不是 SDK 的校验；
- **测试分层**：单测管协议形状（快、确定），`--live` 管真兼容性
  （慢、花钱、手动）——FakeOpenAI 永远发现不了"方舟不认某个参数"，
  这类问题只有联调能暴露；
- E2E 用例直接复用 `test_orchestrator.make_orch(db, [], llm=适配器)`
  ——`llm=` 注入口是子任务 2 预留的，这就是"协议冻结"的含金量。

## 六、动手实验（建议 45 分钟）

### 实验 1：跑单测，对照 AC 表（10 分钟）

```bash
cd backend
python -m pytest tests/test_llm_adapter.py -v
```

挑 AC-5（多 index 交错）读断言：两个工具的参数碎片交替到达，为什么
最终还能各自拼对？（提示：`dict` 按 index 分桶。）

### 实验 2：离线 demo，看事件流形状（5 分钟）

```bash
python -m demo_orchestrator
```

### 实验 3：真机联调（10 分钟，消耗少量 token）

```bash
python -m demo_orchestrator --live
```

观察三件事：① 真实 call_id（厂商生成的，形如 `call_502a...`）；
② token 逐片到达；③ TTFT。然后**看日志行** `llm_stream_done`——
只有 model/chunks/finish/latency，没有内容和 Key。

### 实验 4：验证"未配置不崩"（10 分钟）

把 `.env` 的 `LLM_API_KEY` 临时注释掉，跑 `--live`：应用正常启动并
打印"LLM 未配置"退出。再跑 `python -m pytest`——67 个既有用例一个
都没受影响。恢复配置。

### 实验 5：亲手破坏一个边界（10 分钟）

在 `adapter.py` 把 `arguments or "{}"` 改成 `arguments`，跑
`test_llm_adapter.py`——看 AC-7 怎么红。改回来。

## 七、常见问题（FAQ）

**Q：为什么不继承 `LLMClient` Protocol？**
A：Python 的 Protocol 是结构化类型——`stream` 签名匹配即实现，继承反而
多余（`FakeLLM` 也不继承）。Type Checker 会自动认可。

**Q：`extra_body` 是什么？为什么走它传 thinking？**
A：OpenAI SDK 的逃生舱——不进 SDK 类型系统的参数原样放进请求体。方舟
的 `thinking` 是非 OpenAI 标准参数，只能这么传。

**Q：摘要（压缩）不是非流式吗，怎么复用同一个接口？**
A：编排层 `_compress_or_degrade` 忽略 `delta` 只拼接文本——流式接口
天然覆盖非流式场景，适配器不用特判。

**Q：`--live` 时模型把"明天"算成 2025 年，是 bug 吗？**
A：是**已知遗留项**（05 文档 §6.3）：system prompt 没告诉模型今天是
几号。属 prompt 内容工程，不归适配器管——但它演示了"边界清晰"的价值：
问题能被准确定位到哪一层。

**Q：为什么不把 `openai` 写进根目录 requirements 就完了？**
A：根目录那份是设计期快照，backend 那份才是真实运行清单——两份早就
漂移了（backend 连 `structlog` 都没有，环境靠历史安装残留才能跑）。
本次治理：backend 补齐为唯一事实源，根目录改伞形引用。

## 八、自测题

1. 厂商分片的 `tool_calls[].id` 只在首片出现，适配器怎么处理后续片的
   `id=None`？如果全程没有 id 会怎样？
2. 已 yield 了 3 个 delta 后流断了，用户会看到什么？编排层走哪条路径
   收尾？
3. `Timeout(15, connect=5)` 的 15 丈量的是什么间隔？思考模型开着时
   这个 15s 为什么可能击穿、又为什么可能永远不触发？
4. 为什么适配器不重试，而源系统适配器（edu）用 tenacity 重试 2 次？
   （提示：谁是"已交付给用户"的？）
5. `LLMStreamError` 和 `LLMStreamFailure` 是同一个东西吗？谁定义、
   谁抛出、谁捕获？
6. 把 `llm_thinking_enabled` 打开，多花的钱花在哪？用户感知差异是什么？

（答案都在 05 文档 §2/§3/§4 和本文 §四。）

## 九、下一步

- **子任务 4（SSE 端点）**：把六类 `Event` 映射为 SSE 帧；在 lifespan
  挂 `ArkLLMClient` 单例与 `aclose()`；startup 用 `is_configured` 打
  warning。事件协议（03 §2.3）与异常模型（05 §2.4）都已冻结，可直接
  开工。
- **prompt 内容工程**：注入当前日期（修复"明天"幻觉）；话术模板库。
- **阶段二**：usage 计量入库（开关已留）、语义红线通道。

建议动手顺序：先照 03 学习指南实验 1 复习事件流，再写 SSE 端点把
`--live` 的输出接到浏览器里。

---

*学习指南 v1.0 ｜ 阶段一编排层子任务 3 配套 ｜ 实现版本对应设计文档 v1.0 ｜ 联调实测：2026-10-09，TTFT 6.38s，工具调用链路全通*
