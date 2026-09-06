// 切片结果渲染：父块 → 子块 树（实际切片与参数预览共用）
// 父块=靛蓝（送入 LLM 的上下文），子块=琥珀（检索命中粒度）
import type { SplitFamily } from "../types";
import { Badge } from "./ui";

export default function FamilyList({ families }: { families: SplitFamily[] }) {
  return (
    <div className="space-y-4">
      {families.map((f, i) => (
        <div
          key={f.parent_id + i}
          className="overflow-hidden rounded-xl border border-slate-200/70 border-l-[3px] border-l-indigo-500 bg-white shadow-card"
        >
          <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50/60 px-4 py-2 text-xs text-slate-400">
            <Badge tone="blue">{f.parent_id || `p${i}`}</Badge>
            <span className="font-mono text-[11px]">父块 {f.parent_text.length} 字</span>
            <span className="ml-auto font-mono text-[11px] text-slate-400">
              {f.children.length} 子块
            </span>
          </div>
          <div className="px-4 py-3">
            <div className="rounded-lg bg-indigo-50/40 p-3 text-[13px] leading-relaxed text-slate-700">
              {f.parent_text}
            </div>
            <div className="mt-2.5 space-y-1.5">
              {f.children.map((c, j) => (
                <div key={j} className="flex items-stretch gap-2 text-xs">
                  <Badge tone="amber">c{j}</Badge>
                  <span className="flex-1 rounded-lg border-l-2 border-amber-400 bg-amber-50/50 p-2 leading-relaxed text-slate-600">
                    {c}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
