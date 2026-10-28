import { useEffect, useState, type ReactNode } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { getMetrics, type HType, type Metrics } from './api'

const LIGHT = {
  s1: '#2a78d6', s2: '#eb6834', s3: '#1baf7a', muted: '#898781',
  grid: '#e1e0d9', axis: '#c3c2b7', ink: '#52514e', surface: '#ffffff',
}
const DARK = {
  s1: '#3987e5', s2: '#d95926', s3: '#199e70', muted: '#898781',
  grid: '#2c2c2a', axis: '#383835', ink: '#c3c2b7', surface: '#1a1d1f',
}

function usePalette() {
  const query = '(prefers-color-scheme: dark)'
  const [dark, setDark] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = (e: MediaQueryListEvent) => setDark(e.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])
  return dark ? DARK : LIGHT
}

const TYPES: HType[] = ['factual', 'reasoning', 'consistency']
const pct = (x: number | null | undefined) => (x == null ? '-' : `${Math.round(x * 100)}%`)
const num = (x: number | null | undefined) => (x == null ? '-' : Math.round(x).toLocaleString())

function Card({ title, note, table, children }: { title: string; note?: string; table: ReactNode; children: ReactNode }) {
  const [asTable, setAsTable] = useState(false)
  return (
    <section className="card">
      <div className="card-head">
        <h2>{title}</h2>
        <button type="button" className="link" onClick={() => setAsTable(!asTable)}>
          {asTable ? 'Chart' : 'Table'}
        </button>
      </div>
      {note && <p className="card-note">{note}</p>}
      {asTable ? <div className="table-wrap">{table}</div> : children}
    </section>
  )
}

function Table({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <table>
      <thead>
        <tr>{head.map((h) => <th key={h}>{h}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>
        ))}
      </tbody>
    </table>
  )
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  )
}

