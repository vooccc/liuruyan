/**
 * ToolInspectIcon — 自研高阶图标，用于「工具 (Tools Inventory & Schema)」入口。
 *
 * 语义：检视、诊断 Agent 挂载的内置工具、MCP 协议模组与 IDE 桥接工具及其参数 Schema。
 * 设计：提供 4 种现代科技美学方案（默认 variant="lens-core" 为方案 A 工具透镜）。
 *
 * API 与 lucide-react 图标完全对齐：<ToolInspectIcon size={18} /> 即可使用。
 */
import type { SVGProps } from 'react'

export type ToolInspectIconVariant =
  | 'lens-core'     // 方案 A：工具透镜（检视放大镜 + 镜内透视精密六角工具核心，极力推荐）
  | 'gear-scope'    // 方案 B：机件准星（系统底层运转齿轮 + 仪器诊断准星核心）
  | 'modular-grid'  // 方案 C：工具矩阵（模块化插槽与扫描对焦模组，契合 MCP）
  | 'schema-scope'  // 方案 D：架构取景（对焦框 + 代码函数参数 Schema 探针）
  | 'boxes'         // 原版：旧版堆叠纸箱

export interface ToolInspectIconProps extends Omit<SVGProps<SVGSVGElement>, 'width' | 'height'> {
  size?: number | string
  variant?: ToolInspectIconVariant
}

export default function ToolInspectIcon({
  size = 24,
  variant = 'lens-core',
  strokeWidth = 2,
  ...rest
}: ToolInspectIconProps) {
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
      {variant === 'lens-core' && (
        <>
          {/* 检视透镜环 */}
          <circle cx="10" cy="10" r="6.5" />
          <path d="M15 15l5.5 5.5" />
          {/* 镜内透视的精密六角工具核心零件 */}
          <path d="M10 7.2l2.4 1.4v2.8L10 12.8l-2.4-1.4v-2.8z" strokeWidth={Math.max(1.4, Number(strokeWidth) * 0.85)} />
          <circle cx="10" cy="10" r="0.8" fill="currentColor" />
        </>
      )}

      {variant === 'gear-scope' && (
        <>
          {/* 齿轮轮廓 */}
          <circle cx="12" cy="12" r="5.5" />
          <path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22" />
          <path d="m4.93 4.93 1.77 1.77M17.3 17.3l1.77 1.77M4.93 19.07l1.77-1.77M17.3 6.7l1.77-1.77" />
          {/* 准星核心 */}
          <circle cx="12" cy="12" r="2.2" />
        </>
      )}

      {variant === 'modular-grid' && (
        <>
          {/* 3 个固定工具插槽 */}
          <rect x="3.5" y="3.5" width="6.5" height="6.5" rx="1.5" />
          <rect x="3.5" y="14" width="6.5" height="6.5" rx="1.5" />
          <rect x="14" y="14" width="6.5" height="6.5" rx="1.5" />
          {/* 第 4 个被扫描装配的活动模块 */}
          <circle cx="17.2" cy="6.8" r="3.2" strokeDasharray="2 2" />
          <path d="M17.2 4.5v4.6M14.9 6.8h4.6" />
        </>
      )}

      {variant === 'schema-scope' && (
        <>
          {/* 检视对焦框 */}
          <path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M4 16v3a1 1 0 0 0 1 1h3M16 20h3a1 1 0 0 0 1-1v-3" />
          {/* Schema 代码符号与探针 */}
          <path d="m8.5 9.5-2.5 2.5 2.5 2.5M15.5 9.5l2.5 2.5-2.5 2.5" />
          <path d="m13 8-2 8" />
        </>
      )}

      {variant === 'boxes' && (
        <>
          <path d="M2.97 12.92A2 2 0 0 0 2 14.63v3.24a2 2 0 0 0 .97 1.71l3 1.8a2 2 0 0 0 2.06 0L12 19v-5.5l-5-3-4.03 2.42Z"/>
          <path d="M12 13.5V19l3.97 2.38a2 2 0 0 0 2.06 0l3-1.8a2 2 0 0 0 .97-1.71v-3.24a2 2 0 0 0-.97-1.71L17 10.5l-5 3Z"/>
          <path d="M7.97 4.42A2 2 0 0 0 7 6.13v4.37l5 3 5-3V6.13a2 2 0 0 0-.97-1.71l-3-1.8a2 2 0 0 0-2.06 0l-3 1.8Z"/>
        </>
      )}
    </svg>
  )
}
