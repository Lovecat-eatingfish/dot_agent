/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#F3F4F8",
        // 墨蓝：侧边栏 / 深色面板
        ink: {
          DEFAULT: "#10152B",
          800: "#181F3A",
          700: "#242C50",
          600: "#313A63",
        },
      },
      fontFamily: {
        // 数据字体：ID、分数、块号、耗时统一等宽
        mono: ['"Cascadia Code"', '"JetBrains Mono"', 'ui-monospace', 'Consolas', '"Courier New"', 'monospace'],
      },
      boxShadow: {
        card: "0 1px 2px rgba(16, 21, 43, 0.05), 0 4px 16px rgba(16, 21, 43, 0.06)",
        pop: "0 4px 24px rgba(16, 21, 43, 0.14)",
      },
    },
  },
  plugins: [],
};
