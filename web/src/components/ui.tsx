// 通用 UI 组件（轻量自绘，配合 Tailwind）
import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Card({ title, children, className = "" }: { title?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-slate-200/70 bg-white shadow-card ${className}`}>
      {title && (
        <div className="border-b border-slate-100 px-4 py-3 text-[13px] font-semibold text-slate-700">{title}</div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "default" | "danger" | "ghost";
  loading?: boolean;
};

export function Button({ variant = "default", loading, children, disabled, className = "", ...rest }: ButtonProps) {
  const styles = {
    primary: "bg-indigo-600 text-white shadow-sm hover:bg-indigo-500 active:bg-indigo-700 disabled:bg-indigo-300",
    default: "bg-white text-slate-700 border border-slate-300 hover:border-slate-400 hover:bg-slate-50",
    danger: "bg-white text-red-600 border border-red-200 hover:border-red-300 hover:bg-red-50",
    ghost: "text-slate-500 hover:bg-slate-100 hover:text-slate-700",
  }[variant];
  return (
    <button
      className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${styles} ${className}`}
      disabled={disabled || loading}
      {...rest}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

export function Spinner({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <span className={`inline-block animate-spin rounded-full border-2 border-current border-t-transparent ${className}`} />
  );
}

const BADGE_TONES = {
  slate: "bg-slate-100 text-slate-600 border-slate-200",
  green: "bg-emerald-50 text-emerald-700 border-emerald-200",
  red: "bg-red-50 text-red-700 border-red-200",
  blue: "bg-indigo-50 text-indigo-700 border-indigo-200",
  amber: "bg-amber-50 text-amber-700 border-amber-200",
} as const;

export function Badge({ tone = "slate", children }: { tone?: keyof typeof BADGE_TONES; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-px font-mono text-[11px] leading-4 ${BADGE_TONES[tone]}`}>
      {children}
    </span>
  );
}

export function EmptyState({ text, hint }: { text: string; hint?: string }) {
  return (
    <div className="py-12 text-center">
      <div className="mx-auto mb-3 h-8 w-8 rounded-full border border-dashed border-slate-300" />
      <p className="text-sm text-slate-400">{text}</p>
      {hint && <p className="mt-1 text-xs text-slate-300">{hint}</p>}
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 font-mono text-[13px] text-red-700">
      {message}
    </div>
  );
}
