import { useEffect, useState, type ReactNode } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { getResults, type Results, type Stats } from './api'
import { Card, Table } from './charts'
import { usePalette } from './palette'

const pct = (x: number | null | undefined) => (x == null ? '–' : `${Math.round(x * 100)}%`)
const ci = (c: number[] | null | undefined) => (c ? `${Math.round(c[0] * 100)}–${Math.round(c[1] * 100)}%` : '–')
const ms = (x: number | null | undefined) => (x == null ? '–' : `${Math.round(x)} ms`)

const CONFIG_NAMES: Record<string, string> = {
  baseline: 'Plain model',
  full: 'Full system',
  no_graph: 'Without the graph',
  no_verification: 'Without verification',
  static_router: 'Static router',
  no_reuse: 'Without cache / reuse',
  no_guard: 'Without the faithfulness guard',
}

function StatRow(name: string, s: Stats | undefined): (string | number)[] {
  if (!s || !s.n) return [name, '–', '–', '–', '–', 0]
  return [name, pct(s.accuracy), `${pct(s.hallucination_rate)} (${ci(s.hallucination_ci)})`, pct(s.abstain_rate),
    `${pct(s.accuracy)} (${ci(s.accuracy_ci)})`, s.n]
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="results-section">
      <h2>{title}</h2>
      {children}
    </section>
  )
}

