import React, { createContext, useContext, useState, useCallback, useMemo } from 'react'
import { useAutoUpdater, type UpdateInfo } from '../hooks/useAutoUpdater'

interface UpdaterContextType {
  checking: boolean
  updateAvailable: boolean
  updateInfo: UpdateInfo | null
  downloading: boolean
  downloadProgress: number
  error: string | null
  currentInstalledVersion: string
  showModal: boolean
  isSimulated: boolean
  openModal: () => void
  closeModal: () => void
  checkForUpdates: (manual?: boolean) => Promise<void>
  startUpdate: () => Promise<void>
  openManualDownload: () => Promise<void>
  simulateUpdate: () => void
  clearSimulatedUpdate: () => void
}

const UpdaterContext = createContext<UpdaterContextType | null>(null)

export function UpdaterProvider({ children }: { children: React.ReactNode }) {
  const realUpdater = useAutoUpdater()
  const [showModal, setShowModal] = useState(false)
  const [simulatedUpdateInfo, setSimulatedUpdateInfo] = useState<UpdateInfo | null>(null)

  const isSimulated = simulatedUpdateInfo !== null
  const updateAvailable = isSimulated ? true : realUpdater.updateAvailable
  const updateInfo = isSimulated ? simulatedUpdateInfo : realUpdater.updateInfo

  const openModal = useCallback(() => {
    setShowModal(true)
  }, [])

  const closeModal = useCallback(() => {
    setShowModal(false)
  }, [])

  // 模拟新版本功能（便于开发与离线体验顶栏更新特性）
  const simulateUpdate = useCallback(() => {
    const cur = realUpdater.currentInstalledVersion || '2.0.18'
    const parts = cur.split('.').map((p) => parseInt(p, 10) || 0)
    const nextVer = `${parts[0] || 2}.${parts[1] || 0}.${(parts[2] || 18) + 1}`

    setSimulatedUpdateInfo({
      version: nextVer,
      currentVersion: cur,
      date: new Date().toISOString().split('T')[0],
      downloadUrl: 'https://github.com/vooccc/liuruyan/releases',
      htmlUrl: 'https://github.com/vooccc/liuruyan/releases',
      body: `### ✨ 柳如烟 v${nextVer} 新特性更新

- **⚡ 顶栏智能更新与胶囊徽标**：极速检测与免打扰提醒，一键查看最新版本内容；
- **🤖 算力插件模型精细管理**：算力账号模型按需启用，告别冗余模型干扰；
- **🎨 界面与交互全面精致化**：模态弹窗一体化圆角重构，头像网络降级自适应；
- **🚀 性能与长会话优化**：底层上下文缓存效率提升 35%，响应更丝滑。`,
    })
  }, [realUpdater.currentInstalledVersion])

  const clearSimulatedUpdate = useCallback(() => {
    setSimulatedUpdateInfo(null)
  }, [])

  const startUpdate = useCallback(async () => {
    if (isSimulated) {
      window.open('https://github.com/vooccc/liuruyan/releases', '_blank')
      return
    }
    return realUpdater.startUpdate()
  }, [isSimulated, realUpdater])

  const openManualDownload = useCallback(async () => {
    if (isSimulated) {
      window.open('https://github.com/vooccc/liuruyan/releases', '_blank')
      return
    }
    return realUpdater.openManualDownload()
  }, [isSimulated, realUpdater])

  const value = useMemo(
    () => ({
      checking: realUpdater.checking,
      updateAvailable,
      updateInfo,
      downloading: realUpdater.downloading,
      downloadProgress: realUpdater.downloadProgress,
      error: realUpdater.error,
      currentInstalledVersion: realUpdater.currentInstalledVersion,
      showModal,
      isSimulated,
      openModal,
      closeModal,
      checkForUpdates: realUpdater.checkForUpdates,
      startUpdate,
      openManualDownload,
      simulateUpdate,
      clearSimulatedUpdate,
    }),
    [
      realUpdater.checking,
      updateAvailable,
      updateInfo,
      realUpdater.downloading,
      realUpdater.downloadProgress,
      realUpdater.error,
      realUpdater.currentInstalledVersion,
      showModal,
      isSimulated,
      openModal,
      closeModal,
      realUpdater.checkForUpdates,
      startUpdate,
      openManualDownload,
      simulateUpdate,
      clearSimulatedUpdate,
    ]
  )

  return <UpdaterContext.Provider value={value}>{children}</UpdaterContext.Provider>
}

export function useUpdaterContext() {
  const ctx = useContext(UpdaterContext)
  if (!ctx) {
    throw new Error('useUpdaterContext must be used within an UpdaterProvider')
  }
  return ctx
}
