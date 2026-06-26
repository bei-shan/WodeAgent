/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#eff6ff', 100: '#dbeafe', 200: '#bfdbfe',
          300: '#93c5fd', 400: '#60a5fa', 500: '#4b80d9',
          600: '#3b6cc7', 700: '#2d5ab5', 800: '#1f4899',
          900: '#1a3a7a',
        },
        surface: {
          bg: '#ffffff',
          hover: '#f6f8fa',
          active: '#eef1f5',
          border: '#e8ecf1',
          dim: '#959da5',
          text: '#24292f',
          muted: '#656d76',
        },
      },
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
      borderRadius: {
        xl: '12px',
        '2xl': '16px',
        '3xl': '20px',
      },
      boxShadow: {
        'input': '0 2px 12px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04)',
        'input-focus': '0 2px 20px rgba(75,128,217,0.15), 0 1px 3px rgba(0,0,0,0.08)',
        'card': '0 1px 3px rgba(0,0,0,0.04)',
      },
    },
  },
  plugins: [],
}
