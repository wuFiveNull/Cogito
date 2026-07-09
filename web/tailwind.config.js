/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // 暖色调设计系统 —— 奶油底色 + 赤陶/琥珀强调色
        // 颜色统一用 rgb(r g b / <alpha-value>) 形式，以支持 /12、/15 等透明度修饰符
        cream: "rgb(251 243 234 / <alpha-value>)", // 页面背景（暖奶油）
        surface: "rgb(255 253 251 / <alpha-value>)", // 卡片/表面（近白暖）
        "surface-2": "rgb(251 237 224 / <alpha-value>)", // 次级表面（蜜桃奶油）
        "surface-3": "rgb(246 226 208 / <alpha-value>)", // 悬停/深蜜桃
        borderc: "rgb(235 217 197 / <alpha-value>)", // 暖沙色描边
        ink: "rgb(58 42 30 / <alpha-value>)", // 主文字（暖咖啡棕）
        muted: "rgb(154 123 102 / <alpha-value>)", // 次要文字（暖灰褐）
        primary: "rgb(224 123 57 / <alpha-value>)", // 主强调（南瓜橙）
        "primary-strong": "rgb(199 91 39 / <alpha-value>)", // 深橘（悬停/按下）
        accent: "rgb(217 164 65 / <alpha-value>)", // 琥珀金（候选/高亮）
        terracotta: "rgb(224 134 107 / <alpha-value>)", // 赤陶（次强调）
        ok: "rgb(94 140 79 / <alpha-value>)", // 成功（暖橄榄绿）
        warn: "rgb(217 138 43 / <alpha-value>)", // 警告（琥珀）
        info: "rgb(224 145 59 / <alpha-value>)", // 进行中（暖橙）
        danger: "rgb(192 73 47 / <alpha-value>)", // 失败（暖红）
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      boxShadow: {
        warm: "0 1px 2px rgba(122, 72, 38, 0.06), 0 8px 24px rgba(122, 72, 38, 0.08)",
        "warm-lg": "0 4px 12px rgba(122, 72, 38, 0.1), 0 16px 40px rgba(122, 72, 38, 0.12)",
      },
      opacity: {
        12: "0.12",
        15: "0.15",
      },
    },
  },
  plugins: [],
};
