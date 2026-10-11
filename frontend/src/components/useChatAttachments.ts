import { useCallback, useEffect, useRef, useState } from 'react'
import { uploadChatFile, type ChatUploadResult } from '../api/chat'

export type AttachmentStatus = 'uploading' | 'ok' | 'failed'

export interface ChatAttachment {
  /** 客户端临时 ID（用作 React key） */
  id: string
  /** 用户选时原始文件名 */
  name: string
  /** 字节数 */
  size: number
  /** MIME type（File.type，可为空） */
  mime?: string
  /** 后端落盘后的绝对路径（仅 ok 状态有） */
  path?: string
  /** 状态：uploading 上传中 / ok 已落盘 / failed 上传失败或被拒 */
  status: AttachmentStatus
  /** 失败时错误 message（含上传失败和 validateFile 拒收） */
  error?: string
  /** 是否为图片（含粘贴截图）。决定 chip 渲染为缩略图卡片还是文本 chip。 */
  isImage?: boolean
  /** 本地预览 URL（URL.createObjectURL），仅 isImage=true 时存在。remove/clear 时 revoke。 */
  thumbUrl?: string
}

function looksLikeImage(file: File): boolean {
  if (file.type && /^image\//i.test(file.type)) return true
  return /\.(png|jpe?g|gif|webp|bmp|svg|ico|tiff?|heic|heif)$/i.test(file.name)
}

export interface UseChatAttachmentsOptions {
  /**
   * 选文件时的前置验证。返 null / undefined 表示通过，返字符串表示拒收且把字符串作为错误消息。
   *
   * 典型用法：非 vision 模型拒收 image 文件，避免把无用图片送去后端（后端有兜底降级，
   * 但前端提早提示用户体验更好）。
   */
  validateFile?: (file: File) => string | null | undefined
}

/**
 * 管理 chat 输入框附件状态：选文件 → 前置验证 → 并发上传 → 失败/成功状态机 → 清空。
 *
 * 返回的 attachmentsRef 给 useChatSubmitFlow 用：只取已上传成功的 path 数组。
 * ref 模式避免 attachments state 改变触发 handleSubmit useCallback 重建。
 */
export function useChatAttachments(opts?: UseChatAttachmentsOptions) {
  const [attachments, setAttachments] = useState<ChatAttachment[]>([])
  const attachmentsRef = useRef<string[]>([])
  // 用 ref 存 validateFile，避免 opts 每帧 new 导致 handleChooseFiles 重建
  const validateFileRef = useRef(opts?.validateFile)
  useEffect(() => {
    validateFileRef.current = opts?.validateFile
  }, [opts?.validateFile])

  useEffect(() => {
    attachmentsRef.current = attachments
      .filter(a => a.status === 'ok' && a.path)
      .map(a => a.path as string)
  }, [attachments])

  const handleChooseFiles = useCallback(async (files: FileList | File[] | null) => {
    if (!files || files.length === 0) return
    const fileArray = Array.from(files)

    // 第一步：validateFile 前置检查。拒收的文件直接以 failed chip 落地，不上传。
    const toUpload: File[] = []
    const newChips: ChatAttachment[] = []
    const uploadIndexMap: number[] = []  // 在 newChips 里的索引 → toUpload 里的索引
    for (const f of fileArray) {
      const err = validateFileRef.current?.(f)
      const baseId = `att_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
      const isImg = looksLikeImage(f)
      // 即使后续 validateFile 拒收，也生成缩略图，让用户看到自己粘的是哪张
      // （比如非 vision 模型拒图时，用户能立即看到"哦这张图发不了"而非看到一行文字 chip 不知所云）
      const thumbUrl = isImg ? URL.createObjectURL(f) : undefined
      if (err) {
        newChips.push({
          id: baseId,
          name: f.name,
          size: f.size,
          mime: f.type || undefined,
          status: 'failed' as const,
          error: err,
          isImage: isImg,
          thumbUrl,
        })
      } else {
        uploadIndexMap.push(newChips.length)
        newChips.push({
          id: baseId,
          name: f.name,
          size: f.size,
          mime: f.type || undefined,
          status: 'uploading' as const,
          isImage: isImg,
          thumbUrl,
        })
        toUpload.push(f)
      }
    }
    setAttachments(prev => [...prev, ...newChips])

    // 第二步：并行上传未被拒的文件
    await Promise.all(toUpload.map(async (file, i) => {
      const placeholderId = newChips[uploadIndexMap[i]].id
      try {
        const result: ChatUploadResult = await uploadChatFile(file)
        setAttachments(prev => prev.map(a =>
          a.id === placeholderId
            ? { ...a, status: 'ok' as const, path: result.path }
            : a
        ))
      } catch (err: any) {
        const errorMsg = err?.message || String(err)
        setAttachments(prev => prev.map(a =>
          a.id === placeholderId
            ? { ...a, status: 'failed' as const, error: errorMsg }
            : a
        ))
      }
    }))
  }, [])

  const removeAttachment = useCallback((id: string) => {
    setAttachments(prev => {
      const target = prev.find(a => a.id === id)
      if (target?.thumbUrl) {
        try { URL.revokeObjectURL(target.thumbUrl) } catch { /* ignore */ }
      }
      return prev.filter(a => a.id !== id)
    })
  }, [])

  const clearAttachments = useCallback(() => {
    setAttachments(prev => {
      for (const a of prev) {
        if (a.thumbUrl) {
          try { URL.revokeObjectURL(a.thumbUrl) } catch { /* ignore */ }
        }
      }
      return []
    })
  }, [])

  // 卸载时清理所有遗留的 ObjectURL（防 React unmount 时内存泄漏）
  useEffect(() => {
    return () => {
      // 拿最新 attachments 用 ref 模式有点重；直接通过 setAttachments 的 callback 拿
      setAttachments(prev => {
        for (const a of prev) {
          if (a.thumbUrl) {
            try { URL.revokeObjectURL(a.thumbUrl) } catch { /* ignore */ }
          }
        }
        return prev
      })
    }
  }, [])

  return { attachments, attachmentsRef, handleChooseFiles, removeAttachment, clearAttachments }
}
