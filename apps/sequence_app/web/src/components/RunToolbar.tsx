import { useRef, useState } from 'react'
import {
  FiAlertTriangle,
  FiCheck,
  FiCheckCircle,
  FiDownload,
  FiPlay,
  FiRotateCcw,
  FiSquare,
  FiUpload,
  FiX,
} from 'react-icons/fi'

import { t } from '../i18n'
import { useSequenceStore } from '../store/sequenceStore'

export function RunToolbar() {
  const language = useSequenceStore((state) => state.language)
  const meta = useSequenceStore((state) => state.meta)
  const definition = useSequenceStore((state) => state.definition)
  const ui = useSequenceStore((state) => state.ui)
  const controllerStatus = useSequenceStore((state) => state.controllerStatus)
  const sequenceSnapshot = useSequenceStore((state) => state.sequenceSnapshot)
  const setSequenceName = useSequenceStore((state) => state.setSequenceName)
  const setSequenceMode = useSequenceStore((state) => state.setSequenceMode)
  const resetDefinition = useSequenceStore((state) => state.resetDefinition)
  const importDefinition = useSequenceStore((state) => state.importDefinition)
  const exportDefinition = useSequenceStore((state) => state.exportDefinition)
  const validateSequence = useSequenceStore((state) => state.validateSequence)
  const runSequence = useSequenceStore((state) => state.runSequence)
  const requestStopConfirmation = useSequenceStore(
    (state) => state.requestStopConfirmation,
  )
  const cancelStopConfirmation = useSequenceStore(
    (state) => state.cancelStopConfirmation,
  )
  const stopSequence = useSequenceStore((state) => state.stopSequence)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [confirmingReset, setConfirmingReset] = useState(false)

  const sequenceBusy =
    sequenceSnapshot !== null &&
    ['RUNNING', 'STOPPING'].includes(sequenceSnapshot.state)
  const isLoading = ui.pendingRequests > 0
  const isControllerConnected =
    controllerStatus?.controller_state === 'CONNECTED'
  const availableModes = meta?.sequence_modes ?? [definition.mode]

  const handleExport = () => {
    const blob = new Blob([exportDefinition()], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${definition.name || 'sequence'}.json`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const handleImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) {
      return
    }
    const text = await file.text()
    importDefinition(text)
    event.target.value = ''
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-950/60 p-4 shadow-lg shadow-black/20">
      <div className="grid gap-4 xl:grid-cols-[1.2fr_1.4fr]">
        <div className="grid gap-3 md:grid-cols-[minmax(16rem,1fr)_13rem]">
          <label className="flex flex-col gap-1 text-sm text-slate-300">
            {t(language, 'sequenceName')}
            <input
              value={definition.name}
              onChange={(event) => setSequenceName(event.target.value)}
              disabled={sequenceBusy}
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-cyan-400 disabled:cursor-not-allowed disabled:text-slate-500"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-slate-300">
            {t(language, 'mode')}
            <select
              value={definition.mode}
              onChange={(event) =>
                setSequenceMode(
                  event.target.value as 'COMPLETION_DRIVEN' | 'TIME_DRIVEN',
                )
              }
              disabled={sequenceBusy || availableModes.length === 1}
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-cyan-400 disabled:cursor-not-allowed disabled:text-slate-500"
            >
              {availableModes.map((mode) => (
                <option key={mode} value={mode}>
                  {t(
                    language,
                    mode === 'COMPLETION_DRIVEN' ? 'modeCompletion' : 'modeTime',
                  )}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="flex flex-wrap items-end justify-start gap-1.5 xl:justify-end">
          <button
            type="button"
            onClick={() => void validateSequence()}
            disabled={isLoading}
            title={t(language, 'validate')}
            aria-label={t(language, 'validate')}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:border-slate-800 disabled:text-slate-500"
          >
            <FiCheckCircle className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => void runSequence()}
            disabled={isLoading || sequenceBusy || !isControllerConnected}
            title={
              isControllerConnected
                ? t(language, 'runSequence')
                : t(language, 'runRequiresConnected')
            }
            aria-label={
              isControllerConnected
                ? t(language, 'runSequence')
                : t(language, 'runRequiresConnected')
            }
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-500 text-slate-950 transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
          >
            {isLoading && !sequenceBusy ? (
              <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
            ) : (
              <FiPlay className="h-4 w-4" />
            )}
          </button>
          {!ui.confirmStop ? (
            <button
              type="button"
              onClick={() => requestStopConfirmation()}
              disabled={!sequenceBusy}
              title={t(language, 'stop')}
              aria-label={t(language, 'stop')}
              className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-amber-500/60 text-amber-300 transition hover:bg-amber-500/10 disabled:cursor-not-allowed disabled:border-slate-800 disabled:text-slate-500"
            >
              <FiSquare className="h-4 w-4" />
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={() => void stopSequence()}
                title={t(language, 'confirmStop')}
                aria-label={t(language, 'confirmStop')}
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-amber-500 text-slate-950 transition hover:bg-amber-400"
              >
                <FiAlertTriangle className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => cancelStopConfirmation()}
                title={t(language, 'cancel')}
                aria-label={t(language, 'cancel')}
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-900"
              >
                <FiX className="h-4 w-4" />
              </button>
            </>
          )}
          <button
            type="button"
            onClick={handleExport}
            title={t(language, 'exportJson')}
            aria-label={t(language, 'exportJson')}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-900"
          >
            <FiUpload className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title={t(language, 'importJson')}
            aria-label={t(language, 'importJson')}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-900"
          >
            <FiDownload className="h-4 w-4" />
          </button>
          {!confirmingReset ? (
            <button
              type="button"
              onClick={() => setConfirmingReset(true)}
              disabled={isLoading}
              title={t(language, 'resetSequence')}
              aria-label={t(language, 'resetSequence')}
              className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-rose-500/60 text-rose-300 transition hover:bg-rose-500/10 disabled:cursor-not-allowed disabled:border-slate-800 disabled:text-slate-500"
            >
              <FiRotateCcw className="h-4 w-4" />
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={() => {
                  resetDefinition()
                  setConfirmingReset(false)
                }}
                title={t(language, 'confirmResetAction')}
                aria-label={t(language, 'confirmResetAction')}
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-rose-500 text-white transition hover:bg-rose-400"
              >
                <FiCheck className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => setConfirmingReset(false)}
                title={t(language, 'cancel')}
                aria-label={t(language, 'cancel')}
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-900"
              >
                <FiX className="h-4 w-4" />
              </button>
            </>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept="application/json"
            onChange={(event) => void handleImport(event)}
            className="hidden"
          />
        </div>
      </div>
    </section>
  )
}
