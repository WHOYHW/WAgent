# 实施设计 — 18：知识库检索 RAG（语料入库 · 混合检索 · Tool 式接入）

> 文档编号：WAgent-IMPL-18
> 版本：v1.9
> 适用阶段：阶段一 · 知识增强横切（为"学校规章制度/办事指南类"问答提供事实锚）
> 上游文档：[`./17-评测黄金集设计.md`](./17-评测黄金集设计.md)（结构模板与评测扩展，附录 B 依赖其 Case 模型）、[`./12-阶段一-技术架构与数据契约.md`](../12-阶段一-技术架构与数据契约.md)（分层架构与降级语义）、[`./16-工程化基线设计.md`](./16-工程化基线设计.md)（分层契约机器化，本任务新增 `app.kb` 层条目）、[`./05-LLM适配器设计.md`](./05-LLM适配器设计.md)（方舟凭证与配置门控惯例）

### 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-11-04 | 初版：五决策点裁决（Tool 式接入 / BLOB+numpy / FTS5-trigram+RRF / 方舟 embedding 复用凭证 / kb_enabled 默认关）、`app.kb` 四模块职责、`kb_chunk` 表与 FTS5 虚表、AC-18a~d、附录 A 语料种子（官网勘察 2026-11-04）、附录 B 评测扩展 |
| v1.1 | 2026-11-04 | K1 实施回写：契约与切块先行独立成步——`app/kb/schemas.py`（KbDocMeta/KbHit/KbSearchParams/Result + RetrievalMode + `chunk_text`/`make_doc_id` 纯函数），表模型与迁移并入 K2；`KbSearchResult.meta` 用嵌套 `ToolMeta` 模型（修正 v1.0 `meta: dict` 与"禁止裸 dict"15 §C 的冲突）；`meta.mode` 提升为 `KbSearchResult.mode` 独立字段（R1.3 三元组保持纯净） |
| v1.2 | 2026-11-05 | K1 范围重裁决（任务卡修订）：① 切块升级为三策略纯函数并独立成模块 `app/kb/chunking.py`——by_clause（正则切「第X条」，中文数字含"第一百零一条"，校规主策略）/ by_heading（markdown 标题，办事指南）/ by_qa（FAQ 问对），超长段回退 fixed 滑窗（500 字 + 50 重叠）；v1.1 的 `chunk_text` 单策略被替代移除。② `kb_chunk` 表提前至 K1 并改键：唯一约束 `(doc_id, section_idx)`（v1.0 的 `(source_url, chunk_index)` 废弃），BLOB 向量列 + provenance 全字段。③ Alembic 迁移随表同批交付：建表 + `CREATE VIRTUAL TABLE kb_chunk_fts USING fts5(..., tokenize='trigram')` 原生 SQL 写进 upgrade + 三触发器同步（external content 模式）；PG 方言跳过虚表（生产全文检索方案 K2 另行裁决，见 §迁移裁决）。④ config 七项（kb_enabled/kb_top_k/kb_chunk_max_chars/kb_score_threshold/kb_rrf_k/kb_corpus_version/kb_embedding_model）。⑤ 切块出参契约 `KbChunkDraft`（Pydantic，禁止裸 dict） |
| v1.3 | 2026-11-06 | K2 实施裁决（桩先行）：① `EmbeddingClient` Protocol 冻结、单方法 `embed_texts`——查询向量化复用同一接口（`embed_texts([query])[0]`），不设 `embed_query` 双入口（重用单一接口避免协议膨胀，LLM 适配器已验证的纪律）；**真方舟客户端随 K3 ingest 落地**（ingest 批量向量化与检索查询向量化共用同一实现），K2 以 Protocol + FakeEmbedder 冻结边界跑通内核全测试（零网络纪律）。② `FakeEmbedder` 规约：字符 sha256 散列桶（64 维、非空白字符逐字 +1、L2 归一化），**禁用内置 `hash()`**（PYTHONHASHSEED 随机化破坏确定性）；共享字符越多余弦越高 → 检索行为可逐条断言（AC-18b 前提）。③ `RetrievalMode` 新增 `VECTOR_ONLY`：查询 <3 字符（trigram 最小 token 长度）FTS 短语必空 → 跳过 FTS 纯向量兜底；检索模式由 v1.2 两态（fts_only/hybrid）扩为三态。④ FTS 转义规则（防 MATCH 语法注入）：`'"' + query.replace('"', '""') + '"'`——整体包成短语查询、内部引号双写，任何用户输入不能逃出短语字面量。⑤ 向量矩阵缓存键 `(MAX(corpus_version), MAX(id))`（仅统计 `embedding_model` 匹配且 `embedding IS NOT NULL` 行），每次检索廉价算键、键变自动重建（ingest 删重插后失效零通知），键同复用；加载时校验全行维度一致，不一致 → 本键标记不可用降级 FTS_ONLY（防 NaN 污染）。⑥ PG 全文检索（tsvector/pg_trgm）**推迟立项**：不为尚未到来的规模引入基建（09 D3 同哲学），K2 内核 SQLite 单方言，切 PG 前另裁决。⑦ FTS DDL 单源化 `app/kb/fts.py`：迁移 0005 与 K2 测试夹具（内存库不走 alembic）共用同一常量，消复制漂移。⑧ 内核出参 `KbRetrievalResult`（hits + mode，Pydantic 禁裸 dict），K4 工具层补 `meta` 组装 `KbSearchResult` |
| v1.4 | 2026-11-07 | K3 真机档实施裁决（任务卡 K3-Embedding；ingest 管线另步交付）：① `ArkEmbeddingClient` 落地 `app/kb/embedder.py`（定名对齐 `ArkLLMClient` 惯例）：AsyncOpenAI `/embeddings` 端点，base_url/key 复用 D4 既有 llm 凭证零新增凭证面，构造接受可选 `client` 注入（单测 FakeOpenAI，零网络）。② 批量化：按新增配置 `kb_embedding_batch_size`（默认 16）切批多次请求，结果按请求序跨批拼接、响应按 `data[].index` 重排并校验 index 连续。③ 懒校验镜像 LLM 适配器语义：构造零副作用，首次 `embed_texts` 才校验；未配置抛 `EmbeddingNotConfiguredError`（未配置是部署问题，启动不失败）；**异常类本地定义镜像语义而非复用**——分层契约 app.kb 位于 app.llm 之下，禁止反向 import。④ 调用失败统一包装 `EmbeddingCallError`（`__cause__` 保留原始异常），SDK `max_retries=0`（重试归调用方），日志只记元数据（model/批数/维度/延迟）不记文本与凭证。⑤ 维度入口校验：新增配置 `kb_embedding_dim`（None=不校验只报告实际维度），配置时全批维度一致且等于配置值，不匹配抛错防脏向量入库（检索内核矩阵加载降级是第二道兜底）。⑥ 任务卡 AC-18c --live 冒烟入口 `demo_embedder.py`：10 条真实文本往返成功、维度与配置一致（与 demo_orchestrator --live 同层：单测 FakeOpenAI 协议形状 / 联调真端点）；注意与 §七 既有 AC-18c（kb_search 门控）编号不同源，冒烟判据以本行 ⑥ 为准 |
| v1.5 | 2026-11-08 | K4 实施裁决（任务卡 K4-kb_search 核心切片）：① `tools/kb.py` `KnowledgeSearchTool`：write=False、boundary="AI"，入参 `query` + `top_k`（可选，student_id 按约定③从根源不适用）；出参键改用 **`items`**（v1.0 的 `hits` 废弃）——与既有五工具出参惯例一致，`orchestrator._result_digest` 认 `items` 键，SSE 摘要免费获得 `{count: n}`，`Expectation.nonempty_result` 判定随之零成本。② 工具层二次钳制（防会话膨胀的兜底，不信任上游）：`top_k ≤ kb_top_k`、单块 `content ≤ kb_chunk_max_chars`（语料入库后配置收紧时旧块超限的防御性截断）。③ 门控注册（D5 落地）：`build_registry` 按 `services.settings.kb_enabled` 注册，False 时工具根本不进注册表（LLM 工具列表不出现）；`default_registry` 以 `Settings(kb_enabled=True)` 显式构建——它是「全量词表」（Schema 导出 + 黄金集受控词表），与运行时门控注册表是两个用途，词表自动跟随工具改名防静默腐烂。④ 依赖注入：`Services` 新增 `kb_retriever` 字段；未注入且 `kb_enabled=True` 时 `__post_init__` 懒装配 `HybridRetriever(settings, ArkEmbeddingClient)`（构造零副作用；凭证未配置 → 首次检索 embed 抛错 → 内核降级 FTS 单路，启动不失败）；main.py 进程级共享单例（矩阵缓存跨请求存活，镜像 edu/lib 共享适配器惯例）。⑤ 异常路径裁决：检索异常**不走降级出参**（mode 是检索执行事实，异常时不存在可如实报告的模式，伪造 mode 即撒谎）——抛 `ToolError(render_degrade_text(UNAVAILABLE, kb_search))` 走既有工具失败通道（ok=False + 模板文案回填 LLM），degrade.yaml 增 `kb_search` 条目。⑥ 新鲜度锚：工具层查 `MAX(kb_chunk.synced_at)`（空库回退当前时间），超 `kb_stale_hours` → `is_stale=True + stale_reason="corpus_aged"`。⑦ 提示词 v2 三条新规则（政策/流程先 kb_search + 引用《文档名》+「以学校最新正式文件为准」；空结果如实明说不编造；kb 证据不改变 R1 终局性红线），PROMPT_VERSION → 2026-11-08.1。⑧ 黄金集扩展四条（klass=`kb` 路由命中 / 空库不编造 / 终局性改述对抗 / 复合双工具），kb 类 fake_only（依赖预灌桩语料，语料正式灌库前 live 档无 kb 工具）；分层契约 app.kb 位置修正至 app.tools 之下（v1.0 误置其上——K4 工具层必须 import 内核），新增 forbidden 契约「kb 内核不感知编排/LLM 层」（仅查直接导入；独立契约不可用——orchestrator→tools→kb 是合法传递链）→ lint-imports 3 kept |
| v1.6 | 2026-11-09 | K5 实施裁决（任务卡 K5-入库管线+首批语料）：① CLI `python -m app.kb.ingest`：`--seed <yaml>`（种子清单，必填）/ `--dry-run`（只抓取+提取+切块+报告计数，**不落库不调 embedding**）/ `--fixture`（种子 url 字段指向本地 fixture HTML 文件路径 + `FakeEmbedder`，**零网络**，CI 与单测共用）/ `--version <锚>`（语料版本锚，缺省 `settings.kb_corpus_version`）/ `--db <url>`（缺省 `settings.db_url`）。门控：`kb_enabled=False` 拒绝运行（D5 同源）。② 种子 YAML 契约：`SeedEntry`（Pydantic 禁裸 dict）= `url + column + strategy`，strategy ∈ clause/heading/qa 显式指定（§三"策略选择是 ingest 职责"落地，无隐藏默认值）；种子文件放 `backend/kb_seed/`。③ 抓取（对齐 adapters/base 模式）：httpx 单客户端、明示 UA（`WAgent-KB-Ingest/1.0`）、限速 `kb_request_interval_seconds`（1 req/s 礼貌下限）、超时 20s（官网站点与校内源系统 2s 口径不同源，独立常量）、重试 `read_retry_times`=2 次指数退避（0.2→0.6s）。④ 博达正文提取：首选 `v_news_content` 选择器（嵌套 div 取最长 content 区）；无选择器回退 `edu-main-content` 区纯文本；script/style 剥离 + HTML 实体归一 + 空白折叠；标题取 `<h1>` 回退 `<title>` 剥站点后缀；**<200 字符或纯链接壳页丢弃**（§三清洗规则落地，常量 `MIN_CONTENT_CHARS=200`），丢弃项打印跳过清单（诚实报告）。⑤ 幂等与增量（AC-18e 铁证）：判重键 = `doc_id`（source_url 确定性）+ `content_hash`（清洗后全文 sha256）——同版本重跑且全文未变 → 全部 skip **零写入零 embedding 调用**；全文已变 → 按 doc_id 整页删除重插；新增 → 插入。落库**单事务**：一次 session 贯穿全程、最终单次 commit，任一步失败整体回滚；FTS5 同步依赖迁移 0005 触发器，ingest 启动时幂等 ensure 虚表+触发器（IF NOT EXISTS，内存库/新库防御性自建，对齐 K4 空库用例教训）。⑥ 首批语料勘察收窄（2026-11-09 实勘，附录 A 回写）：jwc 全部规章制度文章正文 FlexPaper-SWF/PDF/扫描图内嵌**零可提取文本**（58 篇实勘仅 2 篇有正文），按「有正文文本」硬标准收窄首批种子 = 办事指南子文章 13 篇 + 学生考场纪律 + 教育部令第41号 ≈ 15 篇 ~2 万字符 → 预估 40~70 chunks；预估规模（~50 篇 → 500~1000 chunks）不达成是站点形态决定的，不阻塞管线交付。⑦ AC-18e 新增验收标准（§七）：同版本重跑 inserted=updated=0 且行数与 fetched_at 零变化；provenance 字段（source_url/fetched_at/corpus_version）100% 非空 |
| v1.7 | 2026-11-09 | K6 收尾裁决（任务卡 K6-回流线+文档+live 留档）：① `app/kb/faq_export.py` handoff→FAQ 候选**只读导出** CLI（`--db/--out/--status/--since/--min-count`）：`handoff_ticket.user_question` 聚类 → `faq_candidates/` CSV+Markdown → **人工审核兜底**（CSV 审核四列留白：采纳/问题/答案/版本）→ 手工补 `kb_seed/*.yaml`（strategy: qa）→ ingest 入库。三条铁律：全程 SELECT 零写库零 commit（只导出不自动入库，文档 11「人兜底」）；聚类为确定性纯函数（NFKC 归一化 + 剔数字标点 → 精确合并 + 2-gram Jaccard ≥ 0.6 并查集，归一化 <4 字短句只精确合并防过聚），LLM 零参与；导出物含学生原话 → `faq_candidates/` 入 .gitignore，student_id 全程不出脚本。不设 D5 门控：内部运营报表，不提供面向学生的运行时能力（与 ingest 的「能力上线是运营决策」语义不同源）。回流线链路落地见 [`../11-阶段一-AI可执行与人工兜底边界矩阵.md`](../11-阶段一-AI可执行与人工兜底边界矩阵.md) §4.4。② `run_eval --live` 留档：提示词 v1/v2 分数对比（见 §七 验收状态 2026-11-09），live 基线随 v2 有意变更刷新。③ 文档收口：15 §7.2 新增 ingest/faq_export 命令速查（7.2~7.5 顺延）、README 进度勾选 RAG K1~K6、docs/README 索引补 18 |
| v1.8 | 2026-11-09 | RAG 优化三项（评审反馈修复）：① `_fetch_rows` KeyError 防御（retriever.py）：矩阵缓存的 id 已不在库中（手工清库 / 缓存键 `(MAX(corpus_version), MAX(id))` 在「删非最大 id 且无新增」时不变）时原实现 `rows[cid]` 直接抛 KeyError → 整个检索降级成不可访问；改为逐条 `rows.get(cid)` 为空即跳过该块，其余命中正常返回。② `kb_score_threshold` 0.01 → **0.015**（语义失真修正）：候选池 20 时单路 rank-20 = 1/80 = 0.0125 > 0.01，池内任何结果都过阈值，「滤深尾噪音」名不实——k=60 下新值使单路 rank≤6 过线（1/66≈0.0152 > 0.015 > 1/67≈0.0149）、双路共现远高于此，阈值有实际过滤工作；弱匹配时少返回而非凑满 top_k。不选池调大（与 §四「两路各取 Top-20」设计冲突）或删配置（失去弱匹配诚实少返回的门）。③ ingest `--dry-run` 复用 `make_fetcher("live", ...)` 同源抓取器：原实现直连 `fetch_html` 绕过域白名单与 1 req/s 限速——种子写错域时 dry-run 不报错、正式跑才炸；现 dry-run 与正式档同源，域外 URL 即刻报错 |
| v1.9 | 2026-11-09 | RAG 优化第二批四项（评审反馈 4~8）：④ `KbHit` 新增 **`anchor`** 字段（schemas 默认 None / retriever 透传 `row.anchor` / 工具层 `model_copy` 自动转发）——切块产锚、表有 anchor 列、`_fetch_rows` 拿到全行，唯独结果契约没带；补字段后 LLM 引用粒度从文档级升到条款级（性价比最高的一条）。提示词第 9 条同步点明 anchor 用法（"引用优先落到锚这一级"），PROMPT_VERSION → **2026-11-09.1**；eval 桩语料 anchor 填实「第一条」（fake 档覆盖非空路径）。⑤ `fetch_html` **4xx（除 429）不重试**：404/403 是确定性失败，重试无意义——只对传输错误/5xx/429 走指数退避，fail fast 暴露种子错误（MockTransport 单测坐实：404 单次请求 / 503 与 429 重试 1+read_retry_times 次）。⑥ UA 联系方式 `ops@wagent.local` 是占位符：真机跑前必须换真实联系方式（对校方站点礼貌爬虫硬惯例）——待提供后替换 `CRAWLER_USER_AGENT`。⑦ 语料密度是当前真瓶颈（40~70 chunks 向量路区分度有限）：三条补法按优先级回写附录 A「语料扩充路线」——学生手册 PDF 线下渠道（①）/ 扫描图规章 OCR 人工录入作 K7 候选（②）/ faq_export 工单长 FAQ 长线（③）。⑧ live 档 kb 路径零覆盖（kb 类 4 条全 fake_only）：语料正式灌库后解除 kb-route-policy-hit 的 fake_only（或补一条 live 档 kb 用例），让提示词 kb 行为在真机档有归因记录。⑨ 口径对齐（「四态」笔误修正）：`RetrievalMode` 枚举成员即事实（FTS_ONLY/VECTOR_ONLY/HYBRID）实为**三态**——v1.3 修订记录（实为 v1.2 两态扩三态）、§四 模式表标题、WBS K2 行与 retriever docstring 的「四态」统一修正。⑩ `kb_corpus_version` 语义澄清（§六 配置表）：仅是标记锚（provenance 溯源 + 矩阵缓存键组成之一），检索不按它过滤——跨版本 chunk 增量共存（判重靠 doc_id+content_hash），无「切版本」语义 |

