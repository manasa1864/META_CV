export type Mode = 'auto' | 'conservative' | 'balanced' | 'aggressive'
export type StepStatus = 'running' | 'ok' | 'warn' | 'fail' | 'info'
export type HType = 'factual' | 'reasoning' | 'consistency'

export type Step = {
  type: 'step'
  id: string
  stage: string
  title: string
  detail: string
  status: StepStatus
}

export type Caught = {
  type: HType
  risk?: string
  origin_tier?: string
  action: 'corrected' | 'dropped' | 'flagged'
  detail: string
}

/** One node of the verified path: a graph claim (factual / consistency) or a solution step (reasoning). */
export type PathItem = {
  kind?: 'claim' | 'answer'
  subject?: string
  relation?: string
  object?: string
  status?: 'accepted' | 'rejected' | 'unverified' | 'skipped'
  source?: string | null
  reason?: string
  text?: string
  expression?: string
  result?: string
  question?: string
  final_answer?: string
}

export type Stopped = { reason: string; detail: string }

export type Final = {
  type: 'final'
  turn_id: number
  hallucination_type: HType
  risks?: { label: string; weight: number }[]
  classifier?: 'llm' | 'fallback' | null
  tier: HType
  plan?: string[]
  mode: Mode | null
  confidence: 'high' | 'medium' | 'low'
  from_cache: boolean
  reused_state: { question: string; similarity: number } | null
  standalone: string | null
  caught: Caught[]
  withheld: number
  stopped?: Stopped | null
  degraded?: string | null
  verified_only?: boolean
  tokens: { prompt: number; completion: number; total: number; calls: number; saved: number; graph_calls?: number }
  router: { scores: Record<string, number>; learned: boolean; evidence: number; frozen?: boolean } | null
  premises?: PathItem[]
  path: PathItem[]
}

export type ChatEvent =
  | Step
  | Final
  | { type: 'thinking_done'; message: string }
  | { type: 'answer'; delta: string }
  | { type: 'error'; message: string; code?: string }

/** The connection dropped after the answer started streaming; the caller can offer a retry. */
export class StreamDropped extends Error {}

async function readStream(res: Response, onEvent: (e: ChatEvent) => Promise<void> | void) {
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let ended = false
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let cut
      while ((cut = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, cut)
        buffer = buffer.slice(cut + 2)
        const data = block
          .split('\n')
          .filter((l) => l.startsWith('data: '))
          .map((l) => l.slice(6))
          .join('\n')
        if (!data) continue
        const ev = JSON.parse(data) as ChatEvent
        if (ev.type === 'final' || ev.type === 'error') ended = true
        await onEvent(ev)
      }
    }
  } catch {
    throw new StreamDropped('The connection dropped before the answer finished.')
  }
  if (!ended) throw new StreamDropped('The connection dropped before the answer finished.')
}

/** POST the question and hand each server-sent event to onEvent, in order. A request that fails before
 * anything streamed is retried once; a stream that drops mid-way raises StreamDropped. */
export async function streamChat(
  question: string,
  mode: Mode,
  conversationId: string,
  onEvent: (e: ChatEvent) => Promise<void> | void,
) {
  const send = () =>
    fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, mode, conversation_id: conversationId }),
    })
  let res: Response
  try {
    res = await send()
  } catch {
    await new Promise((r) => setTimeout(r, 800))
    res = await send()
  }
  if (res.status >= 500 && res.status !== 503) {
    await new Promise((r) => setTimeout(r, 800))
    res = await send()
  }
  if (!res.ok || !res.body) {
    let detail = `The server returned ${res.status}.`
    try {
      const body = await res.json()
      detail = body.detail ?? body.message ?? detail
    } catch {
      /* not JSON */
    }
    throw new Error(detail)
  }
  await readStream(res, onEvent)
}

export async function sendFeedback(turnId: number, helpful: boolean) {
  const res = await fetch('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ turn_id: turnId, helpful }),
  })
  if (!res.ok) throw new Error(`server returned ${res.status}`)
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`server returned ${res.status}`)
  return res.json()
}

