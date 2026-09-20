import { API_BASE } from '../api/base'

export interface AvatarLike {
  avatar?: string | null
  avatar_url?: string | null
}

const LOCAL_BACKEND_ORIGIN = API_BASE

// 兜底 cache buster：后端 _normalize_agent_avatar_payload / list_templates 会在
// avatar_url 里塞 ?v=mtime；但万一遇到老后端没重启 / 缓存的旧响应，URL 不带 query
// 时浏览器会命中本地图片缓存导致更换头像后不刷新（用户场景：Welcome 漂浮头像、
// Sidebar 角色卡等所有走 getAvatarSrc 的地方）。
// 取本次会话首次加载时间作客户端兜底版本号——同会话里 URL 字符串稳定，不会反复重拉；
// 整页刷新后会换新值。后端有 mtime 时优先用 mtime，不会双拼。
const SESSION_AVATAR_VERSION = Date.now()

function ensureCacheBuster(url: string): string {
  // 已经带 query 的（后端 mtime 版本）保持原样
  if (url.includes('?')) return url
  return `${url}?_sv=${SESSION_AVATAR_VERSION}`
}

export function isImageAvatar(avatar?: string | null): boolean {
  if (!avatar) return false
  return avatar.startsWith('data:') || avatar.startsWith('http') || avatar.startsWith('blob:')
}

export function getAvatarSrc(entity?: AvatarLike | null): string | null {
  if (!entity) return null

  if (entity.avatar_url) {
    // data:/http/blob: 这种内嵌图直接用，不需要 cache buster
    if (isImageAvatar(entity.avatar_url)) return entity.avatar_url
    if (entity.avatar_url.startsWith('/')) {
      return ensureCacheBuster(`${LOCAL_BACKEND_ORIGIN}${entity.avatar_url}`)
    }
    return ensureCacheBuster(entity.avatar_url)
  }

  if (isImageAvatar(entity.avatar)) return entity.avatar || null
  return null
}

export function getAvatarFallback(entity?: AvatarLike | null, fallbackAvatar: string = ''): string {
  if (!entity) return fallbackAvatar
  return getAvatarSrc(entity) ? '' : (entity.avatar || fallbackAvatar)
}