---

## 一、范围与动机

**现状**：六工具的回答能力全部锚定在**结构化数据源**（教务/图书馆适配器的实时接口）上。但"学校有哪些社团""转专业要什么条件""图书馆开放到几点""学生证丢了怎么补办"这类**规章制度与办事指南**问题没有数据源——模型只能靠参数化知识瞎编（对一所具体学校必然编错），红线 R1"终局性结论"和事实正确性约束都要求这类问题必须**查得到、答得准、查不到就承认**。

**本任务交付一套最小闭环 RAG**（新包 `backend/app/kb/` + 一个新工具）：

| 文件 | 职责 | 章节 |
|---|---|---|
| `kb/ingest.py` | 入库管线：种子 URL 抓取 → 正文清洗 → 分块 → 向量化 → 落库 | §三 |
| `kb/embedder.py` | 方舟 embedding 客户端（复用既有凭证，配置门控） | §三 |
| `kb/retriever.py` | 混合检索：FTS5-trigram 关键词路 + 向量语义路 → RRF 融合 | §四 |
| `tools/kb.py` | `kb_search` 工具（编排层唯一接入点，门控 + 降级语义） | §五 |

**不在范围**：语义质量裁判（模型裁判评分另立项）；多轮对话式追问改写；PDF/图片语料（阶段一只收 HTML 正文）；知识库管理面（增删改查 UI）。