export default function ResultsView() {
  const p = usePalette()
  const [r, setR] = useState<Results | null>(null)
  const [state, setState] = useState<'loading' | 'missing' | 'ok'>('loading')

  useEffect(() => {
    getResults()
      .then((d) => {
        setR(d)
        setState('ok')
      })
      .catch(() => setState('missing'))
  }, [])

  if (state === 'loading') return <div className="metrics"><p className="card-note">Loading…</p></div>
  if (state === 'missing' || !r)
    return (
      <div className="metrics results">
        <h1>Results</h1>
        <p className="card-note">
          No measured results yet. Run <code>python -m app.bench</code> (and optionally <code>python -m app.scale</code>,{' '}
          <code>python -m app.bench_taxonomy</code>) to produce them. This page shows measured numbers only.
        </p>
      </div>
    )

  const configs = r.configs ?? {}
  const sets = Array.from(new Set(Object.values(configs).flatMap((c) => Object.keys(c.by_set ?? {}))))
  const axis = { stroke: p.axis, tick: { fill: p.muted, fontSize: 12 }, tickLine: false }
  const tooltip = <Tooltip contentStyle={{ background: p.surface, border: `1px solid ${p.grid}`, borderRadius: 8, fontSize: 13 }} />
  const curve = r.routing?.in_sample_curve ?? []
  const scaleRows = r.scale?.rows ?? []

  return (
    <div className="metrics results">
      <h1>Results</h1>
      {r.meta ? (
        <p className="card-note">
          Benchmark run {new Date(r.meta.created_at * 1000).toLocaleString()}: {r.meta.items} items, {r.meta.turns} graded
          turns, sets {r.meta.sets.join(', ')}, {r.meta.repeats} repeat(s), graph backend {r.meta.backend}
          {r.meta.model ? `, model ${r.meta.model}` : ''}. Each answer is graded correct, abstained (declined or stopped) or
          hallucinated (wrong, or a forbidden claim); intervals are Wilson 95%.
        </p>
      ) : (
        <p className="card-note">No benchmark run yet.</p>
      )}

      {Object.keys(configs).length > 0 && (
        <Section title="Benchmark">
          <div className="table-wrap">
            <Table
              head={['configuration', 'accuracy', 'hallucination rate (95% CI)', 'abstained', 'accuracy (95% CI)', 'n']}
              rows={Object.entries(configs).map(([k, c]) => StatRow(CONFIG_NAMES[k] ?? k, c.overall))}
            />
          </div>
          {sets.map((set) => (
            <div key={set} className="table-wrap">
              <h3>{set} set</h3>
              <Table
                head={['configuration', 'accuracy', 'hallucination rate (95% CI)', 'abstained', 'accuracy (95% CI)', 'n']}
                rows={Object.entries(configs).map(([k, c]) => StatRow(CONFIG_NAMES[k] ?? k, c.by_set?.[set]))}
              />
            </div>
          ))}
          {configs.full && (
            <div className="table-wrap">
              <h3>Full system by question kind</h3>
              <Table
                head={['kind', 'accuracy', 'hallucination rate (95% CI)', 'abstained', 'accuracy (95% CI)', 'n']}
                rows={Object.entries(configs.full.by_kind).map(([k, s]) => StatRow(k, s))}
              />
            </div>
          )}
          {configs.full?.risk_coverage && (
            <div className="table-wrap">
              <h3>Risk–coverage (full system)</h3>
              <Table
                head={['answer only at confidence ≥', 'share answered', 'accuracy on answered', 'n answered']}
                rows={configs.full.risk_coverage.map((x) => [x.min_confidence, pct(x.coverage), pct(x.accuracy_answered), x.n_answered])}
              />
            </div>
          )}
        </Section>
      )}

      {r.routing && (
        <Section title="Learned routing">
          <p className="card-note">
            Held-out test ({r.routing.test.n} turns where some tier answered correctly): learned router{' '}
            {pct(r.routing.test.learned)} ({ci(r.routing.test.learned_ci)}), specialist only {pct(r.routing.test.static)} (
            {ci(r.routing.test.static_ci)}). {r.routing.note}.
          </p>
          <Card
            title="In-sample learning curve"
            note="Learned and scored on the same items, in order; read with the held-out numbers above."
            table={<Table head={['items seen', 'learned', 'specialist only']} rows={curve.map((c) => [c.n, pct(c.learned), pct(c.static)])} />}
          >
            {curve.length ? (
              <ResponsiveContainer width="100%" height={240}>
                <LineChart data={curve.map((c) => ({ n: c.n, learned: c.learned * 100, 'specialist only': c.static * 100 }))}>
                  <CartesianGrid stroke={p.grid} vertical={false} />
                  <XAxis dataKey="n" {...axis} />
                  <YAxis {...axis} unit="%" domain={[0, 100]} width={44} />
                  {tooltip}
                  <Legend wrapperStyle={{ fontSize: 12, color: p.ink }} iconType="circle" iconSize={8} />
                  <Line dataKey="learned" stroke={p.s1} strokeWidth={2} dot={false} />
                  <Line dataKey="specialist only" stroke={p.muted} strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <p className="empty-chart">No training run.</p>
            )}
          </Card>
        </Section>
      )}

      {scaleRows.length > 0 && (
        <Section title={`Graph scale (${r.scale?.backend})`}>
          <p className="card-note">{r.scale?.report.statement}</p>
          <Card
            title="validate p95 latency by graph size"
            note="Timed from the app process, so the network round trip to the graph is included."
            table={
              <Table
                head={['facts', 'load facts/s', 'validate p95', 'transitive p95', 'lookup p95', 'audit', 'factual tier p95']}
                rows={scaleRows.map((x) => [x.size, Math.round(x.facts_per_s), ms(x.validate_supported.p95_ms),
                  ms(x.validate_transitive.p95_ms), ms(x.facts_about_exact.p95_ms), `${x.audit_s}s`, ms(x.factual_tier.p95_ms)])}
              />
            }
          >
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={scaleRows.map((x) => ({ size: x.size, direct: x.validate_supported.p95_ms, transitive: x.validate_transitive.p95_ms }))}>
                <CartesianGrid stroke={p.grid} vertical={false} />
                <XAxis dataKey="size" {...axis} />
                <YAxis {...axis} unit=" ms" width={60} />
                {tooltip}
                <Legend wrapperStyle={{ fontSize: 12, color: p.ink }} iconType="circle" iconSize={8} />
                <Line dataKey="direct" stroke={p.s1} strokeWidth={2} />
                <Line dataKey="transitive" stroke={p.s2} strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </Card>
          <div className="table-wrap">
            <h3>Audit recall on injected errors</h3>
            <Table
              head={['facts', ...Object.keys(scaleRows[0].audit_recall)]}
              rows={scaleRows.map((x) => [x.size, ...Object.values(x.audit_recall).map((v) => `${v.detected}/${v.injected}`)])}
            />
          </div>
        </Section>
      )}

      {r.taxonomy && (
        <Section title="Taxonomy classifier">
          <p className="card-note">
            {r.taxonomy.n} labelled questions, accuracy {pct(r.taxonomy.accuracy)} ({r.taxonomy.mode === 'fallback'
              ? 'keyword fallback, no model'
              : 'model classifier'}). {r.taxonomy.labels_note}.
          </p>
          <div className="table-wrap">
            <Table
              head={['type', 'precision', 'recall', 'n']}
              rows={Object.entries(r.taxonomy.per_class).map(([k, v]) => [k, pct(v.precision), pct(v.recall), v.n])}
            />
          </div>
        </Section>
      )}

      {r.audit && (
        <Section title="Graph audit">
          <p className="card-note">
            Last audit: {r.audit.counts.error ?? 0} errors, {r.audit.counts.warning ?? 0} warnings, {r.audit.counts.info ?? 0}{' '}
            notes.
          </p>
        </Section>
      )}

      <Section title="What this does not show">
        <ul className="caveats">
          {(r.caveats ?? []).map((c) => (
            <li key={c}>{c}</li>
          ))}
          <li>No figure on this page is a target or an estimate; anything not measured is simply absent.</li>
        </ul>
      </Section>
    </div>
  )
}
