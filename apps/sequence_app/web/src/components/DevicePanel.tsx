import { useState } from 'react'
import { FiGlobe, FiLogIn, FiLogOut, FiPause } from 'react-icons/fi'

import { t } from '../i18n'
import { useSequenceStore } from '../store/sequenceStore'
import { StatusBadge } from './StatusBadge'

function formatValue(value: unknown): string {
  if (value === null || value === undefined) {
    return 'n/a'
  }
  if (typeof value === 'string') {
    return value
  }
  return JSON.stringify(value)
}

export function DevicePanel() {
  const language = useSequenceStore((state) => state.language)
  const meta = useSequenceStore((state) => state.meta)
  const controllerStatus = useSequenceStore((state) => state.controllerStatus)
  const ui = useSequenceStore((state) => state.ui)
  const connect = useSequenceStore((state) => state.connect)
  const disconnect = useSequenceStore((state) => state.disconnect)
  const idle = useSequenceStore((state) => state.idle)
  const setLanguage = useSequenceStore((state) => state.setLanguage)
  const [address, setAddress] = useState('COM1')

  const isLoading = ui.pendingRequests > 0
  const controllerState = controllerStatus?.controller_state ?? 'UNKNOWN'
  const isConnected = controllerState === 'CONNECTED'

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-950/60 p-4 shadow-lg shadow-black/20">
      <div className="mb-1 flex items-center justify-end gap-2">
        <FiGlobe className="h-3.5 w-3.5 shrink-0 text-slate-500" aria-hidden />
        <select
          value={language}
          title={t(language, 'language')}
          aria-label={t(language, 'language')}
          onChange={(event) => setLanguage(event.target.value === 'ja' ? 'ja' : 'en')}
          className="rounded-md border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-300 outline-none transition focus:border-cyan-400"
        >
          <option value="ja">日本語</option>
          <option value="en">English</option>
        </select>
      </div>

      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div className="flex-1">
          <p className="text-xs font-semibold uppercase tracking-[0.25em] text-cyan-300">
            {t(language, 'device')}
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-white">
            {meta?.controller_name ?? t(language, 'appTitle')}
          </h1>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-400">
            <div className="flex items-center gap-1.5">
              <span>{t(language, 'state')}:</span>
              <StatusBadge state={controllerState} />
            </div>
            <span className="text-slate-700">·</span>
            <span>
              {t(language, 'target')}:{' '}
              <span className="font-medium text-slate-200">
                {formatValue(controllerStatus?.current_target)}
              </span>
            </span>
          </div>
        </div>

        <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <label className="flex min-w-52 flex-col gap-1 text-sm text-slate-300">
            {t(language, 'address')}
            <input
              value={address}
              onChange={(event) => setAddress(event.target.value)}
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-cyan-400"
              placeholder="COM1 / tcp://..."
            />
          </label>
          <button
            type="button"
            onClick={() => void connect(address)}
            disabled={isLoading || address.trim() === '' || isConnected}
            title={t(language, 'connect')}
            aria-label={t(language, 'connect')}
            className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-cyan-500 text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
          >
            <FiLogIn className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => void idle()}
            disabled={isLoading || !isConnected}
            title={t(language, 'idle')}
            aria-label={t(language, 'idle')}
            className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:border-slate-500 hover:bg-slate-900 disabled:cursor-not-allowed disabled:border-slate-800 disabled:text-slate-500"
          >
            <FiPause className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => void disconnect()}
            disabled={isLoading || !isConnected}
            title={t(language, 'disconnect')}
            aria-label={t(language, 'disconnect')}
            className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:border-slate-500 hover:bg-slate-900 disabled:cursor-not-allowed disabled:border-slate-800 disabled:text-slate-500"
          >
            <FiLogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </section>
  )
}
