import { lazy, Suspense, useEffect, useState } from 'react'
import {
  ArrowLeft,
  Brain,
  FolderOpen,
  Gauge,
  Hexagon,
  Monitor,
  Puzzle,
  Radio,
  RefreshCw,
  Server,
  ShieldCheck,
  Smartphone,
  Terminal,
  Wand2,
  Wrench,
} from 'lucide-react'
import CloudKey from '../icons/CloudKeyIcon'
import ToolInspectIcon from '../icons/ToolInspectIcon'
import ProviderSettings from './ProviderSettings'
import ChannelsSettings from './ChannelsSettings'
import PermissionsSettings from './PermissionsSettings'
import RuntimeBudgetSettings from './RuntimeBudgetSettings'
import MCPSettings from './mcp/MCPSettings'
import VoiceSettings from './VoiceSettings'
import EmbeddingSettings from './EmbeddingSettings'
import ToolsInventory from './ToolsInventory'
import AcpSettings from './AcpSettings'
import GatewaySettings from './GatewaySettings'
import RemoteAccessPanel from './RemoteAccessPanel'
import LryWorkspacePanel from './LryWorkspacePanel'
import ComputerUseSettings from './ComputerUseSettings'
import UpdateSettings from './UpdateSettings'
import { useResizableWidth, ResizeHandle } from '../useResizableWidth'

const SkillPanel = lazy(() => import('../SkillPanel'))
// 算力账号面板（动态懒加载；模块缺失时降级为空面板，不阻塞设置页）
const ComputeAccountsPanel: React.ComponentType = lazy(() =>
  import('../../pro/compute_accounts/ComputeAccountsPanel').catch(() => ({
    default: () => <div />,
  }))
)

export type SettingsTab =
  | 'providers'
  | 'compute_accounts'
  | 'channels'
  | 'permissions'
  | 'runtime_budget'
  | 'mcp'
  | 'acp'
  | 'gateway'
  | 'remote'
  | 'aux'
  | 'embedding'
  | 'tools'
  | 'skills'
  | 'computer_use'
  | 'lry'
  | 'updates'

interface TabDef {
  id: SettingsTab
  label: string
  icon: React.ReactNode
  description?: string
}

const ALL_TABS: TabDef[] = [
  {
    id: 'providers',
    label: '模型服务',
    icon: <Hexagon size={18} />,
    description: '按 provider 分组管理，支持一键发现免费模型',
  },
  {
    id: 'compute_accounts',
    label: '算力账号',
    icon: <Server size={18} />,
    description: '直接关联登录 Google / Codex / Grok 等官方账号，免 API Key 享受高额免费/订阅算力',
  },
  {
    id: 'gateway',
    label: '模型共享',
    icon: <CloudKey size={18} />,
    description: '把柳如烟变成本地 LLM 网关，让外部客户端（Cline / Cursor / Claude Code 等）共用柳如烟 provider 池',
  },
  {
    id: 'permissions',
    label: '权限',
    icon: <ShieldCheck size={18} />,
    description: '按角色配置文件 / 命令 / 后端代码权限',
  },
  {
    id: 'runtime_budget',
    label: '运行预算',
    icon: <Gauge size={18} />,
    description: '单次对话的工具次数 / 输出长度 / 时间上限（全局护栏）',
  },
  {
    id: 'tools',
    label: '工具',
    icon: <ToolInspectIcon size={18} />,
    description: '查看内置/MCP/IDE 工具的 description 与 schema，按角色配工具白名单',
  },
  {
    id: 'skills',
    label: '技能',
    icon: <Wrench size={18} />,
    description: '全局技能池 / 角色装备 / 经验晋升审批',
  },
  {
    id: 'mcp',
    label: 'MCP',
    icon: <Puzzle size={18} />,
    description: '通过 Model Context Protocol 接入第三方工具',
  },
  {
    id: 'acp',
    label: 'ACP',
    icon: <Terminal size={18} />,
    description: 'Agent Client Protocol — 召唤 Claude Code / Gemini CLI 等外部 ACP agent（从模型服务借 API key）',
  },
  {
    id: 'channels',
    label: '频道',
    icon: <Radio size={18} />,
    description: 'IM 接入与对外通道',
  },
  {
    id: 'remote',
    label: '远程访问',
    icon: <Smartphone size={18} />,
    description: 'M8a：让外部设备（手机 / 出差笔记本）通过 Tailscale 等私网访问完整柳如烟 UI + PWA 添加到主屏',
  },
  {
    id: 'computer_use',
    label: '电脑控制',
    icon: <Monitor size={18} />,
    description: '让 AI 截屏 / 点击 / 键入 — 总开关、动作黑名单、限流、立即停止',
  },
  {
    id: 'aux',
    label: '辅助功能',
    icon: <Wand2 size={18} />,
    description: '翻译 / 语音转写 / 语音输出（TTS）/ 搜索引擎 key 等跨场景小工具',
  },
  {
    id: 'embedding',
    label: '长期记忆',
    icon: <Brain size={18} />,
    description: '给 recall_memory 升级向量语义检索（embedding，可选）',
  },
  {
    id: 'lry',
    label: '工作区',
    icon: <FolderOpen size={18} />,
    description: '查看柳如烟在本项目积累的规则、工作流、记忆与笔记（.lry/）',
  },
  {
    id: 'updates',
    label: '版本更新',
    icon: <RefreshCw size={18} />,
    description: '检测新版本、在线更新与历史发布日志',
  },
]

