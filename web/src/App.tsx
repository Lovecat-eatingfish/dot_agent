import type { ReactNode } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import IngestPage from "./pages/IngestPage";
import SplitterPage from "./pages/SplitterPage";
import ChatPage from "./pages/ChatPage";
import TracePage from "./pages/TracePage";
import DocumentsPage from "./pages/DocumentsPage";

const navItems = [
  { to: "/", label: "文档入库", end: true },
  { to: "/split", label: "切片预览" },
  { to: "/query", label: "问答" },
  { to: "/trace", label: "链路追踪" },
  { to: "/documents", label: "文档管理" },
];

// 普通页面的居中容器（自带滚动）
function Page({ children }: { children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl p-6">{children}</div>
    </div>
  );
}

// 页头：标题 + 说明
export function PageHeader({ title, desc }: { title: string; desc?: string }) {
  return (
    <div className="mb-5">
      <h2 className="text-lg font-semibold tracking-tight text-slate-900">{title}</h2>
      {desc && <p className="mt-1 text-[13px] leading-relaxed text-slate-400">{desc}</p>}
    </div>
  );
}

export default function App() {
  return (
    <div className="flex h-screen bg-canvas">
      {/* 侧边栏：深色仪器面板 */}
      <aside className="flex w-52 shrink-0 flex-col bg-ink">
        <div className="px-5 pb-5 pt-6">
          <h1 className="font-mono text-[17px] font-semibold tracking-tight text-white">
            dot<span className="text-indigo-400">.</span>rag
          </h1>
          <p className="mt-1 text-[11px] text-slate-500">RAG 检索控制台</p>
        </div>
        <nav className="flex-1 space-y-0.5 px-3 py-2">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `relative block rounded-lg px-3 py-2 text-[13px] transition ${
                  isActive
                    ? "bg-white/10 font-medium text-white before:absolute before:left-0 before:top-1/2 before:h-4 before:w-0.5 before:-translate-y-1/2 before:rounded-full before:bg-indigo-400"
                    : "text-slate-400 hover:bg-white/5 hover:text-slate-200"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-white/10 px-5 py-3">
          <p className="font-mono text-[10px] leading-relaxed text-slate-600">
            LangChain × Chroma
            <br />
            FastAPI × React
          </p>
        </div>
      </aside>

      {/* 主内容：聊天页占满全高（内部自己管理滚动），其余页面走居中容器 */}
      <main className="min-w-0 flex-1 overflow-hidden">
        <Routes>
          <Route path="/query" element={<ChatPage />} />
          <Route path="/" element={<Page><IngestPage /></Page>} />
          <Route path="/split" element={<Page><SplitterPage /></Page>} />
          <Route path="/trace" element={<Page><TracePage /></Page>} />
          <Route path="/documents" element={<Page><DocumentsPage /></Page>} />
        </Routes>
      </main>
    </div>
  );
}
