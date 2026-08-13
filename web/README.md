# MokioClaw RAG Web 控制台

针对 RAG 模块的 Web 前端：文档入库、切片预览、问答、链路追踪可视化、文档管理。

## 技术栈

React 18 + Vite + TypeScript + Tailwind CSS + Recharts

## 开发

```bash
# 1. 安装依赖
cd web
npm install

# 2. 启动后端（另一终端）
cd ..
uv run dotagent rag serve --foreground
# 后端跑在 http://127.0.0.1:8000

# 3. 启动前端 dev server
npm run dev
# 前端跑在 http://localhost:5173，/api 自动代理到 8000
```

浏览器打开 `http://localhost:5173`。

## 生产构建

```bash
cd web
npm run build    # 产出 web/dist/
```

构建后 `uv run dotagent rag serve --foreground` 会自动托管 `web/dist/`，
浏览器直接开 `http://127.0.0.1:8000` 即用（同源，无需代理）。

## 页面

| 路由 | 页面 | 功能 |
|---|---|---|
| `/` | 文档入库 | 文件上传 / 文本 / URL 三种接入方式 |
| `/split` | 切片预览 | 实时调父子分块参数，可视化 parent → child 结构（不入库） |
| `/query` | 问答 | 全链路 RAG 查询，高级开关，答案 + 引用 + 检索片段 |
| `/trace` | 链路追踪 | 按 trace_id 查单次请求完整链路，步骤时间线 + 耗时图 |
| `/documents` | 文档管理 | 文档列表 + version + 逻辑删除 |
