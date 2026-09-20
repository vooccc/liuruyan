import { ArrowRight, Loader2 } from 'lucide-react'

interface CollabEmptyStateProps {
  loading?: boolean
  emoji?: string
  title: string
  description?: string
  tip?: string
}

export default function CollabEmptyState({
  loading = false,
  emoji,
  title,
  description,
  tip,
}: CollabEmptyStateProps) {
  if (loading) {
    return (
      <div className="collab-empty-state">
        <Loader2 size={20} className="animate-spin collab-accent-icon" />
        <p>{title}</p>
      </div>
    )
  }

  return (
    <div className="collab-empty-state">
      {emoji && <div className="collab-empty-icon">{emoji}</div>}
      <p className="collab-empty-title">{title}</p>
      {description && <p className="collab-empty-desc">{description}</p>}
      {tip && (
        <div className="collab-empty-tip">
          <ArrowRight size={12} className="collab-accent-icon" />
          <span>{tip}</span>
        </div>
      )}
    </div>
  )
}
