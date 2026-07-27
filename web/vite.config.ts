/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

const apiTarget = `http://localhost:${process.env.OWLERY_API_PORT || '8000'}`

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      // Every specimen keeps a directly shareable HTML entry, while the entries
      // mount one cabinet runtime so internal navigation never reloads the page.
      input: {
        app: fileURLToPath(new URL('./index.html', import.meta.url)),
        functionCabinet: fileURLToPath(
          new URL('./function-cabinet.html', import.meta.url)
        ),
        streamingAnatomy: fileURLToPath(
          new URL('./streaming-anatomy.html', import.meta.url)
        ),
        agentDelegation: fileURLToPath(
          new URL('./agent-delegation.html', import.meta.url)
        ),
        bgTaskPipeline: fileURLToPath(
          new URL('./bg-task-pipeline.html', import.meta.url)
        ),
        deepResearch: fileURLToPath(
          new URL('./deep-research.html', import.meta.url)
        ),
        sessionForkRewind: fileURLToPath(
          new URL('./session-fork-rewind.html', import.meta.url)
        ),
        agentMemory: fileURLToPath(
          new URL('./agent-memory.html', import.meta.url)
        ),
        harnessRecovery: fileURLToPath(
          new URL('./harness-recovery.html', import.meta.url)
        ),
        automationPipeline: fileURLToPath(
          new URL('./automation-pipeline.html', import.meta.url)
        ),
      },
    },
  },
  server: {
    proxy: {
      '/api': apiTarget,
      '/ws': { target: apiTarget, ws: true },
      '/health': apiTarget,
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test-setup.ts',
    exclude: ['e2e/**', 'node_modules/**'],
  },
})
