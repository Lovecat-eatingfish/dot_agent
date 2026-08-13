import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { SplitPreviewResp } from "../types";
import { Button, Card, PageHeader, Spinner, ErrorBanner, Badge } from "../components/ui";

const SAMPLE = `# MokioClaw 项目说明

## 概述
MokioClaw 是一个对标 Claude Code 的 AI Coding Agent，使用 LangGraph 编排多智能体工作流。

## 核心架构

### 引擎层
实现 BudgetTracker + OutputTokenRecovery + PromptTooLongRecovery 三段式恢复链。

### 工具层
自研工具执行管线，支持并行执行、危险命令拦截、输出预算控制。

\`\`\`python
def create_model():
    return ChatOpenAI(model="gpt-4o", temperature=0)
\`\`\`

## 安装
\`\`\`bash
uv add langchain chromadb
\`\`\`
`;

export default function SplitterPage() {
  const [content, setContent] = useState(SAMPLE);
  const [parentSize, setParentSize] = useState(2000);
  const [childSize, setChildSize] = useState(500);
  const [childOverlap, setChildOverlap] = useState(80);
  const [result, setResult] = useState<SplitPreviewResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [hoverParent, setHoverParent] = useState<number | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function runPreview(text: string, pSize: number, cSize: number, cOverlap: number) {
    if (!text.trim()) {
      setResult(null);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const r = await api.previewSplit(text, {
        parentSize: pSize,
        childSize: cSize,
        childOverlap: cOverlap,
      });
      if (r.ok) setResult(r);
      else setError(r.error || "切分失败");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  // debounce 自动预览
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      runPreview(content, parentSize, childSize, childOverlap);
    }, 500);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [content, parentSize, childSize, childOverlap]);

  return (
    <div className="p-8">
      <PageHeader title="切片预览" subtitle="实时调整父子分块参数，可视化 parent → child 结构（不入库）" />

      <div className="grid grid-cols-2 gap-6">
        {/* 左侧：输入 + 参数 */}
        <div className="space-y-4">
          <Card className="p-4">
            <label className="block text-sm text-slate-600 mb-2">文本内容</label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={16}
              className="w-full px-3 py-2 border border-slate-300 rounded-md text-xs font-mono focus:outline-none focus:border-brand-500 resize-y"
            />
          </Card>

          <Card className="p-4 space-y-3">
            <Slider label="父块大小 parent_size" value={parentSize} min={200} max={5000} step={100} onChange={setParentSize} />
            <Slider label="子块大小 child_size" value={childSize} min={100} max={2000} step={50} onChange={setChildSize} />
            <Slider label="重叠 child_overlap" value={childOverlap} min={0} max={500} step={10} onChange={setChildOverlap} />
            <Button variant="secondary" onClick={() => runPreview(content, parentSize, childSize, childOverlap)} disabled={loading}>
              {loading ? <Spinner /> : "重新切分"}
            </Button>
          </Card>
        </div>

        {/* 右侧：结果 */}
        <div className="space-y-4">
          {error && <ErrorBanner message={error} />}

          {result && (
            <>
              <Card className="p-4">
                <div className="grid grid-cols-3 gap-3 text-center">
                  <Stat label="父块数" value={result.stats.parent_count} />
                  <Stat label="子块数" value={result.stats.child_count} />
                  <Stat label="平均子块长度" value={result.stats.avg_child_len} />
                </div>
              </Card>

              {/* Parent 块 */}
              <div>
                <h3 className="text-sm font-semibold text-slate-700 mb-2">父块（{result.parents.length}）</h3>
                <div className="space-y-2 max-h-60 overflow-auto pr-1">
                  {result.parents.map((p, i) => (
                    <div
                      key={i}
                      onMouseEnter={() => setHoverParent(i)}
                      onMouseLeave={() => setHoverParent(null)}
                      className={`p-2 rounded border text-xs cursor-pointer transition-colors ${
                        hoverParent === i
                          ? "border-brand-400 bg-brand-50"
                          : "border-slate-200 bg-white"
                      }`}
                    >
                      <div className="flex items-center justify-between mb-1">
                        <Badge color="blue">parent[{i}]</Badge>
                        {p.metadata.heading_path && (
                          <span className="text-slate-400">{p.metadata.heading_path}</span>
                        )}
                      </div>
                      <p className="text-slate-600 line-clamp-2 font-mono">{p.content.slice(0, 200)}</p>
                    </div>
                  ))}
                </div>
              </div>

              {/* Child 块 */}
              <div>
                <h3 className="text-sm font-semibold text-slate-700 mb-2">子块（{result.children.length}）</h3>
                <div className="space-y-2 max-h-96 overflow-auto pr-1">
                  {result.children.map((c, i) => {
                    const pidx = typeof c.metadata.parent_index === "number" ? c.metadata.parent_index : null;
                    return (
                      <div
                        key={i}
                        onMouseEnter={() => pidx !== null && setHoverParent(pidx)}
                        onMouseLeave={() => setHoverParent(null)}
                        className={`p-2 rounded border text-xs transition-colors ${
                          pidx === hoverParent && hoverParent !== null
                            ? "border-brand-400 bg-brand-50"
                            : "border-slate-200 bg-white"
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1">
                          <div className="flex items-center gap-1">
                            <Badge color="slate">child[{i}]</Badge>
                            {pidx !== null && <Badge color="amber">→ parent[{pidx}]</Badge>}
                          </div>
                          <span className="text-slate-400">{c.content.length} 字</span>
                        </div>
                        <p className="text-slate-600 line-clamp-3 font-mono">{c.content.slice(0, 150)}</p>
                      </div>
                    );
                  })}
                </div>
              </div>
            </>
          )}

          {!result && !loading && !error && (
            <Card className="p-8 text-center text-sm text-slate-400">输入文本后自动预览切分结果</Card>
          )}
        </div>
      </div>
    </div>
  );
}

function Slider({ label, value, min, max, step, onChange }: {
  label: string; value: number; min: number; max: number; step: number; onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="flex justify-between text-xs text-slate-600 mb-1">
        <span>{label}</span>
        <span className="font-mono text-brand-700">{value}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full accent-brand-600"
      />
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div>
      <div className="text-2xl font-bold text-brand-700">{value}</div>
      <div className="text-xs text-slate-400">{label}</div>
    </div>
  );
}
