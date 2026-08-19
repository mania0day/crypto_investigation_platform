/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        vaultrix: {
          bg: '#001D39',
          card: '#052B50',
          cardHover: '#0A4174',
          cyan: '#7BBDE8',
          cyanHover: '#BDD8E9',
          text: '#DCEAF5',
          textMuted: '#6EA2B3',
          border: 'rgba(123, 189, 232, 0.20)',
        },
        crypto: {
          space: '#0a0a1a',
          cyan: '#00d4ff',
          blue: '#0088ff',
          orange: '#ff6b35',
          amber: '#ff9500',
          purple: '#1a0033',
          indigo: '#2d0047',
        }
      },
      fontFamily: {
        sans: ['Inter', 'Poppins', 'sans-serif'],
        display: ['Orbitron', 'sans-serif'],
        login: ['Poppins', 'Inter', 'sans-serif'],
      },
      backgroundImage: {
        'grid-pattern': "linear-gradient(to right, #7BBDE805 1px, transparent 1px), linear-gradient(to bottom, #7BBDE805 1px, transparent 1px)",
      },
      animation: {
        'pan-image': 'panImage 40s linear infinite alternate',
      },
      keyframes: {
        panImage: {
          '0%': { transform: 'scale(1.1) translate(0, 0)' },
          '100%': { transform: 'scale(1.1) translate(-2%, -2%)' },
        }
      }
    },
  },
  plugins: [],
}
