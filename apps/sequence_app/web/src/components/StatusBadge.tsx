export function getStatusColorClass(state: string): string {
  const s = state.toUpperCase()
  if (['RUNNING', 'CONNECTING'].includes(s)) {
    return 'bg-sky-500/20 text-sky-300'
  }
  if (['SUCCEEDED', 'COMPLETED', 'CONNECTED'].includes(s)) {
    return 'bg-emerald-500/20 text-emerald-300'
  }
  if (['FAILED', 'ERROR', 'DISCONNECTED'].includes(s)) {
    return 'bg-rose-500/20 text-rose-300'
  }
  if (['STOPPING', 'CANCELLING'].includes(s)) {
    return 'bg-amber-500/20 text-amber-300'
  }
  return 'bg-slate-700/50 text-slate-400'
}

export function StatusBadge({ state }: { state: string }) {
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-semibold ${getStatusColorClass(state)}`}
    >
      {state}
    </span>
  )
}