export type Status = {
  llm: { configured: boolean; breaker_open: boolean }
  degraded: string | null
  health: { verified_only: boolean; reason: string | null }
  judge_mode: boolean
  router_frozen: boolean
  scripted?: boolean
  backend: string
}
export const getStatus = () => getJson<Status>('/api/status')

export type Example = { id: string; label: string; tier: HType; turns: string[]; replay: boolean }
export const getExamples = () => getJson<Example[]>('/api/examples')

export type Replay = {
  id: string
  label: string
  recorded_at: number
  model: string
  backend: string
  turns: { question: string; events: { t: number; event: ChatEvent }[] }[]
}
export const getReplay = (id: string) => getJson<Replay>(`/api/replays/${encodeURIComponent(id)}`)

export type Stats = {
  n: number
  correct?: number
  abstained?: number
  hallucinated?: number
  error?: number
  accuracy?: number
  accuracy_ci?: [number, number] | null
  hallucination_rate?: number
  hallucination_ci?: [number, number] | null
  abstain_rate?: number
  avg_tokens?: number
}

export type ConfigResult = {
  overall: Stats
  by_set: Record<string, Stats>
  by_kind: Record<string, Stats>
  by_type: Record<string, Stats>
  risk_coverage: { min_confidence: string; coverage: number; accuracy_answered: number | null; n_answered: number }[]
}

export type ScaleRow = {
  size: number
  load_s: number
  facts_per_s: number
  validate_supported: { p50_ms?: number; p95_ms?: number }
  validate_transitive: { p50_ms?: number; p95_ms?: number }
  facts_about_exact: { p95_ms?: number }
  relation_stats: { p95_ms?: number }
  audit_s: number
  audit_recall: Record<string, { injected: number; detected: number; recall: number | null }>
  factual_tier: { p50_ms?: number; p95_ms?: number }
}

export type Results = {
  meta?: {
    run_id: string
    created_at: number
    sets: string[]
    items: number
    turns: number
    repeats: number
    backend: string
    model: string
    real_items: number
  }
  configs?: Record<string, ConfigResult>
  routing?: {
    test: { n: number; learned: number | null; learned_ci: number[] | null; static: number | null; static_ci: number[] | null }
    in_sample_curve: { n: number; learned: number; static: number }[]
    per_tier: Record<string, Record<string, number>>
    note: string
  } | null
  caveats?: string[]
  scale?: { backend: string; rows: ScaleRow[]; report: { statement: string }; created_at: number } | null
  taxonomy?: {
    n: number
    accuracy: number
    mode: string
    per_class: Record<string, { precision: number | null; recall: number | null; n: number }>
    confusion: Record<string, Record<string, number>>
    labels_note: string
  } | null
  audit?: { counts: Record<string, number>; by_check: Record<string, number>; ran_at: number } | null
}
export const getResults = () => getJson<Results>('/api/results/latest')

export type Metrics = {
  totals: {
    queries: number
    cache_hits: number
    reused_states: number
    success_rate: number | null
    avg_tokens: number | null
    tokens_spent: number
    tokens_saved: number
    caught: number
    stopped: number
  }
  outcomes: Record<'correct' | 'abstained' | 'hallucinated' | 'unknown', number>
  stops: Record<string, number>
  caught_by_type: { type: HType; corrected: number; dropped: number; flagged: number }[]
  caught_by_risk: { risk: string; label: string; count: number }[]
  by_tier: {
    tier: HType
    queries: number
    success_rate: number | null
    avg_tokens: number | null
    avg_tokens_reused: number | null
    avg_tokens_fresh: number | null
  }[]
  router: {
    type: HType
    tier: HType
    n: number
    successes: number
    success_rate: number | null
    abstain_rate: number | null
    hallucination_rate: number | null
    avg_tokens: number
    score: number
  }[]
  router_frozen: boolean
  timeline: { n: number; rolling_success: number | null; rolling_tokens: number | null; cached: boolean }[]
  health: { verified_only: boolean; reason: string | null; reject_rate: number; error_rate: number }
  graph_health: { counts: Record<string, number>; by_check: Record<string, number>; ran_at: number } | null
  bench: Results | null
}

export const getMetrics = () => getJson<Metrics>('/api/metrics')