---

## 二、决策点裁决

| # | 裁决 | 备选与理由 |
|---|---|---|
| D1 | **Tool 式接入（`kb_search` 注册进 registry），不做中间件自动注入** | 中间件式（每轮对话先检索、把片段塞 system prompt）违背现有 Agent 模式：编排层六工具全部经 registry 路由，LLM 按需调用；自动注入对闲聊/课表等无关轮次烧 token，且无审计锚点。Tool 式白拿既有三件套——`audit_log` 审计轨迹、黄金集工具计数断言、降级链管辖（检索失败走 C4 语义而非污染回答） |
| D2 | **SQLite BLOB 存向量 + numpy 暴力余弦，不上向量库** | 语料量级是校园 FAQ（数百~数千 chunk），numpy 点积毫秒级完成，向量库（Milvus/QDuvant/faiss）是部署面负资产。与 09 D3"否决分布式锁"同一哲学：**不为尚未到来的规模引入基建**。单文件随 SQLite 一起备份/迁移，零新服务依赖（仅新增 numpy 一项） |
| D3 | **FTS5-trigram 关键词路 + 向量语义路，RRF 融合** | FTS5 默认 unicode61 对中文基本不可用（无空格分词）；trigram tokenizer 对 CJK 友好且 SQLite ≥3.34 内建，无 jieba 分词依赖。但 trigram 召不回同义改写（"奖助学金"查不到"资助"），向量路补语义。融合用 **RRF（Reciprocal Rank Fusion，k=60）**而非加权分数：只融合排名不比较分数，两路得分分布完全不可比时依然稳健，纯 Python 十行实现 |
| D4 | **方舟 embedding API（复用既有凭证），不上本地模型** | `llm/adapter.py` 已有方舟 base_url + API key 凭证管理，embedding 是同一控制台的另一个模型 ID（doubao-embedding），**零新增凭证面**。本地模型（bge + sentence-transformers）要引入 torch 级依赖（数百 MB），违背依赖最小化。代价是检索语义路依赖网络——由 D5 门控 + 降级兜底：embedding 不可用时语义路降级，FTS5 单路仍可服务 |
| D5 | **`kb_enabled` 默认 `false` 关门控** | 全项目配置门控惯例（`reminder_enabled`、`push_wecom_webhook_url=None` 走桩）。默认关的三重理由：① 语料未灌库时开检索只返回空噪音；② embedding 是真网络调用，违反"测试与 CI 不触网"纪律（16 §五），关门控即天然断网隔离；③ 语料管线跑通、附录 B 评测通过后由部署配置显式打开——**能力上线是运营决策，不是代码合并的副作用** |

---

## 三、数据模型与入库管线（K1：chunking + 表 + 迁移；K3：ingest）

### 表结构（`app/orm/models.py` 新增 `KbChunk`，v1.2 键裁决）

```python
class KbChunk(Base):
    """知识库分块：一条网页正文切成 N 块，每块一行（BLOB 存 float32 向量，D2）。

    键裁决（v1.2）：唯一约束 ``(doc_id, section_idx)`` —— doc_id 由
    make_doc_id(source_url) 确定性生成，整页删除粒度以 doc_id 定位；
    section_idx 是切块器输出的页内序号。v1.0 的 (source_url, chunk_index) 废弃。
    provenance 字段按块冗余（denormalized）：检索命中行自带全部溯源信息，
    无需 join 一张 doc 级表——FAQ 语料量级下冗余换简洁是正收益。
    """
    __tablename__ = "kb_chunk"

    id: Mapped[int] = ...                                        # 自增主键
    doc_id: Mapped[str] = mapped_column(String(16), nullable=False)      # make_doc_id
    section_idx: Mapped[int] = mapped_column(Integer, nullable=False)    # 页内序号
    anchor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #              ↑ 切块锚（「第三条」/ markdown 标题 / FAQ 问题行），无锚（前言/滑窗尾片）为 None
    title: Mapped[str] = mapped_column(String(256), nullable=False)      # 页面标题（FTS 列）
    content: Mapped[str] = mapped_column(Text, nullable=False)           # 清洗后块原文（FTS 列）
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #              ↑ None = 尚未向量化（embedding 服务不可用时只进 FTS 路，
    #                重跑 ingest 回填——宁可单路也不阻塞）
    embedding_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256，增量刷新去重
    # ---- provenance 全字段（按块冗余）----
    source_url: Mapped[str] = mapped_column(String(512), nullable=False)
    column: Mapped[str] = mapped_column(String(128), nullable=False)     # 栏目，如 教务处/规章制度
    corpus_version: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(TimestampTZ, nullable=False)
    # uq_kb_chunk_doc_section = unique(doc_id, section_idx)
```

