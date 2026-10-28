export type Mode = 'auto' | 'conservative' | 'balanced' | 'aggressive'
export type StepStatus = 'running' | 'ok' | 'warn' | 'fail' | 'info'

export type Step = {
  type: 'step'
  id: string
  stage: string
  title: string
  detail: string
  status: StepStatus
}

export type Final = {
  type: 'final'
  tier: 'factual' | 'reasoning'
  mode: Mode | null
  confidence: 'high' | 'medium' | 'low'
  from_cache: boolean
}

export type ChatEvent =
  | Step
  | Final
  | { type: 'thinking_done'; message: string }
  | { type: 'answer'; delta: string }
  | { type: 'error'; message: string }

/** POST the question and hand each server-sent event to onEvent, in order. */
export async function streamChat(question: string, mode: Mode, onEvent: (e: ChatEvent) => Promise<void> | void) {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, mode }),
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
