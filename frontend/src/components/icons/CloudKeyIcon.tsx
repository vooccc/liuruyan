/**
 * ModelShareIcon / CloudKey — 自研高阶图标，用于「模型共享 (LLM Gateway)」入口。
 *
 * 语义：柳如烟作为本地算力网关，将聚合的模型提供池向外部兼容编辑器与应用共享分发。
 * 设计：提供 4 种现代科技美学方案（默认 variant="neural-share" 为方案 A 智核分发）。
 *
 * API 与 lucide-react 图标完全对齐：<CloudKey size={18} /> 即可使用。
 */
import type { SVGProps } from 'react'

export type ModelShareIconVariant =
  | 'neural-share'   // 方案 A：智核分发（AI星芒中枢向外辐射共享节点，极力推荐）
  | 'gateway-portal'  // 方案 B：算力星门（科技通道拱门 + 悬浮模型星芒 + 底部总线）
  | 'cloud-mesh'      // 方案 C：智云总线（超薄流线型科技云穹顶 + 三路多端端口）
  | 'model-matrix'    // 方案 D：超维矩阵（等轴测立体模型晶体 + 分支数据流）
  | 'legacy'          // 原版：卡通云+钥匙

export interface CloudKeyProps extends Omit<SVGProps<SVGSVGElement>, 'width' | 'height'> {
  size?: number | string
  variant?: ModelShareIconVariant
}

export default function CloudKey({
  size = 24,
  variant = 'neural-share',
  strokeWidth = 2,
  ...rest
}: CloudKeyProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {variant === 'neural-share' && (
        <>
          {/* 智算中枢核心（左侧星核节点） */}
          <circle cx="6.5" cy="12" r="3.5" />
          <path d="M6.5 10v4M4.5 12h4" strokeWidth={Math.max(1.4, Number(strokeWidth) * 0.85)} />
          {/* 外部客户端接入节点（右侧终端） */}
          <circle cx="17.5" cy="6.5" r="2.5" />
          <circle cx="17.5" cy="17.5" r="2.5" />
          {/* 共享分发拓扑总线 */}
          <path d="M9.8 10.5l5.2-2.8" />
          <path d="M9.8 13.5l5.2 2.8" />
        </>
      )}

      {variant === 'gateway-portal' && (
        <>
          {/* 网关通道门 */}
          <path d="M4 14V7a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v7" />
          {/* 门心悬浮 AI 星芒 */}
          <path d="M12 7l1 2.2 2.2 1-2.2 1-1 2.2-1-2.2-2.2-1 2.2-1z" />
          {/* 底部贯通链路与接口 */}
          <path d="M2 19h20" />
          <path d="M12 14.4V19" />
          <circle cx="6" cy="19" r="1.5" />
          <circle cx="18" cy="19" r="1.5" />
        </>
      )}

      {variant === 'cloud-mesh' && (
        <>
          {/* 流线型超薄科技云弧 */}
          <path d="M17.5 13.5a3.8 3.8 0 0 0 .5-7.5 4.8 4.8 0 0 0-9.2-1.2A4 4 0 0 0 5 12" />
          {/* 云心核心微星 */}
          <path d="M12 7.5l.6 1.4 1.4.6-1.4.6-.6 1.4-.6-1.4-1.4-.6 1.4-.6z" />
          {/* 底部多端分发总线与端口 */}
          <path d="M5 14h14" />
          <path d="M8 14v4M12 14v4M16 14v4" />
          <circle cx="8" cy="19.5" r="1.5" />
          <circle cx="12" cy="19.5" r="1.5" />
          <circle cx="16" cy="19.5" r="1.5" />
        </>
      )}

      {variant === 'model-matrix' && (
        <>
          {/* 3D 算力模型核心 */}
          <path d="M12 2.5l7 4v7l-7 4-7-4v-7z" />
          <path d="M12 2.5v8M12 10.5l7 4M12 10.5l-7 4" />
          {/* 底部互联发射端点 */}
          <path d="M7 18.5l-3 3M17 18.5l3 3" />
          <circle cx="3" cy="22" r="1.2" />
          <circle cx="21" cy="22" r="1.2" />
        </>
      )}

      {variant === 'legacy' && (
        <>
          <path d="M17.5 18a4 4 0 0 0 -0.7 -7.95 A5.5 5.5 0 0 0 6 9 A4 4 0 0 0 5.5 17 H 17.5 Z" />
          <circle cx="9.5" cy="13.5" r="1.9" />
          <path d="M11.4 13.5 H 16" />
          <path d="M14.2 13.5 V 15.2" />
        </>
      )}
    </svg>
  )
}
