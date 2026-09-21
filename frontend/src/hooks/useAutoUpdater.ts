import { useState, useEffect, useCallback } from 'react'
import { API_BASE } from '../api/base'

export interface UpdateInfo {
  version: string
  currentVersion: string
  body?: string
  date?: string
  downloadUrl?: string
  htmlUrl?: string
}

function isNewerVersion(remoteVer: string, localVer: string): boolean {
  const parse = (v: string) =>
    v.replace(/^[^\d]*/, '').split('.').map((p) => parseInt(p, 10) || 0)
  const r = parse(remoteVer)
  const l = parse(localVer)
  for (let i = 0; i < Math.max(r.length, l.length); i++) {
    const rv = r[i] || 0
    const lv = l[i] || 0
    if (rv > lv) return true
    if (rv < lv) return false
  }
  return false
}

export function useAutoUpdater() {
  const [checking, setChecking] = useState(false)
  const [updateAvailable, setUpdateAvailable] = useState(false)
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [downloadProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [currentInstalledVersion, setCurrentInstalledVersion] = useState<string>('2.0.18')

  // 初始化读取本地真实安装版本
  useEffect(() => {
    async function loadVersion() {
      try {
        const { getVersion } = await import('@tauri-apps/api/app')
        const v = await getVersion()
        if (v) setCurrentInstalledVersion(v)
      } catch {
        // 普通浏览器或 dev 模式下降级
      }
    }
    loadVersion()
  }, [])

  // 检查更新（优先走后端网络加速代理，兼具直连 GitHub Releases 降级）
  const checkForUpdates = useCallback(
    async (manual = false) => {
      try {
        setChecking(true)
        setError(null)

        // 1. 优先通过后端本地加速代理检测（解决国内直连 GitHub API 经常连接重置/超时问题）
        try {
          const res = await fetch(`${API_BASE}/utility/check_update`, {
            signal: AbortSignal.timeout(8000),
          })
          if (res.ok) {
            const data = await res.json()
            if (data.success && data.latest_version) {
              const hasNew = isNewerVersion(data.latest_version, currentInstalledVersion)
              if (hasNew) {
                setUpdateAvailable(true)
                setUpdateInfo({
                  version: data.latest_version,
                  currentVersion: currentInstalledVersion,
                  body: data.release_notes || '',
                  date: data.published_at || '',
                  downloadUrl: data.download_url || data.html_url,
                  htmlUrl: data.html_url || 'https://github.com/vooccc/liuruyan/releases',
                })
              } else {
                setUpdateAvailable(false)
                setUpdateInfo(null)
                if (manual) {
                  setError(`当前已是最新版本 (v${currentInstalledVersion})`)
                }
              }
              return
            } else if (data.message) {
              if (manual) {
                setError(data.message)
              }
              return
            }
          }
        } catch (backendErr) {
          console.warn('[AutoUpdater] 后端代理检测跳过，尝试直连 GitHub Releases:', backendErr)
        }

        // 2. 前端直连 GitHub Releases API 降级检测
        try {
          const ghRes = await fetch('https://api.github.com/repos/vooccc/liuruyan/releases/latest', {
            headers: { Accept: 'application/vnd.github.v3+json' },
            signal: AbortSignal.timeout(8000),
          })
          if (ghRes.ok) {
            const data = await ghRes.json()
            const latestVer = data.tag_name ? data.tag_name.replace(/^[^\d]*/, '') : ''
            if (latestVer) {
              const hasNew = isNewerVersion(latestVer, currentInstalledVersion)
              const exeAsset = data.assets?.find((a: any) =>
                a.name && typeof a.name === 'string' && a.name.endsWith('.exe')
              )
              const downloadUrl = exeAsset?.browser_download_url || data.html_url

              if (hasNew) {
                setUpdateAvailable(true)
                setUpdateInfo({
                  version: latestVer,
                  currentVersion: currentInstalledVersion,
                  body: data.body || '',
                  date: data.published_at || '',
                  downloadUrl,
                  htmlUrl: data.html_url || 'https://github.com/vooccc/liuruyan/releases',
                })
              } else {
                setUpdateAvailable(false)
                setUpdateInfo(null)
                if (manual) {
                  setError(`当前已是最新版本 (v${currentInstalledVersion})`)
                }
              }
              return
            }
          } else if (ghRes.status === 404) {
            if (manual) {
              setError('未在 GitHub 检索到公开发布的 Release。')
            }
            return
          }
        } catch (ghErr: any) {
          console.warn('[AutoUpdater] 直连 GitHub API 失败:', ghErr)
          if (manual) {
            setError('网络连接受限，无法连接至 GitHub Releases。您可以点击下方「手动下载」直接访问。')
          }
        }
      } finally {
        setChecking(false)
      }
    },
    [currentInstalledVersion]
  )

  // 启动下载并升级
  const startUpdate = useCallback(async () => {
    const targetUrl =
      updateInfo?.downloadUrl || updateInfo?.htmlUrl || 'https://github.com/vooccc/liuruyan/releases/latest'
    try {
      setDownloading(true)
      window.open(targetUrl, '_blank')
    } catch {
      window.location.href = targetUrl
    } finally {
      setTimeout(() => setDownloading(false), 2000)
    }
  }, [updateInfo])

  // 手动前往 Releases 页面
  const openManualDownload = useCallback(async () => {
    const url = 'https://github.com/vooccc/liuruyan/releases'
    window.open(url, '_blank')
  }, [])

  // 挂载后延迟 3 秒静默执行一次自动检测
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
    currentInstalledVersion,
    checkForUpdates,
    startUpdate,
    openManualDownload,
  }
}
