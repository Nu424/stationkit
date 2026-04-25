import { useEffect } from 'react'
import { FiX } from 'react-icons/fi'

import { DevicePanel } from './components/DevicePanel'
import { RunStatusPanel } from './components/RunStatusPanel'
import { RunToolbar } from './components/RunToolbar'
import { SequenceTable } from './components/SequenceTable'
import { t } from './i18n'
import { useSequenceStore } from './store/sequenceStore'

function App() {
  const language = useSequenceStore((state) => state.language)
  const loadMeta = useSequenceStore((state) => state.loadMeta)
  const refreshStatus = useSequenceStore((state) => state.refreshStatus)
  const ui = useSequenceStore((state) => state.ui)
  const clearError = useSequenceStore((state) => state.clearError)

  useEffect(() => {
    void loadMeta()
    void refreshStatus()
  }, [loadMeta, refreshStatus])

  useEffect(() => {
    if (!ui.pollingEnabled) {
      return undefined
    }

    const timer = window.setInterval(() => {
      void refreshStatus()
    }, 1000)
    return () => window.clearInterval(timer)
  }, [refreshStatus, ui.pollingEnabled])

  return (
    <main className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-4 py-6 lg:px-6">
        <DevicePanel />

        {ui.errorMessage && (
          <div className="flex items-center justify-between gap-3 rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">
            <span>{ui.errorMessage}</span>
            <button
              type="button"
              onClick={() => clearError()}
              title={t(language, 'dismiss')}
              aria-label={t(language, 'dismiss')}
              className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-rose-400/50 text-rose-100 transition hover:bg-rose-500/10"
            >
              <FiX className="h-4 w-4" />
            </button>
          </div>
        )}

        <RunToolbar />

        <SequenceTable />
        <RunStatusPanel />
      </div>
    </main>
  )
}

export default App
