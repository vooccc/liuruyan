import { spawn } from 'node:child_process'

const devUrl = process.env.TAURI_DEV_URL || 'http://localhost:1420'

async function isDevServerReady() {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 1500)
  try {
    const response = await fetch(devUrl, { signal: controller.signal })
    return response.ok
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

if (await isDevServerReady()) {
  console.log(`[tauri-before-dev] Reusing existing Vite dev server: ${devUrl}`)
  process.exit(0)
}

const viteBin = process.platform === 'win32' ? 'vite.cmd' : 'vite'
const child = spawn(viteBin, [], {
  stdio: 'inherit',
  shell: process.platform === 'win32',
})

child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal)
    return
  }
  process.exit(code ?? 0)
})
