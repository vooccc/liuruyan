import { useUpdaterContext } from '../context/UpdaterContext'

export function TopBarUpdateBadge() {
  const { updateAvailable, updateInfo, openModal } = useUpdaterContext()

  if (!updateAvailable || !updateInfo) {
    return null
  }

  return (
    <button
      type="button"
      className="top-bar-update-badge"
      onClick={(e) => {
        e.stopPropagation()
        openModal()
      }}
      title={`发现新版本 v${updateInfo.version}，点击查看更新详情与升级`}
      data-no-drag
    >
      <span className="top-bar-update-text">更新</span>
    </button>
  )
}

export default TopBarUpdateBadge
