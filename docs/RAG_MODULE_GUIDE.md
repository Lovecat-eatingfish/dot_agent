# RAG 模块详解

本文档对项目的 RAG 模块进行完整讲解，按数据流顺序逐个模块展开。

## 核心流水线

```
文档接入 → 解析 → 结构感知分割 → Embedding → ChromaDB 向量存储 → 混合检索 → 答案生成
```

---

## 1. 数据模型（types.py）

定义了三个核心数据结构：

- **ParentChunk**：大块原文，不向量化，用于查询时返回完整上下文
- **ChildChunk**：小块切片，向量化用于检索，每个 child 绑定一个 parent_id
- **DocRecord**：文档级元数据，带 version 和 deleted 标记

**父子分块**是这个 RAG 的核心设计。为什么需要父子分块？
- 如果只存小 chunk，检索命中后上下文残缺
- 如果只存大 chunk，检索精度差
- 父子结合：child 用于向量检索，命中后按 parent_id 取回完整的 parent 原文

---

## 2. 文档解析（loader.py）

三种输入来源：
- **文本**：直接传入，返回单页
- **文件**：按扩展名分发，`.pdf` 用 pypdf 逐页提取，其他按 utf-8/gbk/latin-1 读取
- **URL**：urllib 抓取 + BeautifulSoup 提取正文（去 script/style），带 SSRF 防护

返回统一的 `ParsedPage = tuple[str, int | None]`，即 (文本, 页码)。

---

## 3. 结构感知分割器（splitter.py）

这是这个 RAG 最值得学习的部分之一。