### 切块三策略（`app/kb/chunking.py`，v1.2 新裁决，纯函数零 IO）

页面形态决定策略，三策略均为 `(text, *, max_chars=500, overlap=50) -> list[KbChunkDraft]`
的纯函数（零 IO、零调度，全量可单测）；出参 `KbChunkDraft` 为 Pydantic 模型
（`section_idx` / `anchor` / `text`，禁止裸 dict，15 §C）：

| 策略 | 适用语料 | 切分规则 | 超长回退 |
|---|---|---|---|
| `by_clause` | 校规章程（**主策略**） | 正则 `第[零一二三四五六七八九十百千]+条` 定位条款边界（中文数字任意长，含"第一百零一条"）；首个条款前的前言独立成块（anchor=None） | 单条超限 → fixed 滑窗 |
| `by_heading` | 办事指南（markdown 化正文） | `^#{1,6} ` 标题行定节；anchor=标题文本 | 单节超限 → fixed 滑窗 |
| `by_qa` | FAQ 问答页 | `问：/Q:` 行开启新对，anchor=问题行 | 单对超限 → fixed 滑窗 |

- **fixed 滑窗回退**：步长 `max_chars - overlap`（450），窗口长 `max_chars`（500），推进保证无死循环；无任何锚可识别时整篇走滑窗。
- **不变量**：① 任意块 `len(text) ≤ max_chars`；② **不丢正文**——原文每个非空行必出现在某块中（重叠只复制不删除，空白行丢弃不算丢正文）；③ 块序 = 原文序，`section_idx` 连续自 0 递增。
- 策略选择是 K3 ingest 的职责（按页面栏目路由），chunking 层只提供纯函数不自动探测——探测逻辑混进切块函数会破坏"单测确定性"。

### 迁移（Alembic 0005，v1.2 随表同批交付）

- 建表：`kb_chunk`（与 ORM 一致，`(doc_id, section_idx)` 唯一约束）。
- FTS5 虚表：`CREATE VIRTUAL TABLE kb_chunk_fts USING fts5(title, content, content='kb_chunk', content_rowid='id', tokenize='trigram')` —— **原生 SQL 写进 upgrade**（Alembic 不理解虚表）；external content 模式 + AFTER INSERT/DELETE/UPDATE 三触发器同步，检索免双写。
- **方言裁决**：FTS5/trigram 为 SQLite 专有，upgrade 内以 `bind.dialect.name == "sqlite"` 守卫——SQLite（开发/测试/单机部署）全量建；PostgreSQL（生产目标库）跳过虚表只建表，PG 全文检索方案（tsvector / pg_trgm）在 K2 检索层按方言分派另行裁决。理由：FTS5 写进 unguarded 迁移会让生产 PG 迁移直接崩溃，而 PG 方案属于检索层职责，不该由建表迁移越权决定。
- trigram 可用性：Python 3.13 自带 SQLite ≥3.45（trigram tokenizer 自 3.34 内建），测试以迁移 roundtrip 实测兜底。

### 入库管线（K5 v1.6 裁决落地，CLI 驱动：`python -m app.kb.ingest`）

**CLI 形态（v1.6 ①②）**：`python -m app.kb.ingest --seed kb_seed/jwc_seed.yaml [--dry-run] [--fixture] [--version 2026-11-09.1] [--db <url>]`。
种子清单显式列举（v1.6 ⑥：站点勘察证明制度正文 SWF 化，爬虫发现只会挖到壳页与不可提取正文，**v1.0 的深度爬取降级为显式种子清单**——语料范围是人工运营决策）；`SeedEntry`（Pydantic）= `url + column + strategy`，strategy ∈ clause/heading/qa 显式指定。域白名单校验 `hnist.cn` / `*.hnist.edu.cn` 保留（fixture 档豁免，url 是本地文件路径）。

四步单向流（每步可独立重跑）：

1. **抓取（v1.6 ③，对齐 adapters/base 模式）**：httpx 单客户端、明示 UA `WAgent-KB-Ingest/1.0`、限速 `kb_request_interval_seconds`、超时 20s（独立常量，官网站点与校内源系统 2s 口径不同源）、重试 2 次指数退避（0.2→0.6s，对齐 `read_retry_times`/`retry_backoff_*`）。fixture 档以本地文件读取替代网络。
2. **提取与清洗（v1.6 ④，博达 CMS）**：正文首选 `v_news_content` 选择器（嵌套 div 取最长 content 区），无选择器回退 `edu-main-content` 区纯文本；script/style 剥离 + 实体归一 + 空白折叠；标题取 `<h1>` 回退 `<title>` 剥站点后缀；`<200 字符`（`MIN_CONTENT_CHARS`）或纯链接壳页丢弃，丢弃项打印跳过清单（诚实报告，不静默吞）。
3. **切块（K1 纯函数）**：按种子 `strategy` 路由三策略，装配 `KbChunkDraft` → `KbChunk` 行（doc_id / section_idx / anchor / content_hash）。
4. **向量化与落库（v1.6 ⑤）**：`embed_texts` 批量调方舟（fixture 档 FakeEmbedder）；**单事务**——一次 session 贯穿全程、最终单次 commit，任一步失败整体回滚；FTS5 同步依赖迁移 0005 触发器，ingest 启动幂等 ensure 虚表+触发器（IF NOT EXISTS）。embedding 调用失败 = 本次入库失败（整体回滚），**不做 embedding=None 半态入库**——v1.0 的半态容忍在 K2 矩阵加载「embedding_model 匹配 + 非 NULL」过滤下只会产生永远不参与的死数据，单事务回滚让重跑自然补齐。

**幂等与增量刷新（AC-18e 铁证，v1.6 ⑤）**：判重键 = `doc_id`（source_url 确定性）+ `content_hash`（清洗后全文 sha256）。同 `corpus_version` 重跑：全文未变 → 全部 skip（零写入、零 embedding 调用）；全文已变 → 按 doc_id 整页删除重插；新增 → 插入。CLI 结束打印 `inserted/updated/skipped` 计数与 provenance 非空校验结果；**同版本重跑行数零变化 + fetched_at 零变化 = 幂等铁证**。

---

## 四、检索管线（v1.3，K2 内核：`kb/embedder.py` + `kb/retriever.py`）

### embedding 客户端协议与确定性桩（`kb/embedder.py`，K2 桩先行）

```python
class EmbeddingClient(Protocol):
    """embedding 客户端冻结协议（duck typing，对齐 LLMClient 惯例，无 ABC 继承）。"""
    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

class FakeEmbedder:
    """确定性假 embedder（测试/黄金集专用，零网络零依赖）。

    规约（v1.3）：字符 sha256 散列桶——文本每个非空白字符 c 散列到固定
    64 维桶计数 +1，L2 归一化。禁用内置 hash()（PYTHONHASHSEED 随机化
    会破坏确定性）。共享字符越多余弦越高，检索行为可逐条断言（AC-18b）。
    """
```

- **协议单方法**：查询向量化复用 `embed_texts([query])[0]`，不设 `embed_query` 双入口——重用单一接口避免协议膨胀，是 LLM 适配器"非流式复用流接口"同一条纪律。
- **真方舟客户端（v1.4，K3 真机档）**：`ArkEmbeddingClient` 落地同一文件，ingest 批量向量化与检索查询向量化共用同一实现——AsyncOpenAI `/embeddings` 端点、base_url/key 复用 D4 既有 llm 凭证（零新增凭证面）、按 `kb_embedding_batch_size` 批量化（结果按请求序跨批拼接，响应按 `data[].index` 重排 + 连续性校验）、懒校验镜像 LLM 适配器语义（构造零副作用，未配置首次调用抛 `EmbeddingNotConfiguredError`，启动不失败）、失败包装 `EmbeddingCallError`（`__cause__` 保留）、SDK `max_retries=0`（重试归调用方）、日志只记元数据、幂等 `aclose()`。**异常类本地定义镜像 `LLMNotConfiguredError` 语义而非复用**——分层契约 app.kb 位于 app.llm 之下禁止反向 import。维度入口校验：`kb_embedding_dim` 配置时全批维度必须一致且等于配置值（防脏向量入库，检索内核矩阵加载降级是第二道兜底）。

### 混合检索内核（`kb/retriever.py`）

