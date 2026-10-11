/**
 * Provider 自定义头像的 IndexedDB 存储（纯前端，后端不感知）。
 *
 * 为什么用 IndexedDB 而不是 localStorage：
 * - 图片 base64 可能 50-500KB/张，13 个 provider 累计轻松几 MB；
 *   localStorage 单域名配额 5MB 很容易爆。
 * - IndexedDB 异步，不阻塞主线程。
 *
 * 为什么不存在后端 JSON：
 * - models.json 频繁读写（每次切 provider / 填 key 都 save），把 base64
 *   图片嵌进去会让 I/O 变重，且 git diff 不友好。
 * - 图标是纯 UI 状态，没必要同步到服务端。
 *
 * API：
 *   await providerLogoStorage.set(providerId, base64)
 *   const src = await providerLogoStorage.get(providerId)  // null if none
 *   await providerLogoStorage.remove(providerId)
 */

const DB_NAME = 'liuruyan_provider_logos'
const STORE_NAME = 'logos'
const DB_VERSION = 1

let dbPromise: Promise<IDBDatabase> | null = null

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME)
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  return dbPromise
}

async function withStore<T>(
  mode: IDBTransactionMode,
  fn: (store: IDBObjectStore) => IDBRequest<T> | void,
): Promise<T | null> {
  try {
    const db = await openDb()
    return await new Promise<T | null>((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, mode)
      const store = tx.objectStore(STORE_NAME)
      const req = fn(store)
      if (!req) {
        tx.oncomplete = () => resolve(null)
        tx.onerror = () => reject(tx.error)
        return
      }
      req.onsuccess = () => resolve(req.result ?? null)
      req.onerror = () => reject(req.error)
    })
  } catch (err) {
    // SSR / 测试 / 隐私模式可能没 IndexedDB，降级为 null
    console.warn('[providerLogoStorage]', err)
    return null
  }
}

export interface ProviderLogoStorage {
  get(providerId: string): Promise<string | null>
  set(providerId: string, base64: string): Promise<void>
  remove(providerId: string): Promise<void>
  getAll(): Promise<Record<string, string>>
}

export const providerLogoStorage: ProviderLogoStorage = {
  async get(providerId) {
    const value = await withStore<string>('readonly', (store) => store.get(providerId))
    return value ?? null
  },

  async set(providerId, base64) {
    await withStore('readwrite', (store) => {
      store.put(base64, providerId)
    })
  },

  async remove(providerId) {
    await withStore('readwrite', (store) => {
      store.delete(providerId)
    })
  },

  async getAll() {
    const out: Record<string, string> = {}
    try {
      const db = await openDb()
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, 'readonly')
        const store = tx.objectStore(STORE_NAME)
        const req = store.openCursor()
        req.onsuccess = () => {
          const cursor = req.result
          if (cursor) {
            if (typeof cursor.value === 'string') {
              out[String(cursor.key)] = cursor.value
            }
            cursor.continue()
          } else {
            resolve()
          }
        }
        req.onerror = () => reject(req.error)
      })
    } catch (err) {
      console.warn('[providerLogoStorage.getAll]', err)
    }
    return out
  },
}

/**
 * 压缩图片到指定最大边长 + JPEG 质量，返回 base64 data URL。
 * 对齐 cherry 的 compressImage（maxWidthOrHeight=300, maxSizeMB=1）但纯前端实现。
 */
export async function compressImageToBase64(
  file: File,
  options: { maxSize?: number; quality?: number } = {},
): Promise<string> {
  const { maxSize = 256, quality = 0.85 } = options

  // GIF 保留动图直接 base64
  if (file.type === 'image/gif') {
    return await fileToBase64(file)
  }

  const dataUrl = await fileToBase64(file)
  const img = await loadImage(dataUrl)

  const { width, height } = img
  const scale = Math.min(1, maxSize / Math.max(width, height))
  const targetW = Math.round(width * scale)
  const targetH = Math.round(height * scale)

  const canvas = document.createElement('canvas')
  canvas.width = targetW
  canvas.height = targetH
  const ctx = canvas.getContext('2d')
  if (!ctx) return dataUrl
  ctx.drawImage(img, 0, 0, targetW, targetH)

  // 透明通道保留用 PNG，否则 JPEG（更小）
  const hasAlpha = file.type === 'image/png' || file.type === 'image/webp'
  return canvas.toDataURL(hasAlpha ? 'image/png' : 'image/jpeg', quality)
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('image load failed'))
    img.src = src
  })
}
