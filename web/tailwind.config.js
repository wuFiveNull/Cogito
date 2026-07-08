/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: "#0b0f1a",
        surface: "#121829",
        "surface-2": "#1b2236",
        border: "#232c45",
        textc: "#e6e9f2",
        muted: "#8b93ad",
        accent: "#7c5cff",
        ok: "#22c55e",
        warn: "#f59e0b",
        info: "#38bdf8",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
    },
  },
  plugins: [],
};