```python
class Retriever(Protocol):
    """检索内核冻结协议（K4 工具层与黄金集桩依赖，duck typing）。"""
    async def search(self, session: AsyncSession, query: str) -> KbRetrievalResult: ...

class HybridRetriever:
    def __init__(self, settings: Settings, embedder: EmbeddingClient | None = None): ...
```

出参 `KbRetrievalResult`（schemas，Pydantic 禁裸 dict）：`hits: list[KbHit]`（RRF 序、已过阈值门与 Top-K）+ `mode: RetrievalMode`；K4 工具层据此补 `meta` 组装 `KbSearchResult`。DB 异常向上抛（C4 语义包装是 K4 职责，本层不吞错、不重试）。

**双路语义与三态模式**（v1.3，`RetrievalMode` 新增 `VECTOR_ONLY`；枚举成员即事实：`FTS_ONLY / VECTOR_ONLY / HYBRID`）：

| 条件 | mode | 行为 |
|---|---|---|
| FTS 可用（查询 ≥3 字符）+ 向量路可用 | `hybrid` | 双路候选 RRF 融合 |
| 查询 <3 字符（trigram 最小 token 长度，FTS 短语必空）+ 向量路可用 | `vector_only` | 跳过 FTS，纯向量兜底 |
| 向量路不可用（无 embedder / embed 抛错 / 维度不一致） | `fts_only` | 仅关键词路；<3 字查询照常执行 FTS（短语必空如实返回空，不谎报模式） |

**FTS 路（SQLite 方言，D3）**：

- **转义规则（防 MATCH 语法注入）**：`'"' + query.replace('"', '""') + '"'`——整体包成短语查询、内部引号双写，任何用户输入不能逃出短语字面量（`NEAR(`、`AND`、列过滤语法全部失效）；
- 候选池：`SELECT c.id FROM kb_chunk_fts JOIN kb_chunk c ON c.id = rowid WHERE kb_chunk_fts MATCH :q ORDER BY bm25(kb_chunk_fts), c.id LIMIT 20`（`c.id` 平局决胜保证确定性），返回序即排名（1 基）；
- **PG 推迟立项**（v1.3 ⑥）：不为尚未到来的规模引入基建，K2 内核 SQLite 单方言；生产切 PG 时全文检索方案（tsvector / pg_trgm）另立项裁决。

**向量路（numpy 全量余弦，D2）**：

- **矩阵懒加载缓存**：键 `(MAX(corpus_version), MAX(id))`，仅统计 `embedding_model = kb_embedding_model` 且 `embedding IS NOT NULL` 的行——每次检索先廉价算键，键变自动重建（ingest 删重插后失效零通知，多实例各进程独立失效），键同复用；键下维度不一致 → 记日志 + 本键标记不可用（降级 FTS_ONLY，防 NaN 污染全量计算）；
- 余弦：行向量加载时 L2 归一化（零向量守卫），查询向量归一化后 `M @ q`，`argsort(kind="stable")` 取 Top-20（零向量查询向量保持零向量，不产生 NaN）；
- 候选 chunk 行数据按 Top-20 id 回查 `kb_chunk`——矩阵缓存只存 `ids + 矩阵`，内存轻量。

**RRF 融合与阈值门**：

- `score(d) = Σ 1/(k + rank_i(d))`，`k = kb_rrf_k`（默认 60），两路候选并集；同分平局按 chunk id 升序（确定性）；单路 rank-1 = 1/(k+1) ≈ 0.0164，双路均 rank-1 = 2/(k+1) ≈ 0.0328；
- 阈值门：`score >= kb_score_threshold`（v1.8 修正为 0.015：k=60 下单路 rank≤6 过线，双路共现远高于此；池深 20 时旧值 0.01 对池内任何结果都不构成过滤）后取 `kb_top_k`（§六语义）；弱匹配时少返回而非凑满 Top-K。
- **新鲜度锚**：`KbHit` 不带时间字段给用户——工具层在结果 `meta` 附 `synced_at` 最新值，进入 C4 的 stale 展示链（知识可能过期的诚实标注，复用 `stale.py` 判定惯例，阈值独立配置）。

---

## 五、Tool 式接入（`tools/kb.py`，D1 落地，v1.5 实现裁决）

```python
SPEC = ToolSpec(
    name="kb_search",
    description="检索学校规章制度与办事指南公开知识库（转专业、证件补办、开放时间等）；"
                "未命中就如实说明，不要编造",
    params_model=KbSearchParams,    # query + top_k（可选）
    result_model=KbSearchResult,    # items + mode + meta(ToolMeta)
    write=False, boundary="AI",     # 只读检索，无写门禁
)

class KbSearchParams(BaseModel):
    query: str = Field(min_length=1, max_length=256, description="检索词，用学生的自然说法")
    top_k: int | None = Field(default=None, ge=1, description="返回条数上限，缺省由系统决定")

class KbSearchResult(BaseModel):
    items: list[KbHit]              # v1.5 改名：出参键沿用既有工具 items 惯例，
                                    # _result_digest 认 items → SSE 摘要免费获得 {count}
    mode: RetrievalMode
    meta: ToolMeta                  # source=INTERNAL / synced_at=MAX(语料 synced_at) /
                                    # degrade_status=fresh / is_stale 按 kb_stale_hours 判定
```

- **注册与门控（D5）**：`build_registry` 按 `services.settings.kb_enabled` 注册——False 时 `kb_search` **不注册**（而非注册后报错），LLM 工具列表里根本不出现，杜绝"调用后被告知不可用"的轮次浪费；`kb_enabled=True` 但 embedder 未配置 → 内核降级 FTS 单路。`default_registry` 以 `Settings(kb_enabled=True)` 构建，定位是**全量词表**（Schema 导出 + 黄金集 `validate_cases` 受控词表），与运行时门控注册表是两个用途。
- **依赖注入与装配**：`Services.kb_retriever: Retriever | None`——显式注入（测试/黄金集 FakeEmbedder 桩）优先；未注入且门控开启时 `__post_init__` 懒装配 `HybridRetriever(settings, ArkEmbeddingClient(settings))`（构造零副作用，凭证未配置首次检索才报错降级）；main.py 进程级共享单例，检索矩阵缓存跨请求存活（镜像 edu/lib 共享适配器惯例），shutdown 随 aclose 释放。
- **工具层二次钳制（不信任上游，防会话膨胀）**：`top_k = min(params.top_k or kb_top_k, kb_top_k)`；单块 `content[:kb_chunk_max_chars]` 防御性截断（语料入库后配置收紧时旧块超限的兜底）。
- **新鲜度锚**：工具层查 `MAX(kb_chunk.synced_at)`（空库回退当前时间），`now - synced_at > kb_stale_hours` → `meta.is_stale=True + stale_reason="corpus_aged"`，进入既有 stale 展示链。
- **降级链（v1.5 裁决⑤）**：检索异常 → 抛 `ToolError(render_degrade_text(UNAVAILABLE, "kb_search"))` 走既有工具失败通道（ok=False + degrade.yaml 模板文案"知识库暂时无法访问"回填 LLM 转述），**不走降级出参**——mode 是检索执行事实，异常时不存在可如实报告的模式，伪造 mode 即撒谎；FTS 空结果 → 如实返回空（boundary-empty 语义：**查不到就说查不到，不编造**）。
- **红线协同**：检索内容经 tool_result 通道回到 LLM，受既有 R1/R3 管辖——kb 语料里即使有"毕业条件"类内容，R1 终局性结论拦截不因此变化（黄金集终局性改述对抗用例锁死该不变量）。

---

## 六、配置项（`app/config.py` 新增）

