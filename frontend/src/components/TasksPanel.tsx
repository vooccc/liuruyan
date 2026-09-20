import { useEffect, useState } from 'react'
import { CheckSquare, RefreshCw, Trash2, X } from 'lucide-react'
import { API_BASE } from '../api/base'
import { startPolling } from '../lib/pollingLoop'
import { useConfirmDialog, useAlertDialog } from './ConfirmDialog'

interface Task {
  id: string;
  title: string;
  description: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'stale';
  created_at: number;
  started_at?: number | null;
  updated_at: number;
  metadata?: Record<string, any>;
  session_id?: string | null;
  agent_id?: string | null;
}

interface TasksPanelProps {
  sessionId?: string;
  onClose: () => void;
}

export default function TasksPanel({ sessionId, onClose }: TasksPanelProps) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<string>('all');
  // Tauri webview 不支持 window.confirm/alert
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog();
  const { alert: showAlert, dialog: alertDialog } = useAlertDialog();

  const fetchTasks = async () => {
    try {
      setLoading(true);
      const url = sessionId
        ? `${API_BASE}/tasks?session_id=${encodeURIComponent(sessionId)}`
        : `${API_BASE}/tasks`;
      const response = await fetch(url);
      const data = await response.json();

      if (data.success) {
        setTasks(data.tasks || []);
      }
    } catch (error) {
      console.error('Failed to fetch tasks:', error);
    } finally {
      setLoading(false);
    }
  };

  const deleteTask = async (taskId: string) => {
    try {
      const resp = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`, {
        method: 'DELETE',
      });
      if (resp.ok) {
        setTasks(prev => prev.filter(t => t.id !== taskId));
      } else {
        console.error('Failed to delete task:', resp.status);
      }
    } catch (error) {
      console.error('Failed to delete task:', error);
    }
  };

  const clearAll = async () => {
    if (!sessionId) {
      await showAlert('当前会话信息缺失，无法清空');
      return;
    }
    const ok = await askConfirm(
      '清空会话任务',
      '确认清空本会话的所有任务？此操作不可撤销。',
      'danger',
      '清空',
    );
    if (!ok) return;
    try {
      await fetch(
        `${API_BASE}/tasks/all?session_id=${encodeURIComponent(sessionId)}`,
        { method: 'DELETE' }
      );
      fetchTasks();
    } catch (error) {
      console.error('Failed to clear tasks:', error);
    }
  };

  useEffect(() => {
    return startPolling(fetchTasks, 3000);
  }, [sessionId]);

  const pendingTasks = tasks.filter(t => t.status === 'pending');
  const inProgressTasks = tasks.filter(t => t.status === 'in_progress');
  const completedTasks = tasks.filter(t => t.status === 'completed');
  const failedTasks = tasks.filter(t => t.status === 'failed' || t.status === 'stale');

  const filteredTasks = filter === 'all'
    ? tasks
    : filter === 'failed'
      ? tasks.filter(t => t.status === 'failed' || t.status === 'stale')
      : tasks.filter(t => t.status === filter);

  return (
    <div className="tool-stats-panel tasks-panel-elegant">
      {/* 顶部栏 */}
      <div className="stats-header">
        <h2>
          <CheckSquare size={18} strokeWidth={1.5} />
          <span style={{ fontWeight: 400, letterSpacing: '0.3px' }}>任务</span>
        </h2>
        <div className="stats-actions">
          <button onClick={() => fetchTasks()} disabled={loading} title="刷新">
            <RefreshCw size={15} strokeWidth={1.5} className={loading ? 'spinning' : ''} />
          </button>
          {tasks.length > 0 && (
            <button onClick={clearAll} title="全部清空">
              <Trash2 size={15} strokeWidth={1.5} />
            </button>
          )}
          <button onClick={onClose} className="close-btn" title="关闭">
            <X size={15} strokeWidth={1.5} />
          </button>
        </div>
      </div>

      {/* 内容区域 */}
      <div style={{
        flex: 1,
        overflowY: 'auto',
        padding: '20px'
      }}>
        {/* 统计卡片 */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(4, 1fr)',
          gap: '8px',
          marginBottom: '20px'
        }}>
          <StatCard
            label="待处理"
            count={pendingTasks.length}
            color="var(--text-muted)"
            active={filter === 'pending'}
            onClick={() => setFilter(filter === 'pending' ? 'all' : 'pending')}
          />
          <StatCard
            label="进行中"
            count={inProgressTasks.length}
            color="#3b82f6"
            active={filter === 'in_progress'}
            onClick={() => setFilter(filter === 'in_progress' ? 'all' : 'in_progress')}
          />
          <StatCard
            label="已完成"
            count={completedTasks.length}
            color="#10b981"
            active={filter === 'completed'}
            onClick={() => setFilter(filter === 'completed' ? 'all' : 'completed')}
          />
          <StatCard
            label="失败/归档"
            count={failedTasks.length}
            color="#ef4444"
            active={filter === 'failed'}
            onClick={() => setFilter(filter === 'failed' ? 'all' : 'failed')}
          />
        </div>

        {/* 任务列表 */}
        {filteredTasks.length === 0 ? (
          <div style={{
            textAlign: 'center',
            padding: '80px 20px',
            color: 'var(--text-muted)',
            fontSize: '14px',
            fontWeight: 300,
            letterSpacing: '0.3px'
          }}>
            {filter === 'all' ? '暂无任务' : `暂无${getFilterLabel(filter)}任务`}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {filteredTasks.map(task => (
              <TaskItem key={task.id} task={task} onDelete={deleteTask} />
            ))}
          </div>
        )}
      </div>

      <style>{`
        .tool-stats-panel {
          position: fixed;
          top: 60px;
          bottom: 160px;
          right: 20px;
          width: 420px;
          max-width: calc(100vw - 40px);
          background: var(--bg-color);
          border: 1px solid var(--border-color);
          border-radius: 8px;
          box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
          z-index: 1000;
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }

        .stats-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 16px 20px;
          border-bottom: 1px solid var(--border-color);
          flex-shrink: 0;
        }

        .stats-header h2 {
          display: flex;
          align-items: center;
          gap: 8px;
          margin: 0;
          font-size: 15px;
          font-weight: 400;
          color: var(--text-color);
        }

        .stats-actions {
          display: flex;
          gap: 8px;
        }

        .stats-actions button {
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 6px;
          background: transparent;
          border: 1px solid var(--border-color);
          border-radius: 4px;
          color: var(--text-secondary);
          cursor: pointer;
          transition: all 0.15s;
        }

        .stats-actions button:hover:not(:disabled) {
          background: var(--bg-secondary);
          color: var(--text-color);
          border-color: var(--text-tertiary);
        }

        .stats-actions button:disabled {
          opacity: 0.4;
          cursor: not-allowed;
        }

        .stats-actions button.close-btn:hover {
          border-color: rgba(248, 81, 73, 0.5);
          color: #f85149;
        }

        .spinning {
          animation: spin 1s linear infinite;
        }

        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }

        .tasks-panel-elegant .stats-header h2 {
          font-size: 15px;
          font-weight: 400;
          gap: 8px;
        }

        .tasks-panel-elegant .stats-actions button {
          opacity: 0.6;
          transition: opacity 0.2s;
        }

        .tasks-panel-elegant .stats-actions button:hover {
          opacity: 1;
        }
      `}</style>
      {confirmDialog}
      {alertDialog}
    </div>
  );
}

function StatCard({ label, count, color, active, onClick }: {
  label: string;
  count: number;
  color: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        background: active ? 'var(--bg-tertiary)' : 'var(--bg-secondary)',
        border: active ? `1px solid ${color}` : '1px solid var(--border-color)',
        borderRadius: '6px',
        padding: '10px 8px',
        cursor: 'pointer',
        transition: 'all 0.2s',
        textAlign: 'center'
      }}
      onMouseEnter={(e) => {
        if (!active) {
          e.currentTarget.style.background = 'var(--bg-tertiary)';
          e.currentTarget.style.borderColor = color;
        }
      }}
      onMouseLeave={(e) => {
        if (!active) {
          e.currentTarget.style.background = 'var(--bg-secondary)';
          e.currentTarget.style.borderColor = 'var(--border-color)';
        }
      }}
    >
      <div style={{
        fontSize: '22px',
        fontWeight: 500,
        color: color,
        marginBottom: '2px',
        letterSpacing: '-0.5px'
      }}>
        {count}
      </div>
      <div style={{
        fontSize: '12px',
        color: 'var(--text-color)',
        fontWeight: 400,
        letterSpacing: '0.3px'
      }}>
        {label}
      </div>
    </button>
  );
}

function TaskItem({ task, onDelete }: { task: Task; onDelete: (id: string) => void }) {
  const [hovered, setHovered] = useState(false);
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog();
  const handleDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    const ok = await askConfirm('删除任务', `删除任务「${task.title}」？`, 'danger', '删除');
    if (ok) {
      onDelete(task.id);
    }
  };

  const getStatusStyle = () => {
    switch (task.status) {
      case 'in_progress':
        return {
          borderLeft: '3px solid #3b82f6',
          background: 'var(--bg-secondary)'
        };
      case 'completed':
        return {
          borderLeft: '3px solid #10b981',
          background: 'var(--bg-secondary)',
          opacity: 0.7
        };
      case 'failed':
        return {
          borderLeft: '3px solid #ef4444',
          background: 'var(--bg-secondary)'
        };
      case 'stale':
        return {
          borderLeft: '3px solid #a78bfa',
          background: 'var(--bg-secondary)',
          opacity: 0.65
        };
      default:
        return {
          borderLeft: '3px solid var(--border-color)',
          background: 'var(--bg-secondary)'
        };
    }
  };

  const getStatusIcon = () => {
    switch (task.status) {
      case 'pending':
        return <span style={{ opacity: 0.5, fontSize: '14px' }}>○</span>;
      case 'in_progress':
        return <span style={{ color: '#3b82f6', fontSize: '14px' }}>◐</span>;
      case 'completed':
        return <span style={{ color: '#10b981', fontSize: '14px' }}>●</span>;
      case 'failed':
        return <span style={{ color: '#ef4444', fontSize: '14px' }}>✕</span>;
      case 'stale':
        return <span style={{ color: '#a78bfa', fontSize: '14px' }}>⊘</span>;
    }
  };

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        ...getStatusStyle(),
        borderRadius: '6px',
        padding: '12px 14px',
        transition: 'all 0.2s',
        position: 'relative',
      }}
    >
      <div style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: '10px'
      }}>
        <div style={{ paddingTop: '2px' }}>
          {getStatusIcon()}
        </div>
        <div style={{ flex: 1, minWidth: 0, paddingRight: '24px' }}>
          <div style={{
            fontSize: '14px',
            color: 'var(--text-color)',
            fontWeight: task.status === 'in_progress' ? 500 : 400,
            marginBottom: task.description ? '4px' : 0,
            letterSpacing: '0.2px'
          }}>
            {task.title}
          </div>
          {task.description && (
            <div style={{
              fontSize: '13px',
              color: 'var(--text-secondary)',
              lineHeight: '1.5',
              fontWeight: 300,
              letterSpacing: '0.2px'
            }}>
              {task.description}
            </div>
          )}
        </div>
      </div>
      <button
        onClick={handleDelete}
        title="删除此任务"
        style={{
          position: 'absolute',
          top: '8px',
          right: '8px',
          width: '22px',
          height: '22px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'transparent',
          border: 'none',
          borderRadius: '4px',
          color: 'var(--text-muted)',
          cursor: 'pointer',
          opacity: hovered ? 0.7 : 0,
          transition: 'opacity 0.15s, background 0.15s, color 0.15s',
          padding: 0,
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = 'rgba(239, 68, 68, 0.12)';
          e.currentTarget.style.color = '#ef4444';
          e.currentTarget.style.opacity = '1';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = 'transparent';
          e.currentTarget.style.color = 'var(--text-muted)';
          e.currentTarget.style.opacity = hovered ? '0.7' : '0';
        }}
      >
        <X size={13} strokeWidth={1.5} />
      </button>
      {confirmDialog}
    </div>
  );
}

function getFilterLabel(filter: string): string {
  const labels: Record<string, string> = {
    pending: '待处理',
    in_progress: '进行中',
    completed: '已完成',
    failed: '失败/归档',
    stale: '归档'
  };
  return labels[filter] || '';
}
