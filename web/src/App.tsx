import { NavLink, Route, Routes } from "react-router-dom";
import IngestPage from "./pages/IngestPage";
import SplitterPage from "./pages/SplitterPage";
import QueryPage from "./pages/QueryPage";
import TracePage from "./pages/TracePage";
import DocumentsPage from "./pages/DocumentsPage";

const navItems = [
  { to: "/", label: "文档入库", end: true },
  { to: "/split", label: "切片预览" },
  { to: "/query", label: "问答" },
  { to: "/trace", label: "链路追踪" },
  { to: "/documents", label: "文档管理" },
];

export default function App() {
  return (
    <div className="flex h-screen">
      {/* 侧边栏 */}
      <aside className="w-56 shrink-0 border-r border-slate-200 bg-white flex flex-col">
        <div className="px-5 py-5 border-b border-slate-200">
          <h1 className="text-lg font-bold text-slate-800">MokioClaw RAG</h1>
          <p className="text-xs text-slate-400 mt-0.5">生产级 RAG 控制台</p>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `block px-3 py-2 rounded-md text-sm transition-colors ${
                  isActive
                    ? "bg-brand-50 text-brand-700 font-medium"
                    : "text-slate-600 hover:bg-slate-50"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="px-5 py-3 border-t border-slate-200 text-xs text-slate-400">
          FastAPI · ChromaDB · RRF
        </div>
      </aside>

      {/* 主内容区 */}
      <main className="flex-1 overflow-auto">
        <Routes>
          <Route path="/" element={<IngestPage />} />
          <Route path="/split" element={<SplitterPage />} />
          <Route path="/query" element={<QueryPage />} />
          <Route path="/trace" element={<TracePage />} />
          <Route path="/documents" element={<DocumentsPage />} />
        </Routes>
      </main>
    </div>
  );
}