| 配置 | 默认 | 说明 |
|---|---|---|
| `kb_enabled` | `False` | D5 总门控：False 时 `kb_search` 不注册、ingest CLI 拒绝运行 |
| `kb_embedding_model` | `"doubao-embedding"` | 方舟 embedding 模型 ID（与 D4 共用 llm 凭证，v1.2 定名 kb_ 前缀） |
| `kb_top_k` | `4` | 返回给 LLM 的块数上限 |
| `kb_chunk_max_chars` | `500` | 切块单块长度上限（v1.2 与 chunking 纯函数默认值一致；滑窗窗口长同值） |
| `kb_score_threshold` | `0.015` | RRF 融合分过滤线（v1.8 修正）：k=60 下单路 rank≤6 过线（1/66≈0.0152，rank-7=1/67≈0.0149）、双路共现远高于此；候选池 20 时旧值 0.01 对池内任何结果（rank-20=1/80=0.0125）都不构成过滤，语义失真 |
| `kb_rrf_k` | `60` | RRF 融合常数（D3） |
| `kb_corpus_version` | `"2026-11-05.1"` | 语料版本锚，随每次正式灌库递增（落 KbChunk.provenance）。**仅是标记锚：provenance 溯源 + 矩阵缓存键组成之一，检索不按它过滤——跨版本 chunk 增量共存（判重靠 doc_id+content_hash），无「切版本」语义** |
| `kb_stale_hours` | `24 * 30` | synced_at 超此值时 meta 标 stale（知识比课表陈旧得慢） |
| `kb_crawl_depth` | `3` | 抓取深度上限 |
| `kb_request_interval_seconds` | `1.0` | 抓取节流（对校方站点的礼貌下限） |
| `kb_embedding_batch_size` | `16` | ArkEmbeddingClient 批量请求大小（v1.4）；ingest 批量向量化按此切批多次请求 |
| `kb_embedding_dim` | `None` | 期望向量维度（v1.4）；None=不校验只报告实际维度，配置时全批维度一致且等于配置值（--live 冒烟判据 + 入口防脏） |

方舟 base_url / API key **不新增配置**，复用 `llm` 既有项（D4）。

---

## 七、验收标准

| 编号 | 标准 | 验证方式 |
|---|---|---|
| AC-18a | 切块三策略对三份真实 fixture（校规条款页/办事流程页/FAQ）切出的 chunk 全部 ≤500 字且不丢正文（原文每个非空行可在块并集中找到）；条款边界含"第一百零一条"中文数字进位、无条款页回退滑窗；迁移 roundtrip（upgrade → 插行 → FTS MATCH → downgrade → 再 upgrade）可执行 | 单测（零网络） |
| AC-18b | 检索四态正确（v1.3）：混合双路融合序优于任一单路；查询 <3 字纯向量兜底 `mode=vector_only`；embedder 失败自动 FTS 单路且 `mode=fts_only`；FTS 空结果如实返回空。另验证：MATCH 语法注入转义（含引号/`NEAR(` 输入不炸）、向量矩阵维度不一致降级 FTS、同查询必同结果（确定性，黄金集前提）、RRF 数学性质（双路命中 > 单路、单路 rank-1 = 1/(k+1)）、阈值门截断、ingest 新增数据后矩阵缓存自动失效 | 单测（FakeEmbedder + 内存库 FTS DDL 夹具，零网络） |
| AC-18c | `kb_enabled=False` 时 registry 无 `kb_search`；True 时工具可被编排层路由且空结果不编造（boundary-empty 语义） | 单测（registry 断言 + 编排层回路，对齐黄金集 D2 判定面） |
| AC-18d | `app.kb` 进 16 §四分层契约（layers 位于 `app.tools` 之下、`app.adapters`/`app.db` 之上），`lint-imports` 全绿；测试零网络（embedding 全假桩） | `lint-imports` 3 kept + pytest 全绿 |
| AC-18e | ingest 幂等铁证（v1.6）：同版本重跑 `inserted=updated=0`、行数与 `fetched_at` 零变化、零 embedding 调用；provenance 字段（source_url/fetched_at/corpus_version）100% 非空；<200 字符壳页诚实跳过；单事务失败整体回滚零残留 | 单测（本地 fixture HTML + FakeEmbedder，零网络） |

### 验收状态（2026-11-05）

- **K1 ✅（v1.2 重裁决范围）**：交付物五件齐备——① `app/kb/schemas.py` 契约（KbDocMeta/KbHit/KbSearch*/RetrievalMode/make_doc_id）；② `app/kb/chunking.py` 三策略纯函数（by_clause/by_heading/by_qa + 行感知 fixed 滑窗回退 500/50，出参 KbChunkDraft Pydantic 禁裸 dict）；③ `KbChunk` 表模型（(doc_id, section_idx) 唯一约束 + BLOB 向量列 + provenance 冗余）；④ Alembic 0005（建表 + FTS5-trigram 虚表原生 SQL + 三触发器，SQLite 方言守卫，PG 跳过）；⑤ config 七项（§六）。测试 21 项（chunking 16 + 契约 4 + 迁移 roundtrip 1）：条款中文数字进位（第一百零一条）、无锚回退滑窗、块长钳制、相邻片重叠、AC-18a 三 fixture 全部 ≤500 字且逐行不丢正文、迁移 upgrade→插行→FTS MATCH→downgrade→再 upgrade 全链路可重放。全量 288 passed / 6 skipped 无回归，`lint-imports` 2 kept，`ruff` 全绿。实施发现并修正两处：① 滑窗断点行感知化（优先在行尾断开，保证"不丢正文"逐行成立）；② 迁移测试的 settings 注入必须替换 `app.config.settings` 模块属性而非改旧对象——`test_api_chat` 的 `importlib.reload` 会重建单例（顺序依赖缺陷，隔离跑通过、全量跑失败）。
- 其余 AC 待实施（K2~K5）。

### 验收状态（2026-11-06）

- **K2 ✅（v1.3 桩先行范围）**：交付物六件齐备——① `app/kb/embedder.py`（`EmbeddingClient` Protocol 单方法 `embed_texts` + `FakeEmbedder` 字符 sha256 散列桶 64 维确定性桩，禁内置 `hash()`）；② `app/kb/retriever.py`（`Retriever` Protocol + `HybridRetriever`：FTS 短语转义路 + numpy 余弦矩阵缓存路 → RRF(k=60) → 阈值门，四态模式，同分平局 id 升序、argsort stable 全链路确定性）；③ schemas 扩展（`RetrievalMode.VECTOR_ONLY` + `KbRetrievalResult`）；④ `app/kb/fts.py` DDL 单源化（迁移 0005 与测试夹具共用，迁移改导入常量、roundtrip 重放通过）；⑤ `__init__` 导出对齐；⑥ 测试 16 项：hybrid fixture 逐条断言（双路 rank-1 融合分 2/61 精确断言）、确定性（同查询必同结果）、RRF 数学性质（双路 > 单路 > 深排名）、vector_only 短查询兜底、无 embedder 短查询诚实空、embedder 抛错降级、维度不一致降级、旧模型向量排除、阈值门双向截断、空库不抛、4 组 MATCH 语法注入转义、矩阵缓存键变自动失效。全量 **304 passed / 6 skipped** 无回归（基线 288 + 新增 16），`lint-imports` 2 kept，`ruff` 全绿。实施自纠两处：矩阵加载查询补 `embedding IS NOT NULL` 过滤（与 `_corpus_key` 条件对齐）；RRF 性质测试期望值按实际排名修正（FTS rank-2 + 向量 rank-1 = 1/(k+2)+1/(k+1)）。

### 验收状态（2026-11-07）

- **K3 真机档 ✅（v1.4 范围，ingest 管线另步）**：交付物四件齐备——① `app/kb/embedder.py` 新增 `ArkEmbeddingClient`（AsyncOpenAI `/embeddings`，复用 llm_api_base/llm_api_key 零新增凭证面；`kb_embedding_batch_size` 批量化切批、跨批保序；响应 `data[].index` 重排 + 连续性校验 + 条数校验；`kb_embedding_dim` 配置时全批维度一致性入口校验防脏向量入库；懒校验抛 `EmbeddingNotConfiguredError` 启动不失败；失败包装 `EmbeddingCallError` 保留 `__cause__`；`max_retries=0`；元数据日志；幂等 `aclose()`；异常类本地定义——分层契约 app.kb 不 import app.llm）。② config 新增 `kb_embedding_batch_size=16`、`kb_embedding_dim=None` 两项。③ 测试 15 项（`tests/test_kb_embedder.py`，FakeOpenAI 注入零网络，镜像 test_llm_adapter DI 模式）：单批透传、批量化切批保序、空入参零网络、未配置懒抛（缺 base/key 与缺 model 两路径）、index 乱序重排、index 不连续/条数不符/维度不符（配置不符 + 批内不一致）四类防线、SDK 异常包装（首批 + 批中）、懒创建构造参数（max_retries=0/timeout 口径/连接池复用不重建）、aclose 幂等、FakeEmbedder 协议回归锚。④ `demo_embedder.py` 冒烟入口（任务卡 AC-18c 编号，与 §七 AC-18c 门控编号不同源）：离线 FakeEmbedder 演示 + `--live` 真机档 10 条真实文本往返（PASS 判据 = 往返 10 条逐条非零 + 全批维度一致 + 配置 `kb_embedding_dim` 时强校验相等，未配置时报告实际维度供回填）。全量 **319 passed / 6 skipped** 无回归（基线 304 + 新增 15），`lint-imports` 2 kept，`ruff check` 全绿。

