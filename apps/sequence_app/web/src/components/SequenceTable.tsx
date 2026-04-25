import { useMemo, useState } from 'react'

import {
  closestCenter,
  DndContext,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core'
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

import {
  FiCheck,
  FiCopy,
  FiEdit2,
  FiPlay,
  FiPlus,
  FiTrash2,
  FiX,
} from 'react-icons/fi'

import type { FieldMeta, InputMeta, SequenceStep, SequenceStepStatus } from '../api/types'
import type { Language } from '../i18n'
import { t } from '../i18n'
import { useSequenceStore } from '../store/sequenceStore'
import { StatusBadge } from './StatusBadge'

function formatValue(value: unknown, fallback: string): string {
  if (value === null || value === undefined || value === '') {
    return fallback
  }
  if (typeof value === 'string') {
    return value
  }
  return JSON.stringify(value)
}

function formatSchedule(
  startAt: string | null,
  endAt: string | null,
  fallback: string,
): string {
  if (startAt === null && endAt === null) {
    return fallback
  }
  if (startAt !== null && endAt !== null) {
    return `${startAt} -> ${endAt}`
  }
  return startAt ?? endAt ?? fallback
}

function summarizeExecuteParams(
  params: Record<string, unknown> | null,
  fallback: string,
): string {
  if (params === null) {
    return fallback
  }
  const entries = Object.entries(params)
  if (entries.length === 0) {
    return fallback
  }
  return entries
    .slice(0, 2)
    .map(([key, value]) => `${key}: ${formatValue(value, fallback)}`)
    .join(', ')
}

function getRuntimeStep(
  runtimeSteps: SequenceStepStatus[] | undefined,
  stepId: string,
): SequenceStepStatus | null {
  return runtimeSteps?.find((step) => step.id === stepId) ?? null
}

function coerceInputValue(field: FieldMeta, rawValue: string, checked: boolean): unknown {
  if (field.type === 'bool') {
    return checked
  }
  if (rawValue.trim() === '') {
    return null
  }
  if (field.type === 'int') {
    const parsed = Number.parseInt(rawValue, 10)
    return Number.isNaN(parsed) ? null : parsed
  }
  if (field.type === 'float') {
    const parsed = Number.parseFloat(rawValue)
    return Number.isNaN(parsed) ? null : parsed
  }
  return rawValue
}

function inputTypeForField(field: FieldMeta): string {
  switch (field.type) {
    case 'int':
    case 'float':
      return 'number'
    default:
      return 'text'
  }
}

function getInputValue(value: unknown): string {
  if (value === null || value === undefined) {
    return ''
  }
  return String(value)
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function JsonEditor({
  value,
  onChange,
  language,
}: {
  value: unknown
  onChange: (value: unknown) => void
  language: Language
}) {
  const [text, setText] = useState(value === null ? '' : JSON.stringify(value, null, 2))
  const [error, setError] = useState<string | null>(null)

  return (
    <div className="space-y-2">
      <textarea
        value={text}
        onChange={(event) => {
          const nextText = event.target.value
          setText(nextText)
          if (nextText.trim() === '') {
            setError(null)
            onChange(null)
            return
          }
          try {
            onChange(JSON.parse(nextText))
            setError(null)
          } catch {
            setError(t(language, 'invalidJson'))
          }
        }}
        rows={6}
        className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 font-mono text-xs text-slate-100 outline-none transition focus:border-cyan-400"
      />
      {error && <p className="text-xs text-rose-300">{error}</p>}
    </div>
  )
}

function StepDetailsDialog({
  open,
  onClose,
  language,
  executeMeta,
  step,
  onChange,
  onExecuteFieldChange,
}: {
  open: boolean
  onClose: () => void
  language: Language
  executeMeta: InputMeta
  step: SequenceStep | null
  onChange: (patch: Partial<SequenceStep>) => void
  onExecuteFieldChange: (name: string, value: unknown) => void
}) {
  if (!open || step === null) {
    return null
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/75 p-4">
      <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-slate-800 bg-slate-950 p-5 shadow-2xl shadow-black/40">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.25em] text-cyan-300">
              {t(language, 'details')}
            </p>
            <h3 className="mt-1 text-lg font-semibold text-white">
              {step.label || t(language, 'editDetails')}
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            title={t(language, 'close')}
            aria-label={t(language, 'close')}
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-900"
          >
            <FiX className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-4">
          <label className="flex flex-col gap-1 text-sm text-slate-300">
            {t(language, 'label')}
            <input
              value={step.label}
              onChange={(event) => onChange({ label: event.target.value })}
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-cyan-400"
            />
          </label>

          <div className="space-y-2">
            <p className="text-sm text-slate-300">{t(language, 'executeParameters')}</p>
            {executeMeta.kind === 'none' ? (
              <p className="rounded-lg border border-dashed border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-500">
                {t(language, 'requiresNoParams')}
              </p>
            ) : executeMeta.kind === 'json' ? (
              <JsonEditor
                value={step.execute_params}
                onChange={(value) => {
                  if (value === null) {
                    onChange({ execute_params: null })
                    return
                  }
                  if (isPlainObject(value)) {
                    onChange({ execute_params: value })
                  }
                }}
                language={language}
              />
            ) : (
              <div className="grid gap-3 md:grid-cols-2">
                {executeMeta.fields.map((field) =>
                  field.type === 'bool' ? (
                    <label
                      key={field.name}
                      className="flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2 text-sm text-slate-300"
                    >
                      <input
                        type="checkbox"
                        checked={Boolean(step.execute_params?.[field.name] ?? field.default)}
                        onChange={(event) =>
                          onExecuteFieldChange(field.name, event.target.checked)
                        }
                        className="h-4 w-4 rounded border-slate-600 bg-slate-900 text-cyan-400"
                      />
                      {field.label}
                    </label>
                  ) : (
                    <label
                      key={field.name}
                      className="flex flex-col gap-1 text-sm text-slate-300"
                    >
                      {field.label}
                      <input
                        type={inputTypeForField(field)}
                        value={getInputValue(step.execute_params?.[field.name] ?? field.default)}
                        onChange={(event) =>
                          onExecuteFieldChange(
                            field.name,
                            coerceInputValue(
                              field,
                              event.target.value,
                              event.target.checked,
                            ),
                          )
                        }
                        className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-cyan-400"
                      />
                    </label>
                  ),
                )}
              </div>
            )}
          </div>

          <label className="flex flex-col gap-1 text-sm text-slate-300">
            {t(language, 'notes')}
            <textarea
              value={step.notes}
              onChange={(event) => onChange({ notes: event.target.value })}
              rows={5}
              className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100 outline-none transition focus:border-cyan-400"
            />
          </label>
        </div>
      </div>
    </div>
  )
}

function InlineTargetEditor({
  language,
  value,
  meta,
  disabled,
  onCommit,
}: {
  language: Language
  value: unknown
  meta: InputMeta
  disabled: boolean
  onCommit: (value: unknown) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)

  if (!editing) {
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={() => {
          setDraft(value)
          setEditing(true)
        }}
        className="w-full rounded-lg border border-transparent bg-slate-900/70 px-2 py-2 text-left text-slate-200 transition hover:border-slate-700 hover:bg-slate-900 disabled:cursor-not-allowed disabled:text-slate-500"
      >
        {formatValue(value, t(language, 'notAvailable'))}
      </button>
    )
  }

  const field = meta.fields[0]

  return (
    <div className="space-y-2 rounded-lg border border-cyan-500/40 bg-slate-900 p-2">
      {meta.kind === 'json' || !field ? (
        <JsonEditor value={draft} onChange={setDraft} language={language} />
      ) : field.type === 'bool' ? (
        <label className="flex items-center gap-2 text-sm text-slate-200">
          <input
            type="checkbox"
            checked={Boolean(draft)}
            onChange={(event) => setDraft(event.target.checked)}
            className="h-4 w-4 rounded border-slate-600 bg-slate-900 text-cyan-400"
          />
          {field.label}
        </label>
      ) : (
        <input
          type={inputTypeForField(field)}
          value={getInputValue(draft)}
          onChange={(event) =>
            setDraft(coerceInputValue(field, event.target.value, event.target.checked))
          }
          className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-slate-100 outline-none transition focus:border-cyan-400"
        />
      )}
      <div className="flex gap-1">
        <button
          type="button"
          onClick={() => {
            onCommit(draft)
            setEditing(false)
          }}
          title={t(language, 'save')}
          aria-label={t(language, 'save')}
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-cyan-500 text-slate-950 transition hover:bg-cyan-400"
        >
          <FiCheck className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => {
            setDraft(value)
            setEditing(false)
          }}
          title={t(language, 'cancel')}
          aria-label={t(language, 'cancel')}
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-800"
        >
          <FiX className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

function InlineScheduleEditor({
  language,
  startAt,
  endAt,
  disabled,
  onCommit,
}: {
  language: Language
  startAt: string | null
  endAt: string | null
  disabled: boolean
  onCommit: (value: { start_at: string | null; end_at: string | null }) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draftStart, setDraftStart] = useState(startAt ?? '')
  const [draftEnd, setDraftEnd] = useState(endAt ?? '')

  if (!editing) {
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={() => {
          setDraftStart(startAt ?? '')
          setDraftEnd(endAt ?? '')
          setEditing(true)
        }}
        className="w-full rounded-lg border border-transparent bg-slate-900/70 px-2 py-2 text-left text-slate-200 transition hover:border-slate-700 hover:bg-slate-900 disabled:cursor-not-allowed disabled:text-slate-500"
      >
        {formatSchedule(startAt, endAt, t(language, 'notAvailable'))}
      </button>
    )
  }

  return (
    <div className="space-y-2 rounded-lg border border-cyan-500/40 bg-slate-900 p-2">
      <label className="flex flex-col gap-1 text-xs text-slate-300">
        {t(language, 'startsAt')}
        <input
          type="datetime-local"
          value={draftStart}
          onChange={(event) => setDraftStart(event.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-slate-100 outline-none transition focus:border-cyan-400"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-slate-300">
        {t(language, 'endsAt')}
        <input
          type="datetime-local"
          value={draftEnd}
          onChange={(event) => setDraftEnd(event.target.value)}
          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-2 text-slate-100 outline-none transition focus:border-cyan-400"
        />
      </label>
      <div className="flex gap-1">
        <button
          type="button"
          onClick={() => {
            onCommit({
              start_at: draftStart === '' ? null : draftStart,
              end_at: draftEnd === '' ? null : draftEnd,
            })
            setEditing(false)
          }}
          title={t(language, 'save')}
          aria-label={t(language, 'save')}
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-cyan-500 text-slate-950 transition hover:bg-cyan-400"
        >
          <FiCheck className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => setEditing(false)}
          title={t(language, 'cancel')}
          aria-label={t(language, 'cancel')}
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-700 text-slate-200 transition hover:bg-slate-800"
        >
          <FiX className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}

interface SortableStepRowProps {
  language: Language
  step: SequenceStep
  index: number
  isSelected: boolean
  targetMeta: InputMeta
  runtime: SequenceStepStatus | null
  sequenceBusy: boolean
  manualBusy: boolean
  onSelect: () => void
  onToggleEnabled: (enabled: boolean) => void
  onUpdateTarget: (value: unknown) => void
  onUpdateSchedule: (value: { start_at: string | null; end_at: string | null }) => void
  onOpenDetails: () => void
  onSingleRun: () => void
  onDuplicate: () => void
  onDelete: () => void
}

function SortableStepRow({
  language,
  step,
  index,
  isSelected,
  targetMeta,
  runtime,
  sequenceBusy,
  manualBusy,
  onSelect,
  onToggleEnabled,
  onUpdateTarget,
  onUpdateSchedule,
  onOpenDetails,
  onSingleRun,
  onDuplicate,
  onDelete,
}: SortableStepRowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({
      id: step.id,
      disabled: sequenceBusy,
    })

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  }

  return (
    <tr
      ref={setNodeRef}
      style={style}
      onClick={onSelect}
      className={`border-b border-slate-900 align-top transition ${
        isSelected ? 'bg-cyan-500/10' : 'hover:bg-slate-900/70'
      } ${isDragging ? 'opacity-60' : ''}`}
    >
      <td
        {...attributes}
        {...listeners}
        onClick={(event) => event.stopPropagation()}
        title={t(language, 'dragToReorder')}
        className={`px-3 py-3 align-middle select-none ${
          sequenceBusy ? 'cursor-default' : 'cursor-grab active:cursor-grabbing'
        }`}
      >
        <input
          type="checkbox"
          checked={step.enabled}
          disabled={sequenceBusy}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => event.stopPropagation()}
          onChange={(event) => onToggleEnabled(event.target.checked)}
          className="h-4 w-4 cursor-pointer rounded border-slate-600 bg-slate-900 text-cyan-400"
        />
      </td>
      <td className="px-3 py-2 text-slate-100">
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            onOpenDetails()
          }}
          className="text-left transition hover:text-cyan-300"
        >
          {step.label || `${t(language, 'label')} ${index + 1}`}
        </button>
        {!step.enabled && (
          <span className="ml-2 inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium bg-slate-700/50 text-slate-400">
            {t(language, 'disabled')}
          </span>
        )}
      </td>
      <td className="min-w-48 px-3 py-2">
        <InlineTargetEditor
          language={language}
          value={step.target}
          meta={targetMeta}
          disabled={sequenceBusy}
          onCommit={onUpdateTarget}
        />
      </td>
      <td className="min-w-64 px-3 py-2">
        <InlineScheduleEditor
          language={language}
          startAt={step.start_at}
          endAt={step.end_at}
          disabled={sequenceBusy}
          onCommit={onUpdateSchedule}
        />
      </td>
      <td className="min-w-48 px-3 py-2">
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation()
            onOpenDetails()
          }}
          title={t(language, 'editDetails')}
          aria-label={t(language, 'editDetails')}
          className="flex w-full items-center gap-2 rounded-lg border border-slate-700 bg-slate-900/70 px-2 py-2 text-left text-slate-300 transition hover:bg-slate-900"
        >
          <FiEdit2 className="h-3.5 w-3.5 shrink-0 text-slate-500" />
          <span className="truncate text-xs">
            {summarizeExecuteParams(step.execute_params, t(language, 'parameterSummary'))}
          </span>
        </button>
      </td>
      <td className="min-w-48 px-3 py-2 text-sm text-slate-300">
        <StatusBadge state={runtime?.state ?? 'PENDING'} />
        <div className="mt-1 text-xs text-slate-500">
          {runtime?.countdown_text ?? runtime?.message ?? t(language, 'notAvailable')}
        </div>
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center justify-end gap-1">
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              onSingleRun()
            }}
            disabled={sequenceBusy || manualBusy}
            title={t(language, 'singleRun')}
            aria-label={t(language, 'singleRun')}
            className="rounded-lg p-2 text-cyan-400 transition hover:bg-cyan-500/10 disabled:cursor-not-allowed disabled:text-slate-600"
          >
            <FiPlay className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              onDuplicate()
            }}
            disabled={sequenceBusy}
            title={t(language, 'duplicate')}
            aria-label={t(language, 'duplicate')}
            className="rounded-lg p-2 text-slate-400 transition hover:bg-slate-700/50 hover:text-slate-200 disabled:cursor-not-allowed disabled:text-slate-600"
          >
            <FiCopy className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              onDelete()
            }}
            disabled={sequenceBusy}
            title={t(language, 'delete')}
            aria-label={t(language, 'delete')}
            className="rounded-lg p-2 text-slate-400 transition hover:bg-rose-500/10 hover:text-rose-300 disabled:cursor-not-allowed disabled:text-slate-600"
          >
            <FiTrash2 className="h-4 w-4" />
          </button>
        </div>
      </td>
    </tr>
  )
}