export default function MetricsView() {
  const p = usePalette()
  const [m, setM] = useState<Metrics | null>(null)
  const [error, setError] = useState('')

  const load = () =>
    getMetrics()
      .then((d) => {
        setM(d)
        setError('')
      })
      .catch((e) => setError(String(e)))

  useEffect(() => {
    load()
  }, [])

  if (error && !m) return <div className="metrics"><p className="error">{error}</p></div>
  if (!m) return <div className="metrics"><p className="card-note">Loading…</p></div>

  const axis = { stroke: p.axis, tick: { fill: p.muted, fontSize: 12 }, tickLine: false }
  const grid = <CartesianGrid stroke={p.grid} vertical={false} />
  const tooltip = (
    <Tooltip
      contentStyle={{ background: p.surface, border: `1px solid ${p.grid}`, borderRadius: 8, fontSize: 13 }}
      labelStyle={{ color: p.ink }}
      cursor={{ fill: p.grid, opacity: 0.4 }}
    />
  )
  const legend = <Legend wrapperStyle={{ fontSize: 12, color: p.ink }} iconType="circle" iconSize={8} />
  const t = m.totals
  const bench = m.bench
  const benchRows = bench
    ? [...TYPES, 'overall' as const].map((type) => {
        const pick = (side: 'baseline' | 'system') => {
          const s = bench[side]
          return s ? (type === 'overall' ? s.overall : s.by_type[type]) : undefined
        }
        return { type, baseline: pick('baseline'), system: pick('system') }
      })
    : []
  const curve = bench?.training?.curve ?? []

  return (
    <div className="metrics">
      <div className="metrics-head">
        <p className="card-note">
          Live numbers come from every answered question. Benchmark numbers come from{' '}
          <code>python -m app.bench</code>
          {bench ? `, last run ${new Date(bench.created_at * 1000).toLocaleString()}` : ' (not run yet)'}.
        </p>
        <button type="button" className="ghost" onClick={load}>
          Refresh
        </button>
      </div>

      <div className="tiles">
        <Tile label="Questions answered" value={num(t.queries)} sub={`${num(t.cache_hits)} from verified cache`} />
        <Tile label="Hallucinations caught" value={num(t.caught)} sub="before they reached an answer" />
        <Tile label="Fully verified" value={pct(t.success_rate)} sub="or rated right by you / the benchmark" />
        <Tile label="Avg tokens / question" value={num(t.avg_tokens)} sub={`${num(t.tokens_saved)} saved by reuse`} />
        <Tile label="Similar-answer reuse" value={num(t.reused_states)} sub="answers adapted a verified state" />
      </div>

      <div className="grid">
        <Card
          title="Benchmark: hallucination rate by type"
          note={
            bench
              ? `Share of ${bench.items} graded questions answered wrongly (declining to answer is not counted as a hallucination). Taxonomy classifier accuracy: ${pct(bench.taxonomy_accuracy)}.`
              : 'Run the benchmark to compare a plain LLM with this system.'
          }
          table={
            <Table
              head={['type', 'baseline acc', 'baseline halluc.', 'system acc', 'system halluc.', 'system abstain', 'tokens base / sys']}
              rows={benchRows.map((r) => [
                r.type,
                pct(r.baseline?.accuracy),
                pct(r.baseline?.hallucination_rate),
                pct(r.system?.accuracy),
                pct(r.system?.hallucination_rate),
                pct(r.system?.abstain_rate),
                `${num(r.baseline?.avg_tokens)} / ${num(r.system?.avg_tokens)}`,
              ])}
            />
          }
        >
          {bench ? (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart
                data={benchRows.map((r) => ({
                  type: r.type,
                  'plain LLM': r.baseline?.hallucination_rate != null ? r.baseline.hallucination_rate * 100 : null,
                  'this system': r.system?.hallucination_rate != null ? r.system.hallucination_rate * 100 : null,
                }))}
                barGap={2}
              >
                {grid}
                <XAxis dataKey="type" {...axis} />
                <YAxis {...axis} unit="%" width={44} />
                {tooltip}
                {legend}
                <Bar dataKey="plain LLM" fill={p.muted} radius={[4, 4, 0, 0]} maxBarSize={28} />
                <Bar dataKey="this system" fill={p.s1} radius={[4, 4, 0, 0]} maxBarSize={28} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="empty-chart">No benchmark run yet.</p>
          )}
        </Card>

        <Card
          title="Learned routing accuracy"
          note={
            curve.length
              ? 'Replaying the benchmark in order, learning from scratch: how often the router picked a tier that answered correctly, vs. always using the specialist tier.'
              : 'Run python -m app.bench --train to measure how routing improves with evidence.'
          }
          table={<Table head={['items seen', 'learned router', 'specialist only']} rows={curve.map((c) => [c.n, pct(c.learned), pct(c.static)])} />}
        >
          {curve.length ? (
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={curve.map((c) => ({ n: c.n, 'learned router': c.learned * 100, 'specialist only': c.static * 100 }))}>
                {grid}
                <XAxis dataKey="n" {...axis} />
                <YAxis {...axis} unit="%" domain={[0, 100]} width={44} />
                {tooltip}
                {legend}
                <Line dataKey="learned router" stroke={p.s1} strokeWidth={2} dot={false} />
                <Line dataKey="specialist only" stroke={p.muted} strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <p className="empty-chart">No training run yet.</p>
          )}
        </Card>

        <Card
          title="Hallucinations caught, by type"
          note="Live: what the checks corrected, dropped or flagged before answering."
          table={
            <Table
              head={['type', 'corrected', 'dropped', 'flagged']}
              rows={m.caught_by_type.map((c) => [c.type, c.corrected, c.dropped, c.flagged])}
            />
          }
        >
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={m.caught_by_type} barGap={2}>
              {grid}
              <XAxis dataKey="type" {...axis} />
              <YAxis {...axis} allowDecimals={false} width={44} />
              {tooltip}
              {legend}
              <Bar dataKey="corrected" fill={p.s1} radius={[4, 4, 0, 0]} maxBarSize={24} />
              <Bar dataKey="dropped" fill={p.s2} radius={[4, 4, 0, 0]} maxBarSize={24} />
              <Bar dataKey="flagged" fill={p.s3} radius={[4, 4, 0, 0]} maxBarSize={24} />
            </BarChart>
          </ResponsiveContainer>
        </Card>

        <Card
          title="Verified answers over time"
          note="Live: rolling share of the last 10 answers that came out fully verified (or were rated right)."
          table={<Table head={['turn', 'rolling verified']} rows={m.timeline.map((r) => [r.n, pct(r.rolling_success)])} />}
        >
          {m.timeline.length ? (
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={m.timeline.map((r) => ({ turn: r.n, verified: r.rolling_success != null ? r.rolling_success * 100 : null }))}>
                {grid}
                <XAxis dataKey="turn" {...axis} />
                <YAxis {...axis} unit="%" domain={[0, 100]} width={44} />
                {tooltip}
                <Line dataKey="verified" stroke={p.s1} strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <p className="empty-chart">No questions answered yet.</p>
          )}
        </Card>

        <Card
          title="Tokens per question over time"
          note="Live: rolling average of the last 10 answers. Cache hits cost 0."
          table={<Table head={['turn', 'rolling tokens']} rows={m.timeline.map((r) => [r.n, num(r.rolling_tokens)])} />}
        >
          {m.timeline.length ? (
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={m.timeline.map((r) => ({ turn: r.n, tokens: r.rolling_tokens }))}>
                {grid}
                <XAxis dataKey="turn" {...axis} />
                <YAxis {...axis} width={52} />
                {tooltip}
                <Line dataKey="tokens" stroke={p.s1} strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <p className="empty-chart">No questions answered yet.</p>
          )}
        </Card>

        <section className="card">
          <div className="card-head">
            <h2>Per-tier performance</h2>
          </div>
          <p className="card-note">Live, excluding cache hits. "Reused" = adapted a similar question's verified state.</p>
          <div className="table-wrap">
            <Table
              head={['tier', 'answers', 'verified', 'avg tokens', 'reused', 'fresh']}
              rows={m.by_tier.map((r) => [
                r.tier,
                r.queries,
                pct(r.success_rate),
                num(r.avg_tokens),
                num(r.avg_tokens_reused),
                num(r.avg_tokens_fresh),
              ])}
            />
          </div>
        </section>

        <section className="card span-2">
          <div className="card-head">
            <h2>Router state</h2>
          </div>
          <p className="card-note">
            Score = observed success rate blended with a prior that favours the specialist, minus a small token cost.
            The highest score per risk type is the tier used next.
          </p>
          <div className="table-wrap">
            <Table
              head={['risk type', 'tier', 'answers', 'successes', 'success rate', 'avg tokens', 'score']}
              rows={m.router.map((r) => {
                const best = Math.max(...m.router.filter((x) => x.type === r.type).map((x) => x.score))
                return [r.type, r.score === best ? `${r.tier} (chosen)` : r.tier, r.n, r.successes, pct(r.success_rate), num(r.avg_tokens), r.score.toFixed(2)]
              })}
            />
          </div>
        </section>
      </div>
    </div>
  )
}