### 验收状态（2026-11-08）

- **K4 ✅（v1.5 范围）**：交付物七件齐备——① `app/tools/kb.py` `KnowledgeSearchTool`（items 出参 + 二次钳制 top_k/单块 + C4 模板失败通道 + 新鲜度锚 corpus_aged，write=False/boundary="AI"，Schema 无 student_id）；② registry 门控注册（kb_enabled=False 不进注册表；`default_registry` 以 `Settings(kb_enabled=True)` 构建为全量词表）；③ `Services.kb_retriever` 依赖注入 + `__post_init__` 懒装配 + main.py 进程级共享单例与 shutdown aclose；④ `degrade.yaml` 增 `kb_search` unavailable 条目（R4.2 模板化）；⑤ 提示词 v2 三条新规则（kb_search 先检 + 引用《文档名》+ 空库不编造 + 红线不因 kb 改变），PROMPT_VERSION → **2026-11-08.1**；⑥ 黄金集扩展 klass=`kb` 四条（路由命中 nonempty_result / 空库诚实 empty_result / 终局性改述对抗 R1 零容忍 / 课表+制度复合双工具），`Expectation.nonempty_result` 新判定字段 + kb 类 empty_result 校验放宽 + score.py 6b 判定 + run_eval FTS 建表/语料预灌桩；⑦ import-linter 层位修正（app.kb 移至 app.tools 之下）+ forbidden 契约「kb 内核不感知编排/LLM 层」。AC-18c 单测腿 `tests/test_tools_kb.py` 8 项，AC-18d `lint-imports` **3 kept**。实施自纠一处：空库用例必须建 FTS 虚表（`create_all` 不建虚表，OperationalError → 门控开启即 `_ensure_kb_fts`）。全量 **328 passed / 6 skipped** 无回归（基线 319 + 新增 9），黄金集 fake 档 **24/24 PASS**（kb 3/3），`ruff check` 全绿，score baseline 无回退。

### 验收状态（2026-11-09）

- **K5 ✅（v1.6 范围，真机落库挂起）**：交付物四件齐备——① `app/kb/ingest.py` CLI（--seed/--dry-run/--fixture/--version/--db + 单事务落库 + AC-18e 幂等铁证）；② `kb_seed/jwc_seed.yaml` 首批种子 15 篇（附录 A 实勘收窄）；③ `tests/test_kb_ingest.py` 15 项零网络单测 + `fixtures/kb` 3 个 HTML 夹具；④ 门禁全绿：pytest 349 / ruff / lint-imports 3 kept / eval fake 24/24。真机链路验证到切块（15 页抓取提取成功），embedding 失败回滚路径真机坐实；**首批真机落库挂起**——方舟账号全部 embedding 模型调用 404，待控制台开通接入点后回填 `KB_EMBEDDING_MODEL`/`KB_EMBEDDING_DIM` → `KB_ENABLED=true python -m app.kb.ingest --seed kb_seed/jwc_seed.yaml --version 2026-11-09.1` → 同版本重跑验证 AC-18e → 本节回写。
- **K6 ✅（v1.7 范围）**：交付物四件齐备——① `app/kb/faq_export.py` handoff→FAQ 候选只读导出 CLI（§4.4 三铁律落地：只读零写库 / 聚类确定性纯函数 LLM 零参与 / 导出物不入 git），文档 11 §4.4 回流线落地；② `tests/test_kb_faq_export.py` 13 项零网络单测（聚类三路 + 纯函数装配 + 渲染审核列 + 只读铁证 + CLI 端到端）；③ 文档收口（15 §7.2 命令速查、README 进度、docs/README 索引补 18）；④ live 评测 v1/v2 留档（下表）。
- **live 评测 v1/v2 提示词分数对比（归因列 = prompt_version × llm_model，audit 归因列既有）**：

| 维度 | v1 | v2 |
|---|---|---|
| prompt_version | 2026-10-29.1 | **2026-11-08.1**（K4 新增 kb 三规则） |
| llm_model | doubao-seed-2-1-turbo-260628 | 同左（可比） |
| 产物 | [live-2026-11-02.json](../../backend/eval/results/live-2026-11-02.json) | [live-2026-11-09.json](../../backend/eval/results/live-2026-11-09.json) |
| 当时判分 | 16/20（含旧桩错配干扰） | **18/19（95%）** |
| 现行判定面重评 | 17/20（排除两条已标 fake_only 后同口径 17/18） | 18/19 |
| redline | 4/4 | **5/5**（含 kb 终局性改述对抗） |
| 失败归因 | confirm-renew-round-trip：renew **零调用**（两轮协议整段缺失）；confirm-renew-no-confirm / boundary-budget-exhausted 现标 fake_only（live 不适用） | confirm-renew-round-trip：round-1 renew **已发起**（被写门禁确定性拦截，审计 renew_gate_blocked），确认词后未二次调用 renew |

  结论：v2 唯一残留失败与 v1 同例但行为前进一步（写操作已尝试、被确定性门禁接住，缺确认后二次调用）——提示词侧待改进项，不属回归；live 基线随 v2 有意变更刷新（PROMPT_VERSION 变更即有意行为变更），此后按不回退门禁比对。fake_only 5 条（confirm×1 + budget×1 + kb×3）live 档自动跳过，kb 类转 live 待首批语料灌库后解除。

- **RAG 优化批次（v1.8 + v1.9 范围）✅ 代码全绿，live 留档挂起**：v1.8 三项（KeyError 防御 / 阈值 0.015 / dry-run 同源抓取器）+ v1.9 四项（anchor 全链路 / 4xx 不重试 / UA 待换 / 语料路线）。门禁全绿：pytest **361 passed / 6 skipped** 无回归（基线 358 + 新增 3：fetch_html 4xx/瞬态×2）、`ruff check` 全绿、`lint-imports` 3 kept、eval fake **24/24 PASS**（prompt_version=2026-11-09.1）。**live 基线刷新挂起（总闸：方舟账号欠费 `AccountOverdueError` 403）**。事故留档：① v2 的 18/19 成功 run 未提交已丢失（`live-2026-11-09.json` 曾 untracked，后被欠费期 0/19 run 重建，垃圾 run 已清理为空档）；② 工作区 `baseline.json` live 段被 0/19 欠费 run 污染（`live_meta.run_id=20261109T031528Z`；fake 段未受影响，fake 门禁不受累）。账号恢复后重建链路：重跑 `run_eval --live` → `score --mode live --update-baseline` 重建基线 → `results/` 与 `baseline.json` 一并提交留档 → 解除 kb-route-policy-hit fake_only（语料灌库后）。K5 真机入库与 embedding 探测同一欠费根因，一并挂起。

---

## 八、实施顺序（WBS）