**两层分割策略**：
1. **第一层 - 结构感知**：按 Markdown 标题层级切分，保留 heading_path 元数据；代码块（```）整体保留不腰斩
2. **第二层 - 递归字符降级**：超 chunk_size 的块用 LangChain 的 RecursiveCharacterTextSplitter 按分隔符列表递归切分

**父子分块模式**（`split_text_parent_child`）：
- parent 用结构感知切大块（2000 字符），保留完整上下文
- child 对 parent 再递归字符切细块（500 字符），用于向量化检索
- child.metadata 带 parent_index，可拼出 parent_id

---

## 4. Embedding（embedding.py）

定义了 `Embedder` Protocol（鸭子类型），两个实现：

- **OpenAIEmbedder**：远程调用 OpenAI API（默认）
- **LocalEmbedder**：本地 sentence-transformers（离线，首次慢）
- **FakeEmbedder**：确定性哈希向量（测试用，不依赖网络）

工厂函数 `create_embedder()` 读 `RAG_EMBEDDING_BACKEND` 环境变量选择后端。

---

## 5. 存储后端（backends/local_file.py）

这是实际落盘的地方，实现了 `StoreBackend` Protocol：

**存储策略**：
- **child 向量** → ChromaDB（collection: `mokioclaw_rag`）
- **parent 原文** → JSON 文件（`parents/{doc_id}.json`）

**版本管理**：
- 同 doc_id 重新入库时，version + 1
- 旧版本 Chroma 记录标记 `deleted=True`
- 查询时自动过滤 `deleted=False`

**逻辑删除**：`delete_doc` 不物理删除，只标记 deleted，保留审计能力。

---

## 6. 兼容门面（store.py）

`ChromaStore` 是向后兼容的门面，内部委托 `LocalFileBackend`。旧的 `add(chunks)` 接口走退化模式（parent=child），行为等价旧实现。

---

## 7. 混合检索（retrieval.py）

这是检索的核心，三步走：

1. **向量召回**：用 ChromaDB similarity_search 做语义匹配
2. **BM25 召回**：纯 Python rank_bm25 做关键词匹配（专有名词/编号强召回）
3. **RRF 融合**：Reciprocal Rank Fusion，两路结果去重 + 重排

公式：`score = Σ 1/(k + rank)`，k=60。

最后按 child 的 parent_id 取回 parent 原文，返回完整上下文。

---

## 8. Reranker（reranker.py）

Cross-encoder 精排，但目前是**框架预留**状态：
- 配置了 `RAG_RERANKER_MODEL` + `RAG_RERANKER_STUB=1` 才可用
- 未配置时 `available=False`，自动降级返回 RRF 原序
- 真实 ONNX 推理尚未实现（当前用 token overlap 做占位）

---

## 9. Query 改写（query_transform.py）

三种 LLM 改写策略（LLM 不可用全部降级）：

- **Multi-Query**：一个问句扩写成多个角度的问句，多路检索
- **HyDE**：先生成假设性答案，用答案向量检索（缩小 query-doc 语义鸿沟）
- **Step-Back**：把具体问题抽象成更上位的问题

---

## 10. Self-Query（self_query.py）

用 LLM 从自然语言中提取结构化过滤条件（如「只看 v2 的文档」→ `{"version": 2}`），只允许白名单字段，防注入。

---

## 11. 上下文构建（context_builder.py）

解决三个问题：
- **去重**：按 parent_id 去重（同 parent 只留一次）
- **长度预算**：按 max_chars 截断，保证至少有一个片段
- **结构化**：每片段带编号 `[n]` + heading_path，供引用溯源用

---

## 12. 答案生成（answer.py）

LLM 生成带引用的答案，Prompt 强制约束：
- 仅基于上下文回答
- 禁止幻觉
- 用 `[n]` 引用标注来源

LLM 不可用时降级为「片段列表直出」。

---

## 13. 引用溯源（citation.py）

- 从答案文本提取 `[n]` 引用标记（支持 `[1]` `[1,2]` `[1-3]` 等格式）
- 校验引用合法性（超出 citations 范围的编号丢弃，防 LLM 编造）
- 输出引用列表（doc_id + source），供前端展示来源

---

## 14. 输出护栏（guardrails.py）

答案输出前的安全检查，纯正则规则：
- 敏感信息泄漏（API key、token、手机号、邮箱、身份证）
- Prompt injection 产出（ignore previous instructions 等）
- 命中 → 替换为拒答话术

---

## 15. 语义缓存（cache.py）

- 精确命中：query 完全一致 + cache_key 匹配
- 语义命中：embedding 余弦相似度超阈值（默认 0.92）
- TTL 过期失效（默认 1 小时）
- FIFO 淘汰（最多 1000 条）
- 支持换 Redis/Memcached（实现 CacheBackend Protocol）

---

## 16. RAG 服务（service.py）

FastAPI 应用，把所有模块串起来：

**路由**：
- `POST /ingest/text` - 文本入库
- `POST /ingest/url` - URL 抓取入库
- `POST /ingest/file` - 文件上传入库
- `POST /preview/split` - 切片预览（不入库）
- `POST /query` - 全链路 RAG（可选 query 改写 + self-query + 混合检索 + rerank + LIM 重排 + 答案生成 + 引用 + guardrails + 缓存）
- `GET /documents` - 列出文档
- `DELETE /documents/{id}` - 逻辑删除
- `GET /trace/{id}` - 按 trace_id 查链路
- `POST /cache/clear` - 清缓存
- `GET /health` - 健康检查

**`/query` 的完整流水线**：
```
语义缓存查 → query 改写 → self-query 过滤 → 多路检索 → RRF 融合 → rerank → LIM 重排 → 上下文构建 → 答案生成 → 引用溯源 → guardrails → 写缓存
```

每个步骤都是 **opt-in** 的（默认关闭高级功能），LLM 不可用时全部降级。

---

## 17. Trace（trace.py）

每请求唯一 trace_id 贯穿全程，记录每一步的 step/hits/degraded，落盘到 `.mokioclaw/rag/traces/{trace_id}.jsonl`。

---

## 18. 安全（security.py）

- `sanitize_doc_id`：防止路径穿越（拒绝 `/` `\` `..`，仅允许安全字符集）
- `validate_fetch_url`：SSRF 防护（拒绝内网/loopback/metadata/私有 IP，返回解析的 IP 列表供后续验证防 DNS 重绑定）

---

## 设计哲学总结

整个 RAG 模块遵循几个核心原则：

1. **减法设计**：每个功能都有降级路径，LLM 不可用 → 检索直出，不阻塞
2. **Protocol 抽象**：StoreBackend、CacheBackend、Embedder 都是 Protocol，可换实现
3. **退化兼容**：ChromaStore 门面保持旧接口，新能力 opt-in
4. **安全优先**：SSRF 防护、路径穿越防护、prompt injection 护栏
5. **可观测性**：每步可 trace，每步可降级，每步有日志
