import { arrayMove } from '@dnd-kit/sortable'
import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

import { sequenceApi } from '../api/client'
import type {
  ControllerStatus,
  ExecutionStatus,
  FieldMeta,
  InputMeta,
  SequenceDefinition,
  SequenceIssue,
  SequenceMetaResponse,
  SequenceMode,
  SequenceSnapshot,
  SequenceStatusResponse,
  SequenceStep,
} from '../api/types'
import type { Language } from '../i18n'
import { t } from '../i18n'

interface UiState {
  pendingRequests: number
  errorMessage: string | null
  confirmStop: boolean
  pollingEnabled: boolean
}

interface SequenceStore {
  language: Language
  meta: SequenceMetaResponse | null
  definition: SequenceDefinition
  selectedStepIndex: number | null
  controllerStatus: ControllerStatus | null
  manualExecutionStatus: ExecutionStatus | null
  sequenceSnapshot: SequenceSnapshot | null
  issues: SequenceIssue[]
  ui: UiState
  setLanguage: (language: Language) => void
  loadMeta: () => Promise<void>
  refreshStatus: () => Promise<void>
  connect: (address: string) => Promise<void>
  disconnect: () => Promise<void>
  idle: () => Promise<void>
  changeTarget: (target: unknown) => Promise<void>
  setSequenceName: (name: string) => void
  setSequenceMode: (mode: SequenceMode) => void
  selectStep: (index: number | null) => void
  addStep: () => void
  duplicateStep: (index: number) => void
  deleteStep: (index: number) => void
  reorderSteps: (activeId: string, overId: string) => void
  updateStep: (index: number, patch: Partial<SequenceStep>) => void
  updateStepExecuteField: (index: number, name: string, value: unknown) => void
  resetDefinition: () => void
  importDefinition: (jsonText: string) => void
  exportDefinition: () => string
  validateSequence: () => Promise<boolean>
  runSequence: () => Promise<void>
  requestStopConfirmation: () => void
  cancelStopConfirmation: () => void
  stopSequence: () => Promise<void>
  runStepCheck: (index: number) => Promise<void>
  clearError: () => void
}

const defaultDefinition: SequenceDefinition = {
  version: 1,
  name: 'Sequence',
  mode: 'COMPLETION_DRIVEN',
  steps: [],
}

function generateStepId(): string {
  return crypto.randomUUID().replaceAll('-', '')
}

function createEmptyStep(meta: SequenceMetaResponse | null): SequenceStep {
  return {
    id: generateStepId(),
    enabled: true,
    label: '',
    target: defaultValueForInput(meta?.target ?? null),
    execute_params: defaultExecuteParams(meta?.execute ?? null),
    start_at: null,
    end_at: null,
    notes: '',
  }
}

function defaultValueForInput(input: InputMeta | null): unknown {
  if (input === null) {
    return null
  }
  if (input.kind === 'field' && input.fields.length > 0) {
    return defaultValueForField(input.fields[0])
  }
  return null
}

function defaultExecuteParams(input: InputMeta | null): Record<string, unknown> | null {
  if (input === null || input.kind === 'none' || input.kind === 'json') {
    return null
  }

  const values: Record<string, unknown> = {}
  for (const field of input.fields) {
    const defaultValue = defaultValueForField(field)
    if (defaultValue !== null && defaultValue !== undefined) {
      values[field.name] = defaultValue
    }
  }
  return values
}

function defaultValueForField(field: FieldMeta): unknown {
  if (field.default !== null && field.default !== undefined) {
    return field.default
  }
  if (field.nullable || !field.required) {
    return null
  }
  switch (field.type) {
    case 'str':
      return ''
    case 'bool':
      return false
    case 'int':
    case 'float':
    case 'json':
      return null
  }
}

function isManualExecutionActive(status: ExecutionStatus | null): boolean {
  return status !== null && ['RUNNING', 'CANCELLING'].includes(status.state)
}

function isSequenceActive(snapshot: SequenceSnapshot | null): boolean {
  return snapshot !== null && ['RUNNING', 'STOPPING'].includes(snapshot.state)
}

function cloneDefinition(definition: SequenceDefinition): SequenceDefinition {
  return JSON.parse(JSON.stringify(definition)) as SequenceDefinition
}

function clampSelectedIndex(index: number | null, length: number): number | null {
  if (length === 0) {
    return null
  }
  if (index === null) {
    return 0
  }
  return Math.min(index, length - 1)
}

function normalizeImportedDefinition(
  parsed: Partial<SequenceDefinition>,
): SequenceDefinition {
  const steps = Array.isArray(parsed.steps)
    ? parsed.steps.map((step) => ({
        id: step.id ?? generateStepId(),
        enabled: step.enabled ?? true,
        label: step.label ?? '',
        target: step.target ?? null,
        execute_params: step.execute_params ?? null,
        start_at: step.start_at ?? null,
        end_at: step.end_at ?? null,
        notes: step.notes ?? '',
      }))
    : []

  return {
    version: parsed.version ?? 1,
    name: parsed.name ?? defaultDefinition.name,
    mode: parsed.mode ?? defaultDefinition.mode,
    steps,
  }
}

