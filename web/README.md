# dot.rag Web 控制台

RAG 模块的 Web 界面：文档入库、切片预览、流式问答、链路追踪、文档管理。
后端为 FastAPI（`src/dot/rag/server.py`），前端为 React 18 + Vite + TS + Tailwind。

## 技术栈

- 后端：FastAPI + Uvicorn（SSE 流式问答），复用 `dot.rag` 的 RAGPipeline
- 前端：React 18 + Vite + TypeScript + Tailwind CSS

## 开发

```bash
# 1. 启动后端（项目根目录）
uv run python -m dot.rag serve
# 后端跑在 http://127.0.0.1:8000

# 2. 启动前端 dev server
cd web
npm run dev
# 前端跑在 http://localhost:5173，/api 自动代理到 8000（代理会剥掉 /api 前缀）
```

浏览器打开 `http://localhost:5173`。

## 生产构建

```bash
cd web
npm run build    # 产出 web/dist/
```

后端启动时自动托管 `web/dist/`，浏览器直接开 `http://127.0.0.1:8000` 即用（同源）。
后端同时以 根路径 和 /api 前缀 挂载了全部接口，开发代理与生产同源都能命中。

## 页面

| 路由 | 页面 | 功能 |
|---|---|---|
| `/` | 文档入库 | 文件上传（拖拽）/ 粘贴文本 / 扫描服务端目录 三种入库方式 |
| `/split` | 切片预览 | 实时调父子分块参数，可视化 parent → children 结构（不入库） |
| `/query` | 问答 | SSE 流式问答：检索片段（子块+score，展开看父块）→ 打字机回答 → 来源 + trace 链接 |
| `/trace` | 链路追踪 | 查询记录列表；详情含步骤耗时条形图、回答、原始 JSON；支持 `/trace?id=xxx` 直达 |
| `/documents` | 文档管理 | 文档列表（版本/状态）；逻辑删除（同时清除向量数据） |

## 后端接口（vite 代理剥 /api 前缀）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/query` | SSE 流式问答（stage / delta / done / error 事件） |
| POST | `/search` | 只检索，返回召回片段 |
| GET | `/documents` | 文档列表 |
| POST | `/documents/upload` | 上传文件入库（multipart，多文件） |
| POST | `/documents/text` | 粘贴文本入库 |
| POST | `/documents/ingest-dir` | 扫描目录批量入库 |
| DELETE | `/documents/{doc_id}` | 逻辑删除（登记表标记 + 清向量） |
| POST | `/split/preview` | 父子分块预览（不入库） |
| GET | `/traces`、`/traces/{id}` | 链路列表 / 详情 |
