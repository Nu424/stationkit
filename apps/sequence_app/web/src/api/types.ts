export type PrimitiveFieldType = 'str' | 'int' | 'float' | 'bool' | 'json'
export type InputFormKind = 'none' | 'field' | 'fields' | 'json'
export type SequenceMode = 'COMPLETION_DRIVEN' | 'TIME_DRIVEN'

export interface FieldMeta {
  name: string
  label: string
  type: PrimitiveFieldType
  required: boolean
  default: unknown
  nullable: boolean
}

export interface InputMeta {
  kind: InputFormKind
  accepts_params: boolean
  required: boolean
  fields: FieldMeta[]
}

export interface CustomActionMeta {
  name: string
  description: string
  input: InputMeta
}

export interface SequenceMetaResponse {
  controller_name: string
  target: InputMeta
  execute: InputMeta
  sequence_modes: SequenceMode[]
  custom_actions: CustomActionMeta[]
}

export interface SequenceIssue {
  severity: string
  code: string
  message: string
  step_id?: string | null
  step_index?: number | null
}

export interface SequenceValidationResponse {
  ok: boolean
  issues: SequenceIssue[]
}

export interface ControllerStatus {
  controller_state: string
  current_target?: unknown
  call_log?: string[]
  [key: string]: unknown
}

export interface ExecutionHandle {
  execution_id: string
}

export interface ExecutionStatus {
  execution_id: string
  state: string
  started_at: string
  finished_at: string | null
  result: unknown | null
  error_message: string | null
  cancel_requested: boolean
}

export interface SequenceStep {
  id: string
  enabled: boolean
  label: string
  target: unknown
  execute_params: Record<string, unknown> | null
  start_at: string | null
  end_at: string | null
  notes: string
}

export interface SequenceDefinition {
  version: number
  name: string
  mode: SequenceMode
  steps: SequenceStep[]
}

export interface SequenceStepStatus extends SequenceStep {
  state: string
  message: string | null
  countdown_text: string | null
  execution_id: string | null
  started_at: string | null
  finished_at: string | null
  result: unknown | null
  error_message: string | null
  cancel_requested: boolean
}

export interface SequenceSnapshot {
  run_id: string | null
  sequence_name: string
  mode: SequenceMode
  state: string
  started_at: string | null
  finished_at: string | null
  current_step_index: number | null
  stop_requested: boolean
  message: string | null
  issues: SequenceIssue[]
  steps: SequenceStepStatus[]
}

export interface SequenceStatusResponse {
  controller: ControllerStatus
  manual_execution: ExecutionStatus | null
  sequence: SequenceSnapshot | null
}