const TABS: TabDef[] = ALL_TABS

interface SettingsViewProps {
  /** 进入设置时定位到哪个 tab；不传默认 models */
  initialTab?: SettingsTab
  /** permissions / skills tab 时定位到哪个 agent */
  focusAgentId?: string | null
  /** 退出设置回主聊天的回调（左侧底部「返回对话」按钮触发） */
  onCloseSettings?: () => void
}

/**
 * 设置主视图（替代 companions/roundtable 的 app-body 内容区）。
 *
 * 三段式：左 tab 导航 → tab 内部自带（providers 索引列 + 详情）。
 * 这里只负责 tab 切换与外壳；具体内容由各 tab 组件自管。
 */
export default function SettingsView({ initialTab, focusAgentId, onCloseSettings }: SettingsViewProps) {
  const [tab, setTab] = useState<SettingsTab>(() => {
    const raw = initialTab ?? 'providers'
    return raw
  })

  // 外部 initialTab 变化（用户从主聊天再次点权限时）→ 跟着切
  useEffect(() => {
    if (initialTab) {
      setTab(initialTab)
    }
  }, [initialTab])

  // 左侧分类栏可拖拽调宽
  const { width: tabsWidth, handleProps: tabsResizeProps } = useResizableWidth({
    storageKey: 'lr:settings-tabs-width',
    defaultWidth: 220,
    min: 160,
    max: 360,
  })

  return (
    <div className="settings-view">
      {/* ======= 左侧：设置分类 tab ======= */}
      <aside
        className="settings-tabs"
        aria-label="设置分类"
        style={{ ['--settings-tabs-width' as string]: `${tabsWidth}px` } as React.CSSProperties}
      >
        <nav className="settings-tabs-list">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`settings-tab-item ${tab === item.id ? 'active' : ''}`}
              onClick={() => setTab(item.id)}
            >
              <span className="settings-tab-icon">{item.icon}</span>
              <span className="settings-tab-label">{item.label}</span>
            </button>
          ))}
        </nav>
        {onCloseSettings ? (
          <div className="settings-tabs-footer-bar">
            <button
              type="button"
              className="settings-tabs-back-btn"
              onClick={onCloseSettings}
              title="返回 (Ctrl+,)"
            >
              <ArrowLeft size={16} />
              <span>返回</span>
            </button>
          </div>
        ) : (
          <div className="settings-tabs-footer-bar" aria-hidden="true" />
        )}
        <ResizeHandle {...tabsResizeProps} />
      </aside>

      {/* ======= 右侧：当前 tab 内容 ======= */}
      <section
        className={`settings-tab-content ${tab === 'compute_accounts' ? 'tab-compute-accounts' : ''}`}
        aria-label="设置内容"
      >
        {tab === 'providers' && <ProviderSettings />}
        {tab === 'compute_accounts' && (
          <Suspense fallback={<div className="empty-state">算力账号面板载入中…</div>}>
            <ComputeAccountsPanel />
          </Suspense>
        )}
        {tab === 'channels' && <ChannelsSettings />}
        {tab === 'permissions' && <PermissionsSettings focusAgentId={focusAgentId} />}
        {tab === 'runtime_budget' && <RuntimeBudgetSettings />}
        {tab === 'mcp' && <MCPSettings />}
        {tab === 'acp' && <AcpSettings onJumpToProviders={() => setTab('providers')} />}
        {tab === 'gateway' && <GatewaySettings />}
        {tab === 'remote' && <RemoteAccessPanel />}
        {tab === 'aux' && <VoiceSettings />}
        {tab === 'embedding' && <EmbeddingSettings />}
        {tab === 'tools' && <ToolsInventory />}
        {tab === 'computer_use' && <ComputerUseSettings />}
        {tab === 'skills' && (
          <Suspense fallback={<div className="empty-state">技能面板载入中…</div>}>
            <SkillPanel currentAgentId={focusAgentId ?? undefined} />
          </Suspense>
        )}
        {tab === 'lry' && <LryWorkspacePanel />}
        {tab === 'updates' && <UpdateSettings />}
      </section>
    </div>
  )
}