export function SequenceTable() {
  const language = useSequenceStore((state) => state.language)
  const meta = useSequenceStore((state) => state.meta)
  const definition = useSequenceStore((state) => state.definition)
  const selectedStepIndex = useSequenceStore((state) => state.selectedStepIndex)
  const manualExecutionStatus = useSequenceStore((state) => state.manualExecutionStatus)
  const sequenceSnapshot = useSequenceStore((state) => state.sequenceSnapshot)
  const addStep = useSequenceStore((state) => state.addStep)
  const duplicateStep = useSequenceStore((state) => state.duplicateStep)
  const deleteStep = useSequenceStore((state) => state.deleteStep)
  const reorderSteps = useSequenceStore((state) => state.reorderSteps)
  const selectStep = useSequenceStore((state) => state.selectStep)
  const updateStep = useSequenceStore((state) => state.updateStep)
  const updateStepExecuteField = useSequenceStore((state) => state.updateStepExecuteField)
  const runStepCheck = useSequenceStore((state) => state.runStepCheck)
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }))
  const [detailsIndex, setDetailsIndex] = useState<number | null>(null)

  const isSequenceBusy =
    sequenceSnapshot !== null &&
    ['RUNNING', 'STOPPING'].includes(sequenceSnapshot.state)
  const isManualBusy =
    manualExecutionStatus !== null &&
    ['RUNNING', 'CANCELLING'].includes(manualExecutionStatus.state)

  const runtimeSteps = useMemo(() => sequenceSnapshot?.steps ?? [], [sequenceSnapshot])
  const dialogStep = detailsIndex === null ? null : definition.steps[detailsIndex] ?? null

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    if (!over || active.id === over.id) {
      return
    }
    reorderSteps(String(active.id), String(over.id))
  }

  if (meta === null) {
    return (
      <section className="rounded-xl border border-slate-800 bg-slate-950/60 p-4 text-sm text-slate-400 shadow-lg shadow-black/20">
        {t(language, 'loading')}
      </section>
    )
  }

  return (
    <section className="rounded-xl border border-slate-800 bg-slate-950/60 p-4 shadow-lg shadow-black/20">
      <div className="overflow-x-auto">
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
          <table className="min-w-full border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-slate-400">
                <th className="w-12 px-3 py-2 font-medium">{t(language, 'enabled')}</th>
                <th className="min-w-44 px-3 py-2 font-medium">{t(language, 'label')}</th>
                <th className="min-w-48 px-3 py-2 font-medium">{t(language, 'target')}</th>
                <th className="min-w-64 px-3 py-2 font-medium">{t(language, 'schedule')}</th>
                <th className="min-w-48 px-3 py-2 font-medium">{t(language, 'parameters')}</th>
                <th className="min-w-48 px-3 py-2 font-medium">{t(language, 'runtime')}</th>
                <th className="px-3 py-2 text-right font-medium">{t(language, 'actions')}</th>
              </tr>
            </thead>
            <SortableContext
              items={definition.steps.map((step) => step.id)}
              strategy={verticalListSortingStrategy}
            >
              <tbody>
                {definition.steps.length === 0 ? (
                  <tr>
                    <td className="px-3 py-5 text-slate-500" colSpan={7}>
                      {t(language, 'noStepsYet')}
                    </td>
                  </tr>
                ) : (
                  definition.steps.map((step, index) => (
                    <SortableStepRow
                      key={step.id}
                      language={language}
                      step={step}
                      index={index}
                      isSelected={selectedStepIndex === index}
                      targetMeta={meta.target}
                      runtime={getRuntimeStep(runtimeSteps, step.id)}
                      sequenceBusy={isSequenceBusy}
                      manualBusy={isManualBusy}
                      onSelect={() => selectStep(index)}
                      onToggleEnabled={(enabled) => updateStep(index, { enabled })}
                      onUpdateTarget={(value) => updateStep(index, { target: value })}
                      onUpdateSchedule={(value) => updateStep(index, value)}
                      onOpenDetails={() => {
                        selectStep(index)
                        setDetailsIndex(index)
                      }}
                      onSingleRun={() => void runStepCheck(index)}
                      onDuplicate={() => duplicateStep(index)}
                      onDelete={() => deleteStep(index)}
                    />
                  ))
                )}
              </tbody>
            </SortableContext>
          </table>
        </DndContext>
      </div>

      <div className="mt-4 border-t border-slate-800 pt-4">
        <button
          type="button"
          onClick={() => addStep()}
          disabled={isSequenceBusy}
          title={t(language, 'addStep')}
          aria-label={t(language, 'addStep')}
          className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-cyan-500 text-slate-950 transition hover:bg-cyan-400 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
        >
          <FiPlus className="h-5 w-5" />
        </button>
      </div>

      <StepDetailsDialog
        open={detailsIndex !== null}
        onClose={() => setDetailsIndex(null)}
        language={language}
        executeMeta={meta.execute}
        step={dialogStep}
        onChange={(patch) => {
          if (detailsIndex !== null) {
            updateStep(detailsIndex, patch)
          }
        }}
        onExecuteFieldChange={(name, value) => {
          if (detailsIndex !== null) {
            updateStepExecuteField(detailsIndex, name, value)
          }
        }}
      />
    </section>
  )
}
