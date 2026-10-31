import { useEffect, useState, type ReactNode } from 'react'
import {
  adminToken,
  adminWrite,
  getPendingRules,
  getQuarantine,
  getReviewQueue,
  getStatus,
  setAdminToken,
  type QuarantineItem,
  type ReviewItem,
  type RuleProposal,
  type Status,
} from './api'

type Outcome = { text: string; tone: 'ok' | 'warn' | 'fail' }
type Triple = { subject: string; relation: string; object: string }

const rel = (r: string) => r.replaceAll('_', ' ')
const when = (t: number) => new Date(t * 1000).toLocaleString()

function Fact({ f }: { f: Triple }) {
  return (
    <span className="review-fact">
      <b>{f.subject}</b> <i>{rel(f.relation)}</i> <b>{f.object}</b>
    </span>
  )
}

function Section({ title, note, count, children }: { title: string; note: string; count: number | null; children: ReactNode }) {
  return (
    <section className="card review-section">
      <div className="card-head">
        <h2>
          {title} {count !== null && <span className="review-count">{count}</span>}
        </h2>
      </div>
      <p className="card-note">{note}</p>
      {children}
    </section>
  )
}

function Empty({ text }: { text: string }) {
  return <p className="review-empty">{text}</p>
}

export default function ReviewView() {
  const [status, setStatus] = useState<Status | null>(null)
  const [queue, setQueue] = useState<ReviewItem[] | null>(null)
  const [quarantine, setQuarantine] = useState<QuarantineItem[] | null>(null)
  const [rules, setRules] = useState<RuleProposal[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [outcomes, setOutcomes] = useState<Record<string, Outcome>>({})
  const [token, setToken] = useState(adminToken)
  const [tokenDraft, setTokenDraft] = useState('')
  const [ruleEdits, setRuleEdits] = useState<Record<string, { functional: boolean; transitive: boolean }>>({})

  const load = () =>
    Promise.all([getStatus(), getReviewQueue(), getQuarantine(), getPendingRules()])
      .then(([s, q, qu, r]) => {
        setError('')
        setStatus(s)
        setQueue(q)
        setQuarantine(qu)
        setRules(r)
        setOutcomes({})
      })
      .catch((e) => setError(`Could not load the review lists: ${String(e)}`))

  useEffect(() => {
    load()
  }, [])

  const writes = status?.writes ?? 'open'
  const canWrite = writes === 'open' || (writes === 'token' && token !== '')

  async function act(key: string, url: string, method: 'POST' | 'DELETE', body: unknown, done: (res: Record<string, unknown>) => Outcome) {
    setBusy(key)
    try {
      const res = await adminWrite(url, method, body)
      setOutcomes((o) => ({ ...o, [key]: done(res) }))
    } catch (e) {
      const msg = String(e).replace(/^Error: /, '')
      setOutcomes((o) => ({ ...o, [key]: { text: msg.includes('token') ? `${msg} - enter the admin token above.` : msg, tone: 'fail' } }))
    } finally {
      setBusy(null)
    }
  }

  const accepted = (res: Record<string, unknown>): Outcome =>
    res.status === 'accepted'
      ? { text: res.created === false ? 'Already in the graph.' : 'Added to the graph.', tone: 'ok' }
      : res.status === 'quarantined'
        ? { text: `Not added - the write gate quarantined it: ${String(res.reason ?? '')}`, tone: 'warn' }
        : { text: 'Not added: that alias already points to another entity.', tone: 'warn' }

  const actions = (k: string, buttons: ReactNode) => {
    const o = outcomes[k]
    if (o) return <p className={`review-outcome ${o.tone}`}>{o.text}</p>
    if (!canWrite) return null
    return <div className="review-actions">{buttons}</div>
  }

  const button = (k: string, label: string, onClick: () => void, primary = false) => (
    <button type="button" className={primary ? 'review-primary' : 'ghost'} disabled={busy !== null} onClick={onClick}>
      {busy === k ? '…' : label}
    </button>
  )

  return (
    <div className="metrics review">
      <div className="metrics-head">
        <div>
          <h1>Review</h1>
          <p className="card-note">
            Nothing the model or Wikidata confirms is written to the knowledge graph on its own. It waits here until you
            accept it, and so does anything the write gate stopped.
          </p>
        </div>
        <button type="button" className="ghost" onClick={load} disabled={busy !== null}>
          Refresh
        </button>
      </div>

      {writes === 'closed' && (
        <p className="review-banner">This deployment is read-only (judge mode): you can see what is waiting, but not change it.</p>
      )}
      {writes === 'token' && (
        <form
          className="review-banner review-token"
          onSubmit={(e) => {
            e.preventDefault()
            setAdminToken(tokenDraft.trim())
            setToken(tokenDraft.trim())
            setTokenDraft('')
          }}
        >
          <span>{token ? 'Admin token set for this tab.' : 'Changes need the admin token (ADMIN_TOKEN on the server).'}</span>
          <input
            type="password"
            value={tokenDraft}
            onChange={(e) => setTokenDraft(e.target.value)}
            placeholder={token ? 'Replace token' : 'Admin token'}
            aria-label="Admin token"
            autoComplete="off"
          />
          <button type="submit" className="ghost" disabled={!tokenDraft.trim()}>
            Use
          </button>
          {token && (
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setAdminToken('')
                setToken('')
              }}
            >
              Forget
            </button>
          )}
        </form>
      )}
      {error && <p className="error">{error}</p>}

      <Section
        title="Verified outside the graph"
        count={queue?.length ?? null}
        note="Facts an answer used after Wikidata or the independent model checks confirmed them. Accepting one sends it through the write gate into the graph, so future answers can use it directly."
      >
        {queue === null ? (
          <Empty text="Loading…" />
        ) : queue.length === 0 ? (
          <Empty text="Nothing waiting." />
        ) : (
          queue.map((item) => {
            const k = `q${item.id}`
            return (
              <div className="review-row" key={k}>
                <div className="review-main">
                  <Fact f={item} />
                  <small>
                    {item.reason} · from {item.source ?? 'unknown'} · {when(item.created_at)}
                  </small>
                </div>
                {actions(k, <>
                  {button(k, 'Accept into graph', () => act(k, `/api/review/${item.id}/accept`, 'POST', undefined, accepted), true)}
                  {button(k, 'Reject', () => act(k, `/api/review/${item.id}/reject`, 'POST', undefined, () => ({ text: 'Rejected.', tone: 'warn' })))}
                </>)}
              </div>
            )
          })
        )}
      </Section>

      <Section
        title="Stopped by the write gate"
        count={quarantine?.length ?? null}
        note="Writes the graph refused: a contradiction, the wrong type of entity, a cycle, or a likely duplicate name. Release writes it anyway; discard drops it."
      >
        {quarantine === null ? (
          <Empty text="Loading…" />
        ) : quarantine.length === 0 ? (
          <Empty text="Nothing quarantined." />
        ) : (
          quarantine.map((item) => {
            const k = `x${item.id}`
            return (
              <div className="review-row" key={k}>
                <div className="review-main">
                  <Fact f={item} />
                  <small className="review-why">{item.reason}</small>
                  <small>
                    from {item.source ?? 'unknown'} · {when(item.created_at)}
                  </small>
                </div>
                {actions(k, <>
                  {button(k, 'Release anyway', () => act(k, `/api/quarantine/${item.id}/release`, 'POST', undefined, () => ({ text: 'Written to the graph.', tone: 'ok' })))}
                  {button(k, 'Discard', () => act(k, `/api/quarantine/${item.id}`, 'DELETE', undefined, () => ({ text: 'Discarded.', tone: 'warn' })), true)}
                </>)}
              </div>
            )
          })
        )}
      </Section>

      <Section
        title="Proposed relation rules"
        count={rules?.length ?? null}
        note="Rules the model proposed for new relations. Single-valued: one value per subject (born in). Transitive: chains imply the whole (located in). A wrong rule makes the graph reject true facts, so check before approving."
      >
        {rules === null ? (
          <Empty text="Loading…" />
        ) : rules.length === 0 ? (
          <Empty text="No rules waiting." />
        ) : (
          rules.map((r) => {
            const k = `r${r.relation}`
            const e = ruleEdits[r.relation] ?? { functional: !!r.functional, transitive: !!r.transitive }
            const set = (patch: Partial<typeof e>) => setRuleEdits((x) => ({ ...x, [r.relation]: { ...e, ...patch } }))
            return (
              <div className="review-row" key={k}>
                <div className="review-main">
                  <span className="review-fact">
                    <i>{rel(r.relation)}</i>
                  </span>
                  <small>
                    proposed with confidence {Math.round(r.confidence * 100)}%{r.rationale ? ` · ${r.rationale}` : ''}
                  </small>
                  <div className="review-flags">
                    <label>
                      <input type="checkbox" checked={e.functional} disabled={!canWrite || !!outcomes[k]}
                        onChange={(ev) => set({ functional: ev.target.checked })} /> single-valued
                    </label>
                    <label>
                      <input type="checkbox" checked={e.transitive} disabled={!canWrite || !!outcomes[k]}
                        onChange={(ev) => set({ transitive: ev.target.checked })} /> transitive
                    </label>
                  </div>
                </div>
                {actions(k, <>
                  {button(k, 'Approve', () => act(k, `/api/rules/${encodeURIComponent(r.relation)}/approve`, 'POST', e,
                      () => ({ text: 'Approved - the graph enforces it from now on.', tone: 'ok' })), true)}
                  {button(k, 'Reject', () => act(k, `/api/rules/${encodeURIComponent(r.relation)}/reject`, 'POST', undefined,
                      () => ({ text: 'Rejected - no rule is enforced for it.', tone: 'warn' })))}
                </>)}
              </div>
            )
          })
        )}
      </Section>
    </div>
  )
}
