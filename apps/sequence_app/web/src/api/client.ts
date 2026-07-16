import type {
  ExecutionHandle,
  SequenceDefinition,
  SequenceMetaResponse,
  SequenceSnapshot,
  SequenceStatusResponse,
  SequenceValidationResponse,
} from './types'

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? ''

function buildUrl(path: string): string {
  return `${apiBaseUrl}${path}`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(buildUrl(path), {
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    ...init,
  })

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as
      | { detail?: string }
      | null
    throw new Error(payload?.detail ?? `${response.status} ${response.statusText}`)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return (await response.json()) as T
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export const sequenceApi = {
  getMeta(): Promise<SequenceMetaResponse> {
    return request<SequenceMetaResponse>('/api/meta')
  },

  getStatus(): Promise<SequenceStatusResponse> {
    return request<SequenceStatusResponse>('/api/status')
  },

  connect(address: string): Promise<{ ok: boolean }> {
    return post<{ ok: boolean }>('/api/controller/connect', { address })
  },

  disconnect(): Promise<{ ok: boolean }> {
    return post<{ ok: boolean }>('/api/controller/disconnect')
  },

  idle(): Promise<{ ok: boolean }> {
    return post<{ ok: boolean }>('/api/controller/idle')
  },

  change(target: unknown): Promise<{ ok: boolean }> {
    return post<{ ok: boolean }>('/api/controller/change', { target })
  },

  validateSequence(
    definition: SequenceDefinition,
  ): Promise<SequenceValidationResponse> {
    return post<SequenceValidationResponse>('/api/sequence/validate', {
      definition,
    })
  },

  runSequence(definition: SequenceDefinition): Promise<SequenceSnapshot> {
    return post<SequenceSnapshot>('/api/sequence/run', { definition })
  },

  stopSequence(): Promise<SequenceSnapshot> {
    return post<SequenceSnapshot>('/api/sequence/stop')
  },

  checkStep(
    definition: SequenceDefinition,
    stepIndex: number,
  ): Promise<ExecutionHandle> {
    return post<ExecutionHandle>('/api/sequence/check-step', {
      definition,
      step_index: stepIndex,
    })
  },
}
