import { useState } from "react";

// 轻量 UI 组件（不引组件库，Tailwind 手写）

export function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`bg-white rounded-lg border border-slate-200 shadow-sm ${className}`}>
      {children}
    </div>
  );
}

export function Button({
  children,
  onClick,
  disabled,
  variant = "primary",
  className = "",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  className?: string;
}) {
  const variants = {
    primary: "bg-brand-600 text-white hover:bg-brand-700 disabled:bg-slate-300",
    secondary: "bg-slate-100 text-slate-700 hover:bg-slate-200 disabled:opacity-50",
    danger: "bg-red-50 text-red-600 hover:bg-red-100 border border-red-200",
    ghost: "text-slate-600 hover:bg-slate-100",
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`px-4 py-2 rounded-md text-sm font-medium transition-colors disabled:cursor-not-allowed ${variants[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

export function Badge({ children, color = "slate" }: { children: React.ReactNode; color?: "slate" | "red" | "green" | "amber" | "blue" }) {
  const colors = {
    slate: "bg-slate-100 text-slate-600",
    red: "bg-red-100 text-red-700",
    green: "bg-green-100 text-green-700",
    amber: "bg-amber-100 text-amber-700",
    blue: "bg-blue-100 text-blue-700",
  };
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${colors[color]}`}>
      {children}
    </span>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg className={`animate-spin h-4 w-4 ${className}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-md text-sm">
      {message}
    </div>
  );
}

export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="mb-6">
      <h2 className="text-xl font-bold text-slate-800">{title}</h2>
      {subtitle && <p className="text-sm text-slate-500 mt-1">{subtitle}</p>}
    </div>
  );
}

// 简单确认弹窗
export function useConfirm() {
  const [state, setState] = useState<{ msg: string; resolve: (v: boolean) => void } | null>(null);
  const confirm = (msg: string): Promise<boolean> =>
    new Promise((resolve) => setState({ msg, resolve }));
  const node = state ? (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => { state.resolve(false); setState(null); }}>
      <div className="bg-white rounded-lg p-6 max-w-sm w-full mx-4" onClick={(e) => e.stopPropagation()}>
        <p className="text-sm text-slate-700 mb-4">{state.msg}</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => { state.resolve(false); setState(null); }}>取消</Button>
          <Button variant="danger" onClick={() => { state.resolve(true); setState(null); }}>确认</Button>
        </div>
      </div>
    </div>
  ) : null;
  return { confirm, node };
}
