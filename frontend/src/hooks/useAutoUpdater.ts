import { useState, useEffect, useCallback } from 'react'

export interface UpdateInfo {
  version: string
  currentVersion: string
  body?: string
  date?: string
}

export function useAutoUpdater() {
  const [checking, setChecking] = useState(false)
  const [updateAvailable, setUpdateAvailable] = useState(false)
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadProgress, setDownloadProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [updateHandle, setUpdateHandle] = useState<any>(null)

  // 检查更新
  const checkForUpdates = useCallback(async (manual = false) => {
    try {
      setChecking(true)
      setError(null)

      // 动态引入插件，确保在普通浏览器 dev 模式下不会崩溃
      const { check } = await import('@tauri-apps/plugin-updater')
      const update = await check()

      if (update?.available) {
        setUpdateAvailable(true)
        setUpdateInfo({
          version: update.version,
          currentVersion: update.currentVersion,
          body: update.body,
          date: update.date,
        })
        setUpdateHandle(update)
      } else {
        setUpdateAvailable(false)
        setUpdateInfo(null)
        setUpdateHandle(null)
        if (manual) {
          setError('当前已是最新版本')
        }
      }
    } catch (err: any) {
      console.warn('[AutoUpdater] Check failed:', err)
      if (manual) {
        setError(err?.message || '检查更新未成功，可能当前运行在浏览器环境或网络受限')
      }
    } finally {
      setChecking(false)
    }
  }, [])

  // 开始下载并安装
  const startUpdate = useCallback(async () => {
    if (!updateHandle) return
    try {
      setDownloading(true)
      setError(null)
      let downloaded = 0
      let total = 0

      await updateHandle.downloadAndInstall((event: any) => {
        if (event.event === 'Started') {
          total = event.data?.contentLength || 0
        } else if (event.event === 'Progress') {
          downloaded += event.data?.chunkLength || 0
          if (total > 0) {
            setDownloadProgress(Math.min(100, Math.round((downloaded / total) * 100)))
          }
        } else if (event.event === 'Finished') {
          setDownloadProgress(100)
        }
      })

      // 安装完成，调用 relaunch 重启
      const { relaunch } = await import('@tauri-apps/plugin-process')
      await relaunch()
    } catch (err: any) {
      console.error('[AutoUpdater] Download & Install failed:', err)
      setError(`自动更新受阻: ${err?.message || err}。您可以点击“手动下载”前往 GitHub 获取最新安装包。`)
    } finally {
      setDownloading(false)
    }
  }, [updateHandle])

  // 浏览器手动下载降级方案
  const openManualDownload = useCallback(async () => {
    const url = 'https://github.com/vooccc/liuruyan/releases/latest'
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      await invoke('open_external', { url })
    } catch {
      window.open(url, '_blank')
    }
  }, [])

  // 挂载后延迟 3 秒静默检查一次
  useEffect(() => {
    const timer = setTimeout(() => {
      checkForUpdates(false)
    }, 3000)
    return () => clearTimeout(timer)
  }, [checkForUpdates])

  return {
    checking,
    updateAvailable,
    updateInfo,
    downloading,
    downloadProgress,
    error,
    checkForUpdates,
    startUpdate,
    openManualDownload,
  }
}
