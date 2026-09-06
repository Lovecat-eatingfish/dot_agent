# dot.rag — RAG（检索增强生成）模块

基于 **LangChain** 的 RAG 全链路实现（参考 [All-in-RAG 教程](https://datawhalechina.github.io/all-in-rag/#/)），
覆盖：**数据准备 → 索引构建 → 检索 → 生成集成**。

```
依赖方向：
  dot.rag ──→ dot.ai          （生成端复用 OpenAI 兼容 Provider）
          └─→ langchain 生态  （分块 / Milvus / HuggingFace Embedding）
```

## 技术选型（当前全部本地化，后续可平滑迁移）

| 环节     | 当前方案                                  | 迁移方式                          |
| -------- | ----------------------------------------- | --------------------------------- |
| Embedding| 本地 HuggingFace 模型 `BAAI/bge-small-zh-v1.5` | 改 `RAG_EMBEDDING_MODEL` 或在 `embeddings.py` 加云端实现 |
| 向量库   | Chroma（本地持久化目录，开箱即用）         | 改 `RAG_VECTOR_BACKEND=chroma\|milvus`，走 LangChain 统一接口，新增后端只需在 `vectorstore.py` 加一个工厂分支 |
| 元数据   | SQLite（`MetadataStoreBase` 抽象类）       | 改 `RAG_METADATA_BACKEND`，新后端实现该抽象类并在 `store.py` 工厂注册即可 |
| 生成 LLM | 复用 `dot.ai` OpenAI 兼容 Provider         | 改 `.env` 的 `API_KEY/BASE_URL/MODEL`（Ollama/vLLM/云端均可） |
| 分块     | `RecursiveCharacterTextSplitter`           | 配置 `RAG_CHUNK_SIZE / RAG_CHUNK_OVERLAP` |

## 快速上手

```bash
# 1. 放文档到 src/dot/rag/docs/（支持 .txt/.md/.pdf/.docx，网页上传的文件也保存在这里）

# 2. 建索引
uv run python -m dot.rag ingest

# 3. 检索问答
uv run python -m dot.rag query "dot_agent 由哪些模块组成？"
uv run python -m dot.rag search "什么是 RAG"   # 只看召回片段，调试检索质量
```

Python API：

```python
from dot.rag import RAGPipeline

pipeline = RAGPipeline()
pipeline.ingest("src/dot/rag/docs")         # 建索引
result = await pipeline.aquery("什么是RAG") # 问答（answer + source_documents）
```

## 配置项（.env，前缀 `RAG_`）

| 变量                           | 默认值                              | 说明                     |
| ------------------------------ | ----------------------------------- | ------------------------ |
| `RAG_METADATA_BACKEND`         | `sqlite`                            | 元数据后端                |
| `RAG_VECTOR_BACKEND`           | `chroma`                            | 向量库后端（chroma/milvus）|
| `RAG_EMBEDDING_MODEL`          | `BAAI/bge-small-zh-v1.5`            | 本地 Embedding 模型       |
| `RAG_EMBEDDING_DEVICE`         | `cpu`                               | 有显卡可改 `cuda`         |
| `RAG_VECTOR_DB_DIR`            | `data/rag/chroma`                   | 向量库持久化目录          |
| `RAG_COLLECTION_NAME`          | `dot_rag`                           | 集合名                    |
| `RAG_CHUNK_SIZE`               | `500`                               | 分块大小（字符）          |
| `RAG_CHUNK_OVERLAP`            | `50`                                | 分块重叠                  |
| `RAG_TOP_K`                    | `4`                                 | 检索条数                  |
| `RAG_GENERATE_MODEL`           | 空（沿用 `MODEL`）                  | 生成模型，可与主模型不同  |

> 国内网络下载 HuggingFace 模型默认走 `hf-mirror.com` 镜像（可用 `HF_ENDPOINT` 覆盖）。

## 模块结构

```
src/dot/rag/
├── config.py      # RagSettings（pydantic-settings，RAG_ 前缀）
├── embeddings.py  # 本地 Embedding（BGE + 查询指令前缀，进程内缓存）
├── loader.py      # 文档加载（txt/md/pdf/docx）+ 递归分块 + 父子分块
├── vectorstore.py # 向量库后端工厂（chroma/milvus，LangChain 统一接口）
├── generator.py   # 拼上下文 → dot.ai Provider 流式生成
├── pipeline.py    # RAGPipeline：索引/检索/生成/文档管理/trace
├── store.py       # 元数据抽象类 MetadataStoreBase + SQLite 实现 + 工厂
├── server.py      # FastAPI：SSE 问答 / 文档管理 / 切片预览 / trace / 托管 dist
└── cli.py         # typer 命令行（ingest / query / search / serve）
```

Web 控制台见 `web/`（React + Vite），启动方式：`uv run python -m dot.rag serve`。

## TODO（后续迭代）

- [ ] 混合检索（BM25 + 向量）与 rerank
- [ ] RAGAS 评估接入
- [ ] GraphRAG（教程第七章）
- [ ] 与 dot.agent 工具系统打通，作为 Agent 的检索工具
