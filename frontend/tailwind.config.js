/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    container: { center: true, padding: "1.5rem" },
    extend: {
      colors: {
        bg:       "rgb(var(--bg) / <alpha-value>)",
        surface:  "rgb(var(--surface) / <alpha-value>)",
        muted:    "rgb(var(--muted) / <alpha-value>)",
        border:   "rgb(var(--border) / <alpha-value>)",
        fg:       "rgb(var(--fg) / <alpha-value>)",
        fgmuted:  "rgb(var(--fg-muted) / <alpha-value>)",
        primary:  "rgb(var(--primary) / <alpha-value>)",
        primaryfg:"rgb(var(--primary-fg) / <alpha-value>)",
        danger:   "rgb(var(--danger) / <alpha-value>)",
        success:  "rgb(var(--success) / <alpha-value>)",
        warning:  "rgb(var(--warning) / <alpha-value>)",
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SF Mono', 'Menlo', 'monospace'],
      },
      boxShadow: {
        card: "0 1px 0 rgba(0,0,0,.04), 0 0 0 1px rgb(var(--border) / 1)",
      },
    },
  },
  plugins: [],
};