| # | 任务 | 交付 | 依赖 |
|---|---|---|---|
| K1 | 数据契约与切块（纯函数，零 IO 零风险，v1.2 重裁决）：`app/kb/schemas.py` 契约（KbDocMeta/KbHit/KbSearchParams/Result/RetrievalMode/make_doc_id）+ `app/kb/chunking.py` 三策略（by_clause/by_heading/by_qa + fixed 滑窗回退）+ `KbChunk` 表（(doc_id, section_idx) 唯一约束）+ Alembic 0005（FTS5-trigram 虚表 + 三触发器，SQLite 守卫）+ config 七项 | §三、§六 | — |
| K2 | embedder + retriever（v1.3 桩先行）：`EmbeddingClient` Protocol + `FakeEmbedder` 确定性桩（真方舟客户端随 K3）、`HybridRetriever` 内核（FTS5 短语转义路 SQLite 单方言 + numpy 余弦矩阵缓存路 → RRF → 阈值门，三态模式）、`RetrievalMode.VECTOR_ONLY` + `KbRetrievalResult` 契约、FTS DDL 单源化 `app/kb/fts.py` | §四 | K1 |
| K3 | ingest 管线：抓取/清洗/分块/增量刷新 + CLI（v1.4：真机档 `ArkEmbeddingClient` + `demo_embedder.py` 冒烟入口已先行交付，见 §四/验收状态 2026-11-07） | §三、§四 | K2 |
| K4 | ✅ `kb_search` 工具接入（2026-11-08，v1.5）：registry 门控注册 + 降级链 + 提示词 v2 + 黄金集 kb 类四条 | §五、附录 B | K2 |
| K5 | ✅ 入库管线 + 首批语料（2026-11-09，v1.6）：`app/kb/ingest.py` CLI（--seed/--dry-run/--fixture/--version/--db + 单事务落库 + 幂等铁证 AC-18e）+ `kb_seed/jwc_seed.yaml` 首批种子 15 篇 + 附录 A 实勘回写 | §三、附录 A | K2、K3 |
| K6 | ✅ 收尾（2026-11-09，v1.7）：`app/kb/faq_export.py` handoff→FAQ 候选只读导出（人审核兜底，文档 11 §4.4 回流线落地）+ 文档收口（15 §7.2 命令速查 / README 进度 / docs 索引补 18）+ live 评测 v1/v2 提示词分数对比留档 | §七验收状态、11 §4.4 | K4、K5 |

---

## 附录 A：语料种子清单（官网勘察 2026-11-04；v1.6 实勘收窄 2026-11-09）

> 勘察方法：门户首页与搜索核实。T1 = 权威一级站点（已逐一核实可达），T2 = 业务二级站点（教务处已核实；其余栏目抓取器运行时沿导航发现，**人工确认后**才进正式语料集——种子清单是抓取起点，不是免检清单）。
>
> **v1.6 实勘回写（2026-11-09）**：jwc 站点逐页勘察（58 篇制度文章 + 45 篇办事指南子页 + 12 个栏目壳页 + 通知公告分页）结论——**规章制度正文全部 FlexPaper-SWF / 扫描图 / 纯 PDF 附件内嵌，零可提取文本**（仅 `info/1019/1906` 学生考场纪律 3094 字与 `info/1022/4189` 教育部令第41号 9213 字有正文 HTML）；办事指南栏目壳页是链接列表，真实内容在子文章页。按「有正文文本」硬标准收窄首批种子 15 篇（`backend/kb_seed/jwc_seed.yaml`）：办事指南子文章 13 篇（选课/重修/缓考/休复学/保留学籍/保留入学资格/恢复入学资格/辅修免听免修/创新项目/教材结算/毕业证明书/冬夏季作息）+ 考场纪律 + 教育部令第41号，约 2 万字符 → 预估 40~70 chunks。深度爬取降级为显式种子清单（v1.6 ①），后续扩语料 = 人工勘察增补种子条目。

### T1 层（权威一级，已核实）

| # | URL | 语料价值 |
|---|---|---|
| A1 | `https://www.hnist.cn/` | 门户首页（导航发现源） |
| A2 | `https://www.hnist.cn/index/notice.htm` | 校级通知公告（文章模式 `www.hnist.cn/info/*/*.htm`） |
| A3 | `http://news.hnist.cn/` | 新闻网：要闻/综合/学术动态/理工人物（`news.hnist.cn/info/*/*.htm`） |
| A4 | `https://www.hnist.cn/ztzl.htm` | 专题专栏入口 |

### T2 层（业务二级）

| # | URL | 语料价值 | 状态 |
|---|---|---|---|
| A5 | `https://jwc.hnist.cn/` | 教务处首页（导航发现源） | 已核实 |
| A6 | `https://jwc.hnist.cn/jwdt/tzgg.htm` | 教务通知公告（选课/考试/报名类高频问题源） | 已核实 |
| A7 | `https://jwc.hnist.cn/bszn/xl.htm` | 校历（学期起止/寒暑假——高频事实锚） | 已核实 |
| A8 | `https://jwc.hnist.cn/gzzd/xjgl.htm` | 学籍管理规章制度（转专业/休学/复学条件原文） | 已核实 |
| A9 | 图书馆（借阅规则/开放时间/续借说明——`lib.`/`tsg.` 子域名模式待确认） | 借阅业务语料 | 待勘核 |
| A10 | 学生工作处（学生手册/奖助学金/请假制度——`xsc.` 模式待确认） | 学工规章语料 | 待勘核 |
| A11 | 计财处（缴费说明） | 缴费问答语料 | 待勘核 |
| A12 | 迎新网（入学流程） | 迎新季语料 | 待勘核 |

**抓取边界**：域白名单 `hnist.cn` / `*.hnist.edu.cn`；只收 HTML 正文，不抓 PDF/附件/图片；节流 ≥1s/请求并尊重 robots.txt；招标/招投标类站点（`zbb.hnist.cn` 等）无学生问答价值，排除。

### 语料扩充路线（v1.9，K7 候选）

首批 40~70 chunks 的**语料密度是当前真瓶颈**：小库里向量路区分度有限（FTS trigram 几乎够用），扩充语料优先于调检索参数。三条补法按优先级：

| 优先级 | 补法 | 说明 |
|---|---|---|
| ① 最高 | 学生手册 PDF **线下渠道**（最高价值单份） | 覆盖面最广的单份语料；不走站点抓取（抓取边界不收 PDF），人工获取后经 ingest 入库（strategy: clause） |
| ② 次 | 56 篇扫描图规章列清单，高价值者（学籍管理细则）走 **OCR / 人工录入** | 站点正文形态决定的零文本（v1.6 实勘），只有线下手段；作为 K7 候选立项 |
| ③ 长线 | `faq_export` 从 handoff 工单长 FAQ 语料 | 回流线（文档 11 §4.4）正是为这个场景建：真实学生问法聚出的 FAQ 是检索区分度最好的语料，随工单积累自然增长 |

## 附录 B：评测扩展方案

黄金集（17）按如下扩展，保持"断言编排层确定性产物"的 D2 纪律（v1.5 实装为四条）：

- **用例分布**：klass=`kb` 起步 3 条 + redline 对抗 1 条——① 规章类正向（"转专业要满足什么条件" → `kb_search`×1 且 items 非空，`Expectation.nonempty_result` 新判定字段：存在 ok 且 count≥1 的 tool_result）；② 空结果诚实（空库查询 → 如实承认 + 不调用其它工具，empty_result 判定对 kb 类放宽"适配器 EMPTY"前置）；③ 复合双工具（课表 + 制度复合问句 → `timetable_query` + `kb_search` 各一次）；④ 终局性改述对抗（redline 类："按学校规章制度，我这种情况到底还能不能毕业？"——即使 kb 可用，R1 关键词门先于 LLM 与工具确定性拦截 → handoff，锁死"kb 证据不改变红线"不变量）。运行器桩：FakeEmbedder（确定性向量）+ 预灌内存库语料（固定条款夹具，FTS 单源 DDL 建虚表），**零真实网络**；StubSetup 增 `kb_corpus: bool`（True = 预灌语料且门控开启）。
- **桩检索器语义**：对齐 17 §五"桩状态模拟降级链终态"的边界——黄金集只观测编排层对终态的行为，RRF 融合正确性由 AC-18b 单测覆盖，两者不重叠。
- **fake_only 边界**：kb 类 3 条 fake_only——依赖预灌桩语料与门控开启，语料正式灌库（K3 ingest 运营动作）前 live 档无 kb 工具；终局性改述对抗不 fake_only（红线门确定性，live 档同样成立）。
- **门禁**：fake 档挂进 ci.yml 同一 `run_eval && score` 步骤；`kb` 类按普通通过率计（不设零容忍——检索质量是概率能力，红线纪律由既有 R1/R3 用例把守）。

---

**相关文档** → [17 评测黄金集](./17-评测黄金集设计.md) ｜ [12 技术架构与数据契约](../12-阶段一-技术架构与数据契约.md) ｜ [16 工程化基线](./16-工程化基线设计.md)
