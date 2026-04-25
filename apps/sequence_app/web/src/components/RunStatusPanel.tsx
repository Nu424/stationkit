import { useMemo } from 'react'
import {
  FiAlertCircle,
  FiAlertTriangle,
  FiCrosshair,
  FiLayers,
  FiTerminal,
  FiZap,
} from 'react-icons/fi'

import { t } from '../i18n'
import { useSequenceStore } from '../store/sequenceStore'
import type { SequenceIssue } from '../api/types'
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

function IssueList({
  title,
  issues,
  language,
  variant,
}: {
  title: string
  issues: SequenceIssue[]
  language: 'en' | 'ja'
  variant: 'validation' | 'run'
}) {
  const IssueIcon = variant === 'validation' ? FiAlertCircle : FiAlertTriangle

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-3">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
        <IssueIcon className="h-4 w-4 shrink-0 text-slate-500" aria-hidden />
        {title}
      </h3>
      {issues.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">{t(language, 'noIssues')}</p>
      ) : (
        <ul className="mt-2 space-y-2 text-sm text-slate-300">
          {issues.map((issue, index) => (
            <li key={`${issue.code}-${index}`} className="rounded-lg bg-slate-950/80 p-2">
              <div className="flex items-center gap-2">
                <span
                  className={`rounded px-2 py-0.5 text-xs font-semibold ${
                    issue.severity === 'ERROR'
                      ? 'bg-rose-500/20 text-rose-300'
                      : 'bg-amber-500/20 text-amber-300'
                  }`}
                >
                  {issue.severity}
                </span>
                <span className="font-medium text-slate-100">{issue.code}</span>
              </div>
              <p className="mt-1 text-slate-400">{issue.message}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function RunStatusPanel() {
  const language = useSequenceStore((state) => state.language)
  const definition = useSequenceStore((state) => state.definition)
  const selectedStepIndex = useSequenceStore((state) => state.selectedStepIndex)
  const controllerStatus = useSequenceStore((state) => state.controllerStatus)
  const manualExecutionStatus = useSequenceStore((state) => state.manualExecutionStatus)
  const sequenceSnapshot = useSequenceStore((state) => state.sequenceSnapshot)
  const issues = useSequenceStore((state) => state.issues)

  const snapshotIssues = useMemo(
    () => sequenceSnapshot?.issues ?? [],
    [sequenceSnapshot],
  )
  const selectedDefinitionStep =
    selectedStepIndex === null ? null : definition.steps[selectedStepIndex] ?? null
  const selectedRuntimeStep =
    selectedDefinitionStep === null
      ? null
      : sequenceSnapshot?.steps.find((step) => step.id === selectedDefinitionStep.id) ?? null

  return (
    <section className="space-y-4 rounded-xl border border-slate-800 bg-slate-950/60 p-4 shadow-lg shadow-black/20">
      <div className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr_0.9fr]">
        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
            <FiCrosshair className="h-4 w-4 shrink-0 text-slate-500" aria-hidden />
            {t(language, 'selectedStep')}
          </h2>
          {selectedDefinitionStep === null ? (
            <p className="mt-3 text-sm text-slate-500">
              {t(language, 'selectedStepEmpty')}
            </p>
          ) : (
            <dl className="mt-3 space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <dt className="text-slate-400">{t(language, 'label')}</dt>
                <dd className="text-slate-200">
                  {selectedDefinitionStep.label || `${t(language, 'label')} ${selectedStepIndex! + 1}`}
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-slate-400">{t(language, 'target')}</dt>
                <dd className="max-w-80 truncate text-slate-200">
                  {formatValue(selectedDefinitionStep.target)}
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-slate-400">{t(language, 'runtime')}</dt>
                <dd>
                  <StatusBadge state={selectedRuntimeStep?.state ?? 'PENDING'} />
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-slate-400">{t(language, 'message')}</dt>
                <dd className="max-w-80 truncate text-slate-200">
                  {selectedRuntimeStep?.countdown_text ??
                    selectedRuntimeStep?.message ??
                    t(language, 'notAvailable')}
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-slate-400">{t(language, 'result')}</dt>
                <dd className="max-w-80 truncate text-slate-200">
                  {formatValue(selectedRuntimeStep?.result)}
                </dd>
              </div>
              <div className="flex justify-between gap-4">
                <dt className="text-slate-400">{t(language, 'error')}</dt>
                <dd className="max-w-80 truncate text-slate-200">
                  {selectedRuntimeStep?.error_message ?? t(language, 'notAvailable')}
                </dd>
              </div>
            </dl>
          )}
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
            <FiZap className="h-4 w-4 shrink-0 text-slate-500" aria-hidden />
            {t(language, 'manualExecution')}
          </h2>
          <dl className="mt-3 space-y-2 text-sm">
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'state')}</dt>
              <dd>
                <StatusBadge state={manualExecutionStatus?.state ?? 'IDLE'} />
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'executionId')}</dt>
              <dd className="truncate text-slate-200">
                {manualExecutionStatus?.execution_id ?? t(language, 'notAvailable')}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'result')}</dt>
              <dd className="max-w-72 truncate text-slate-200">
                {formatValue(manualExecutionStatus?.result)}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'error')}</dt>
              <dd className="max-w-72 truncate text-slate-200">
                {manualExecutionStatus?.error_message ?? t(language, 'notAvailable')}
              </dd>
            </div>
          </dl>
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-3">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
            <FiLayers className="h-4 w-4 shrink-0 text-slate-500" aria-hidden />
            {t(language, 'sequenceRun')}
          </h2>
          <dl className="mt-3 space-y-2 text-sm">
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'state')}</dt>
              <dd>
                <StatusBadge state={sequenceSnapshot?.state ?? 'IDLE'} />
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'runId')}</dt>
              <dd className="truncate text-slate-200">
                {sequenceSnapshot?.run_id ?? t(language, 'notAvailable')}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'currentStep')}</dt>
              <dd className="text-slate-200">
                {sequenceSnapshot?.current_step_index !== null &&
                sequenceSnapshot?.current_step_index !== undefined
                  ? sequenceSnapshot.current_step_index + 1
                  : t(language, 'notAvailable')}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'message')}</dt>
              <dd className="max-w-72 truncate text-slate-200">
                {sequenceSnapshot?.message ?? t(language, 'notAvailable')}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'startedAt')}</dt>
              <dd className="max-w-72 truncate text-slate-200">
                {sequenceSnapshot?.started_at ?? t(language, 'notAvailable')}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-slate-400">{t(language, 'finishedAt')}</dt>
              <dd className="max-w-72 truncate text-slate-200">
                {sequenceSnapshot?.finished_at ?? t(language, 'notAvailable')}
              </dd>
            </div>
          </dl>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.8fr_0.8fr_1.2fr]">
        <IssueList
          title={t(language, 'validationIssues')}
          issues={issues}
          language={language}
          variant="validation"
        />
        <IssueList
          title={t(language, 'runIssues')}
          issues={snapshotIssues}
          language={language}
          variant="run"
        />
        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-3">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-100">
            <FiTerminal className="h-4 w-4 shrink-0 text-slate-500" aria-hidden />
            {t(language, 'callLog')}
          </h3>
          {controllerStatus?.call_log && controllerStatus.call_log.length > 0 ? (
            <pre className="mt-2 max-h-64 overflow-auto rounded-lg bg-slate-950/80 p-3 text-xs text-slate-300">
              {controllerStatus.call_log.join('\n')}
            </pre>
          ) : (
            <p className="mt-2 text-sm text-slate-500">{t(language, 'notAvailable')}</p>
          )}
        </div>
      </div>
    </section>
  )
}