export const useSequenceStore = create<SequenceStore>()(
  persist(
    (set, get) => {
      const beginRequest = () => {
        set((state) => ({
          ui: {
            ...state.ui,
            pendingRequests: state.ui.pendingRequests + 1,
            errorMessage: null,
          },
        }))
      }

      const finishRequest = () => {
        set((state) => ({
          ui: {
            ...state.ui,
            pendingRequests: Math.max(0, state.ui.pendingRequests - 1),
          },
        }))
      }

      const setError = (error: unknown) => {
        set((state) => ({
          ui: {
            ...state.ui,
            errorMessage:
              error instanceof Error
                ? error.message
                : t(state.language, 'unexpectedError'),
          },
        }))
      }

      const mutateDefinition = (
        updater: (definition: SequenceDefinition) => SequenceDefinition,
      ) => {
        set((state) => ({
          definition: updater(cloneDefinition(state.definition)),
          issues: [],
          ui: {
            ...state.ui,
            confirmStop: false,
          },
        }))
      }

      const applyStatus = (status: SequenceStatusResponse) => {
        const shouldPoll =
          isManualExecutionActive(status.manual_execution) ||
          isSequenceActive(status.sequence)
        set((state) => ({
          controllerStatus: status.controller,
          manualExecutionStatus: status.manual_execution,
          sequenceSnapshot: status.sequence,
          ui: {
            ...state.ui,
            pollingEnabled: shouldPoll,
          },
        }))
      }

      return {
        language: 'ja',
        meta: null,
        definition: defaultDefinition,
        selectedStepIndex: null,
        controllerStatus: null,
        manualExecutionStatus: null,
        sequenceSnapshot: null,
        issues: [],
        ui: {
          pendingRequests: 0,
          errorMessage: null,
          confirmStop: false,
          pollingEnabled: false,
        },

        setLanguage(language) {
          set({ language })
        },

        async loadMeta() {
          beginRequest()
          try {
            const meta = await sequenceApi.getMeta()
            set((state) => ({
              meta,
              definition: {
                ...state.definition,
                mode: meta.sequence_modes.includes(state.definition.mode)
                  ? state.definition.mode
                  : (meta.sequence_modes[0] ?? state.definition.mode),
              },
            }))
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        async refreshStatus() {
          try {
            const status = await sequenceApi.getStatus()
            applyStatus(status)
          } catch (error) {
            setError(error)
          }
        },

        async connect(address) {
          beginRequest()
          try {
            await sequenceApi.connect(address)
            await get().refreshStatus()
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        async disconnect() {
          beginRequest()
          try {
            await sequenceApi.disconnect()
            await get().refreshStatus()
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        async idle() {
          beginRequest()
          try {
            await sequenceApi.idle()
            await get().refreshStatus()
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        async changeTarget(target) {
          beginRequest()
          try {
            await sequenceApi.change(target)
            await get().refreshStatus()
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        setSequenceName(name) {
          mutateDefinition((definition) => ({ ...definition, name }))
        },

        setSequenceMode(mode) {
          mutateDefinition((definition) => ({ ...definition, mode }))
        },

        selectStep(index) {
          set({ selectedStepIndex: index })
        },

        addStep() {
          const meta = get().meta
          set((state) => {
            const nextSteps = [...state.definition.steps, createEmptyStep(meta)]
            return {
              definition: { ...state.definition, steps: nextSteps },
              selectedStepIndex: nextSteps.length - 1,
              issues: [],
              ui: { ...state.ui, confirmStop: false },
            }
          })
        },

        duplicateStep(index) {
          set((state) => {
            const step = state.definition.steps[index]
            if (step === undefined) {
              return state
            }
            const duplicated: SequenceStep = {
              ...JSON.parse(JSON.stringify(step)),
              id: generateStepId(),
            }
            const nextSteps = [...state.definition.steps]
            nextSteps.splice(index + 1, 0, duplicated)
            return {
              definition: { ...state.definition, steps: nextSteps },
              selectedStepIndex: index + 1,
              issues: [],
              ui: { ...state.ui, confirmStop: false },
            }
          })
        },

        deleteStep(index) {
          set((state) => {
            const nextSteps = state.definition.steps.filter((_, itemIndex) => itemIndex !== index)
            return {
              definition: { ...state.definition, steps: nextSteps },
              selectedStepIndex: clampSelectedIndex(state.selectedStepIndex, nextSteps.length),
              issues: [],
              ui: { ...state.ui, confirmStop: false },
            }
          })
        },

        reorderSteps(activeId, overId) {
          if (activeId === overId) {
            return
          }
          set((state) => {
            const oldIndex = state.definition.steps.findIndex((step) => step.id === activeId)
            const newIndex = state.definition.steps.findIndex((step) => step.id === overId)
            if (oldIndex < 0 || newIndex < 0) {
              return state
            }
            const nextSteps = arrayMove(state.definition.steps, oldIndex, newIndex)
            // ---選択ステップを更新する
            let nextSelected = state.selectedStepIndex
            if (state.selectedStepIndex === oldIndex) {
              // 既存の選択ステップが移動対象だった場合、移動先の新しい位置にする
              nextSelected = newIndex
            } else if (
              state.selectedStepIndex !== null &&
              oldIndex < state.selectedStepIndex &&
              state.selectedStepIndex <= newIndex
            ) {
              // 既存の選択ステップが、移動前 - 移動先の間にある場合、1つ前にする
              nextSelected = state.selectedStepIndex - 1
            } else if (
              state.selectedStepIndex !== null &&
              newIndex <= state.selectedStepIndex &&
              state.selectedStepIndex < oldIndex
            ) {
              // 既存の選択ステップが、移動前 - 移動後の間にない場合、1つ後にする
              nextSelected = state.selectedStepIndex + 1
            }

            return {
              definition: { ...state.definition, steps: nextSteps },
              selectedStepIndex: nextSelected,
              issues: [],
            }
          })
        },

        updateStep(index, patch) {
          set((state) => {
            const nextSteps = [...state.definition.steps]
            const current = nextSteps[index]
            if (current === undefined) {
              return state
            }
            nextSteps[index] = { ...current, ...patch }
            return {
              definition: { ...state.definition, steps: nextSteps },
              issues: [],
              ui: { ...state.ui, confirmStop: false },
            }
          })
        },

        updateStepExecuteField(index, name, value) {
          set((state) => {
            const nextSteps = [...state.definition.steps]
            const current = nextSteps[index]
            if (current === undefined) {
              return state
            }
            const nextExecuteParams = { ...(current.execute_params ?? {}) }
            if (value === null || value === undefined || value === '') {
              delete nextExecuteParams[name]
            } else {
              nextExecuteParams[name] = value
            }
            nextSteps[index] = {
              ...current,
              execute_params:
                Object.keys(nextExecuteParams).length > 0 ? nextExecuteParams : null,
            }
            return {
              definition: { ...state.definition, steps: nextSteps },
              issues: [],
              ui: { ...state.ui, confirmStop: false },
            }
          })
        },

        resetDefinition() {
          set((state) => ({
            definition: {
              ...defaultDefinition,
              mode: state.meta?.sequence_modes[0] ?? defaultDefinition.mode,
            },
            selectedStepIndex: null,
            issues: [],
            sequenceSnapshot: null,
            ui: {
              ...state.ui,
              errorMessage: null,
              confirmStop: false,
            },
          }))
        },

        importDefinition(jsonText) {
          try {
            const parsed = JSON.parse(jsonText) as Partial<SequenceDefinition>
            const definition = normalizeImportedDefinition(parsed)
            const meta = get().meta
            if (
              meta !== null &&
              !meta.sequence_modes.includes(definition.mode)
            ) {
              throw new Error(t(get().language, 'unsupportedImportedMode'))
            }
            set((state) => ({
              definition,
              selectedStepIndex: definition.steps.length > 0 ? 0 : null,
              issues: [],
              sequenceSnapshot: null,
              ui: {
                ...state.ui,
                errorMessage: null,
                confirmStop: false,
              },
            }))
          } catch (error) {
            setError(error)
          }
        },

        exportDefinition() {
          return JSON.stringify(get().definition, null, 2)
        },

        async validateSequence() {
          beginRequest()
          try {
            const response = await sequenceApi.validateSequence(get().definition)
            set({ issues: response.issues })
            return response.ok
          } catch (error) {
            setError(error)
            return false
          } finally {
            finishRequest()
          }
        },

        async runSequence() {
          beginRequest()
          try {
            const snapshot = await sequenceApi.runSequence(get().definition)
            set((state) => ({
              sequenceSnapshot: snapshot,
              ui: {
                ...state.ui,
                confirmStop: false,
                pollingEnabled: true,
              },
            }))
            await get().refreshStatus()
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        requestStopConfirmation() {
          set((state) => ({
            ui: { ...state.ui, confirmStop: true },
          }))
        },

        cancelStopConfirmation() {
          set((state) => ({
            ui: { ...state.ui, confirmStop: false },
          }))
        },

        async stopSequence() {
          beginRequest()
          try {
            const snapshot = await sequenceApi.stopSequence()
            set((state) => ({
              sequenceSnapshot: snapshot,
              ui: {
                ...state.ui,
                confirmStop: false,
                pollingEnabled: true,
              },
            }))
            await get().refreshStatus()
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        async runStepCheck(index) {
          beginRequest()
          try {
            await sequenceApi.checkStep(get().definition, index)
            set((state) => ({
              selectedStepIndex: index,
              ui: { ...state.ui, pollingEnabled: true },
            }))
            await get().refreshStatus()
          } catch (error) {
            setError(error)
          } finally {
            finishRequest()
          }
        },

        clearError() {
          set((state) => ({
            ui: { ...state.ui, errorMessage: null },
          }))
        },
      }
    },
    {
      name: 'stationkit-sequence-store',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        language: state.language,
        definition: state.definition,
      }),
    },
  ),
)
