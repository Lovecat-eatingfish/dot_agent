import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { deleteSession, listMessages, listSessions, streamChat } from "../api";
import type { ChunkItem, MessageRecord, SessionRecord } from "../types";
import { Badge, Button } from "../components/ui";

// 页面内的消息模型：本地流式中的 assistant 消息还没有 id，用 seq 兜底
interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  trace_id?: string;
  sources?: string[];
  chunks?: ChunkItem[]; // 本次回答的检索片段（本地流式期间暂存）
  streaming?: boolean;
}

export default function ChatPage() {
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState(4);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const activeIdRef = useRef<string | null>(null);
  activeIdRef.current = activeId;

  const scrollToBottom = useCallback(() => {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    });
  }, []);

  const refreshSessions = useCallback(async () => {
    const { sessions: list } = await listSessions();
    setSessions(list);
    return list;
  }, []);

  const openSession = useCallback(async (id: string | null) => {
    setActiveId(id);
    setMessages([]);
    setError("");
    if (id) {
      try {
        const { messages: list } = await listMessages(id);
        setMessages(list.map((m: MessageRecord) => ({ ...m })));
        scrollToBottom();
      } catch {
        /* 会话可能刚被删 */
      }
    }
  }, [scrollToBottom]);

  useEffect(() => {
    (async () => {
      const list = await refreshSessions().catch(() => []);
      if (list.length > 0) openSession(list[0].id);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleDeleteSession(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    if (!window.confirm("删除该会话及其全部聊天记录？")) return;
    await deleteSession(id).catch(() => {});
    const list = await refreshSessions();
    if (activeIdRef.current === id) {
      setActiveId(null);
      setMessages([]);
      if (list.length > 0) openSession(list[0].id);
    }
  }

  function handleNewSession() {
    if (streaming) return;
    openSession(null); // 懒创建：首次提问时后端自动建会话
  }

  async function handleSend() {
    const q = question.trim();
    if (!q || streaming) return;

    setStreaming(true);
    setError("");
    setQuestion("");

    const userMsg: ChatMessage = { id: `u-${Date.now()}`, role: "user", content: q };
    const assistantMsg: ChatMessage = {
      id: `a-${Date.now()}`, role: "assistant", content: "", streaming: true,
    };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    scrollToBottom();

    const controller = new AbortController();
    abortRef.current = controller;

    const patchAssistant = (patch: Partial<ChatMessage>) =>
      setMessages((prev) => prev.map((m) => (m.id === assistantMsg.id ? { ...m, ...patch } : m)));

    try {
      await streamChat(
        { question: q, session_id: activeId ?? undefined, top_k: topK },
        (event) => {
          switch (event.type) {
            case "session":
              // 后端自动新建的会话：同步 id 到本地
              if (!activeIdRef.current) {
                activeIdRef.current = event.session_id;
                setActiveId(event.session_id);
                refreshSessions();
              } else {
                refreshSessions();
              }
              break;
            case "stage":
              patchAssistant({ chunks: event.chunks });
              break;
            case "delta":
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMsg.id ? { ...m, content: m.content + event.text } : m,
                ),
              );
              scrollToBottom();
              break;
            case "done":
              patchAssistant({
                id: event.message_id || assistantMsg.id,
                content: event.answer,
                sources: event.sources,
                trace_id: event.trace_id,
                streaming: false,
              });
              refreshSessions();
              break;
            case "error":
              setError(event.message);
              break;
          }
        },
        controller.signal,
      );
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      // 兜底：无论正常结束还是中断，都关掉流式状态
      setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)));
      setStreaming(false);
      abortRef.current = null;
    }
  }

  return (
    <div className="flex h-full gap-4 p-4">
      {/* 会话侧栏 */}
      <aside className="flex w-56 shrink-0 flex-col rounded-xl border border-slate-200/70 bg-white shadow-card">
        <div className="p-3">
          <Button variant="primary" className="w-full justify-center" onClick={handleNewSession}>
            ＋ 新建会话
          </Button>
        </div>
        <div className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
          {sessions.length === 0 && <p className="px-2 py-4 text-xs text-slate-400">还没有会话</p>}
          {sessions.map((s) => (
            <div
              key={s.id}
              onClick={() => !streaming && openSession(s.id)}
              className={`group flex cursor-pointer items-center gap-1 rounded-lg px-2.5 py-2 text-[13px] transition ${
                activeId === s.id
                  ? "bg-indigo-50 font-medium text-indigo-700"
                  : "text-slate-600 hover:bg-slate-50"
              }`}
            >
              <span className="flex-1 truncate">{s.title || "新会话"}</span>
              <button
                className="hidden text-xs text-slate-400 hover:text-red-500 group-hover:block"
                title="删除会话"
                onClick={(e) => handleDeleteSession(s.id, e)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
        <div className="border-t border-slate-100 p-3">
          <label className="flex items-center gap-2 text-xs text-slate-500">
            <span className="font-mono">top-k</span>
            <select
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value))}
              className="flex-1 rounded-lg border border-slate-300 bg-white px-1.5 py-1 font-mono text-xs focus:border-indigo-500 focus:outline-none"
            >
              {[2, 4, 6, 8, 10].map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
          </label>
        </div>
      </aside>

      {/* 聊天主区 */}
      <section className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-slate-200/70 bg-white shadow-card">
        {/* 会话标题条 */}
        <div className="flex items-center gap-2 border-b border-slate-100 px-5 py-2.5">
          <span className="h-1.5 w-1.5 rounded-full bg-indigo-400" />
          <span className="truncate text-[13px] font-medium text-slate-600">
            {sessions.find((s) => s.id === activeId)?.title || "新会话"}
          </span>
          <span className="ml-auto font-mono text-[10px] uppercase tracking-wider text-slate-300">
            RAG Chat
          </span>
        </div>

        {/* 消息列表 */}
        <div ref={scrollRef} className="flex-1 space-y-5 overflow-y-auto bg-[#F8F9FC] px-5 py-5">
          {messages.length === 0 && (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <p className="font-mono text-sm text-slate-400">dot<span className="text-indigo-400">.</span>rag chat</p>
              <p className="mt-2 text-[13px] text-slate-400">基于已入库文档的检索问答</p>
              <p className="mt-0.5 text-xs text-slate-300">同一会话内支持多轮上下文，Enter 发送</p>
            </div>
          )}
          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="flex justify-end">
                <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-indigo-600 px-4 py-2.5 text-[13px] leading-relaxed text-white shadow-sm">
                  {m.content}
                </div>
              </div>
            ) : (
              <div key={m.id} className="flex justify-start">
                <div className="max-w-[85%] space-y-2">
                  <div className="whitespace-pre-wrap rounded-2xl rounded-bl-md border border-slate-200/70 bg-white px-4 py-2.5 text-[13px] leading-relaxed text-slate-700 shadow-card">
                    {m.content}
                    {m.streaming && <span className="ml-0.5 inline-block h-4 w-2 animate-pulse bg-indigo-500 align-middle" />}
                  </div>
                  {!m.streaming && (m.sources?.length || m.chunks?.length) ? (
                    <div className="space-y-1 pl-1">
                      {m.chunks && m.chunks.length > 0 && (
                        <details className="text-xs text-slate-400">
                          <summary className="cursor-pointer select-none hover:text-slate-600">
                            检索片段（{m.chunks.length} 条，耗时见链路）
                          </summary>
                          <div className="mt-1 space-y-1">
                            {m.chunks.map((c, i) => (
                              <div key={i} className="rounded-lg border border-slate-100 border-l-2 border-l-amber-400 bg-white p-2">
                                <span className="mr-1.5 font-medium text-slate-500">{fileName(c.source)}</span>
                                {c.score != null && <Badge tone="green">{c.score}</Badge>}
                                <p className="mt-1 leading-relaxed text-slate-500">{c.text}</p>
                              </div>
                            ))}
                          </div>
                        </details>
                      )}
                      {m.sources && m.sources.length > 0 && (
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-xs text-slate-400">来源</span>
                          {m.sources.map((s) => (
                            <Badge key={s} tone="slate">{fileName(s)}</Badge>
                          ))}
                        </div>
                      )}
                      {m.trace_id && (
                        <p className="text-xs text-slate-400">
                          链路
                          <Link to={`/trace?id=${m.trace_id}`} className="ml-1.5 font-mono text-indigo-600 hover:underline">
                            {m.trace_id}
                          </Link>
                        </p>
                      )}
                    </div>
                  ) : null}
                </div>
              </div>
            ),
          )}
        </div>

        {/* 输入区 */}
        <div className="border-t border-slate-100 bg-white p-3">
          {error && <p className="mb-2 font-mono text-xs text-red-600">{error}</p>}
          <div className="flex items-end gap-2">
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
              rows={1}
              placeholder="输入问题，Enter 发送，Shift+Enter 换行…"
              className="max-h-32 min-h-[40px] flex-1 resize-y rounded-xl border border-slate-200 bg-slate-50/60 px-3.5 py-2.5 text-[13px] leading-relaxed focus:border-indigo-500 focus:bg-white focus:outline-none"
            />
            {streaming ? (
              <Button variant="danger" onClick={() => abortRef.current?.abort()}>
                停止
              </Button>
            ) : (
              <Button variant="primary" className="h-[40px] rounded-xl px-4" disabled={!question.trim()} onClick={handleSend}>
                发送
              </Button>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}
