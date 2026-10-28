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

export type Caught = { type: HType; action: 'corrected' | 'dropped' | 'flagged'; detail: string }

export type Final = {
  type: 'final'
  turn_id: number
  hallucination_type: HType
  tier: HType
  mode: Mode | null
  confidence: 'high' | 'medium' | 'low'
  from_cache: boolean
  reused_state: { question: string; similarity: number } | null
  standalone: string | null
  caught: Caught[]
  withheld: number
  tokens: { prompt: number; completion: number; total: number; calls: number; saved: number }
  router: { scores: Record<string, number>; learned: boolean; evidence: number } | null
}

export type ChatEvent =
  | Step
  | Final
  | { type: 'thinking_done'; message: string }
  | { type: 'answer'; delta: string }
  | { type: 'error'; message: string }

/** POST the question and hand each server-sent event to onEvent, in order. */
export async function streamChat(
  question: string,
  mode: Mode,
  conversationId: string,
  onEvent: (e: ChatEvent) => Promise<void> | void,
) {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, mode, conversation_id: conversationId }),
  })
  if (!res.ok || !res.body) throw new Error(`server returned ${res.status}`)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
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
      if (data) await onEvent(JSON.parse(data) as ChatEvent)
    }
  }
}

export async function sendFeedback(turnId: number, helpful: boolean) {
  const res = await fetch('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ turn_id: turnId, helpful }),
  })
  if (!res.ok) throw new Error(`server returned ${res.status}`)
}

type Rates = { n: number; accuracy?: number; hallucination_rate?: number; abstain_rate?: number; avg_tokens?: number }
type BenchSide = { overall: Rates; by_type: Record<HType, Rates> }

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
  }
  caught_by_type: { type: HType; corrected: number; dropped: number; flagged: number }[]
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
    avg_tokens: number
    score: number
  }[]
  timeline: { n: number; rolling_success: number | null; rolling_tokens: number | null; cached: boolean }[]
  bench: {
    created_at: number
    items: number
    baseline: BenchSide | null
    system: BenchSide
    taxonomy_accuracy: number
    cache_hits: number
    reused_states: number
    training: {
      curve: { n: number; learned: number; static: number }[]
      per_tier: Record<HType, Record<string, number>>
    } | null
  } | null
}

export async function getMetrics(): Promise<Metrics> {
  const res = await fetch('/api/metrics')
  if (!res.ok) throw new Error(`server returned ${res.status}`)
  return res.json()
}
