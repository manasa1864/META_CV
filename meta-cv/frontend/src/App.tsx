import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { sendFeedback, streamChat, type ChatEvent, type Final, type Mode, type Step } from './api'
import MetricsView from './Metrics'
import './App.css'

type Phase = 'thinking' | 'answering' | 'done' | 'error'

type Message =
  | { id: number; role: 'user'; text: string }
  | {
      id: number
      role: 'assistant'
      steps: Step[]
      phase: Phase
      answer: string
      final?: Final
      error?: string
      showThinking: boolean
      feedback?: boolean
    }

const STAGES: Record<string, string> = {
  cache: 'Verified-path cache',
  router: 'Hallucination risk & routing',
  states: 'Reused reasoning',
  consistency: 'Conversation consistency',
  graph: 'Knowledge graph',
  factual: 'Factual verification',
  reasoning: 'Reasoning checks',
  compose: 'Composing',
}

const newConversationId = () => crypto.randomUUID()

function Meta({ final }: { final: Final }) {
  const [open, setOpen] = useState(false)
  const parts = [
    `risk: ${final.hallucination_type}`,
    `tier: ${final.tier}${final.mode ? ` (${final.mode})` : ''}${final.router?.learned ? ' · learned route' : ''}`,
    `confidence: ${final.confidence}`,
    final.from_cache ? `reused verified path · ${final.tokens.saved} tokens saved` : `${final.tokens.total} tokens`,
  ]
  if (final.reused_state) parts.push(`adapted a similar verified answer`)
  return (
    <div className="meta">
      {parts.join(' · ')}
      {final.standalone && <div>answered as: “{final.standalone}”</div>}
      {(final.caught.length > 0 || final.withheld > 0) && (
        <>
          {' '}
          <button type="button" className="link" onClick={() => setOpen(!open)}>
            {final.caught.length} hallucination{final.caught.length === 1 ? '' : 's'} caught
            {final.withheld ? `, ${final.withheld} unverified claim${final.withheld === 1 ? '' : 's'} withheld` : ''}
          </button>
          {open && (
            <ul className="caught">
              {final.caught.map((c, i) => (
                <li key={i}>
                  <span className={`tag ${c.action}`}>
                    {c.type} · {c.action}
                  </span>{' '}
                  {c.detail}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}

const ICONS: Record<Step['status'], string> = { running: '', ok: '✓', warn: '!', fail: '✗', info: '•' }

const STEP_GAP_MS = 220 // reveal new thinking steps one by one
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function inline(line: string): ReactNode[] {
  return line.split(/(\*\*[^*]+\*\*|_[^_]+_)/g).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={i}>{part.slice(2, -2)}</strong>
    if (part.length > 2 && part.startsWith('_') && part.endsWith('_')) return <em key={i}>{part.slice(1, -1)}</em>
    return part
  })
}

function Text({ text }: { text: string }) {
  return (
    <>
      {text.split('\n').map((line, i) => (
        <p key={i}>{line ? inline(line) : ' '}</p>
      ))}
    </>
  )
}

function Thinking({ steps, phase }: { steps: Step[]; phase: Phase }) {
  return (
    <div className="thinking">
      {steps.map((s, i) => {
        const header = s.stage !== steps[i - 1]?.stage ? STAGES[s.stage] ?? s.stage : null
        return (
          <div key={s.id}>
            {header && <div className="stage">{header}</div>}
            <div className={`step ${s.status}`}>
              <span className="icon">{s.status === 'running' ? <span className="spinner" /> : ICONS[s.status]}</span>
              <div>
                <div className="step-title">{s.title}</div>
                {s.detail && <div className="step-detail">{s.detail}</div>}
              </div>
            </div>
          </div>
        )
      })}
      {phase === 'thinking' && steps.length === 0 && <div className="step-detail">Starting…</div>}
      {phase !== 'thinking' && phase !== 'error' && <div className="complete">✓ Processing complete</div>}
    </div>
  )
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [mode, setMode] = useState<Mode>('auto')
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState<'chat' | 'metrics'>('chat')
  const [conversationId, setConversationId] = useState(newConversationId)
  const nextId = useRef(1)
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [messages])

  const update = (id: number, fn: (m: Extract<Message, { role: 'assistant' }>) => Partial<Message>) =>
    setMessages((prev) => prev.map((m) => (m.id === id && m.role === 'assistant' ? ({ ...m, ...fn(m) } as Message) : m)))

  async function send(e: FormEvent) {
    e.preventDefault()
    const question = input.trim()
    if (!question || busy) return
    const qid = nextId.current++
    const aid = nextId.current++
    setMessages((prev) => [
      ...prev,
      { id: qid, role: 'user', text: question },
      { id: aid, role: 'assistant', steps: [], phase: 'thinking', answer: '', showThinking: false },
    ])
    setInput('')
    setBusy(true)

    const seen = new Set<string>()
    const onEvent = async (ev: ChatEvent) => {
      switch (ev.type) {
        case 'step': {
          const isNew = !seen.has(ev.id)
          seen.add(ev.id)
          update(aid, (m) => ({
            steps: isNew ? [...m.steps, ev] : m.steps.map((s) => (s.id === ev.id ? ev : s)),
          }))
          if (isNew) await sleep(STEP_GAP_MS)
          break
        }
        case 'thinking_done':
          update(aid, () => ({ phase: 'answering' }))
          await sleep(400)
          break
        case 'answer':
          update(aid, (m) => ({ answer: m.answer + ev.delta }))
          break
        case 'final':
          update(aid, () => ({ phase: 'done', final: ev }))
          break
        case 'error':
          update(aid, () => ({ phase: 'error', error: ev.message }))
          break
      }
    }

    try {
      await streamChat(question, mode, conversationId, onEvent)
    } catch (err) {
      update(aid, () => ({ phase: 'error', error: String(err) }))
    } finally {
      update(aid, (m) => (m.phase === 'answering' ? { phase: 'done' } : {}))
      setBusy(false)
    }
  }

  async function rate(id: number, turnId: number, helpful: boolean) {
    try {
      await sendFeedback(turnId, helpful)
      update(id, () => ({ feedback: helpful }))
    } catch (err) {
      update(id, () => ({ error: `Feedback not saved: ${String(err)}` }))
    }
  }

  function newChat() {
    setMessages([])
    setConversationId(newConversationId())
  }

  return (
    <div className={`app ${view === 'metrics' ? 'wide' : ''}`}>
      <header>
        <h1>META</h1>
        <nav className="tabs">
          <button type="button" className={view === 'chat' ? 'active' : ''} onClick={() => setView('chat')}>
            Chat
          </button>
          <button type="button" className={view === 'metrics' ? 'active' : ''} onClick={() => setView('metrics')}>
            Metrics
          </button>
        </nav>
        {view === 'chat' && (
          <div className="controls">
            <label>
              Reasoning mode{' '}
              <select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
                <option value="auto">auto (balanced)</option>
                <option value="conservative">conservative</option>
                <option value="balanced">balanced</option>
                <option value="aggressive">aggressive</option>
              </select>
            </label>
            <button type="button" className="ghost" onClick={newChat} disabled={busy}>
              New chat
            </button>
          </div>
        )}
      </header>

      {view === 'metrics' && <MetricsView />}

      <main hidden={view !== 'chat'}>
        {messages.length === 0 && (
          <div className="empty">Ask about a person, a book and its characters, or give a problem to solve.</div>
        )}
        {messages.map((m) =>
          m.role === 'user' ? (
            <div key={m.id} className="msg user">
              {m.text}
            </div>
          ) : (
            <div key={m.id} className="msg assistant">
              <div className="status-row">
                {m.phase === 'thinking' && (
                  <span className="status">
                    <span className="spinner" /> Thinking…
                  </span>
                )}
                {(m.phase === 'answering' || m.phase === 'done') && <span className="status ok">Processing complete</span>}
                <button
                  type="button"
                  className="see-thinking"
                  onClick={() => update(m.id, (a) => ({ showThinking: !a.showThinking }))}
                >
                  {m.showThinking ? 'Hide the thinking' : 'See the thinking'} ({m.steps.length})
                </button>
              </div>
              {m.showThinking && <Thinking steps={m.steps} phase={m.phase} />}
              {m.answer && (
                <div className="answer">
                  <Text text={m.answer} />
                </div>
              )}
              {m.error && <div className="error">{m.error}</div>}
              {m.final && <Meta final={m.final} />}
              {m.final && (
                <div className="feedback">
                  {m.feedback === undefined ? (
                    <>
                      Was this right?
                      <button type="button" className="ghost" onClick={() => rate(m.id, m.final!.turn_id, true)}>
                        Yes
                      </button>
                      <button type="button" className="ghost" onClick={() => rate(m.id, m.final!.turn_id, false)}>
                        No
                      </button>
                    </>
                  ) : (
                    <span>Thanks - the router will learn from this.</span>
                  )}
                </div>
              )}
            </div>
          ),
        )}
        <div ref={bottom} />
      </main>

      <form onSubmit={send} hidden={view !== 'chat'}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask something…"
          disabled={busy}
          autoFocus
        />
        <button type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  )
}
