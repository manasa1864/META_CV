import { useEffect, useState } from 'react'
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
import { getMetrics, type Metrics } from './api'
import { Card, Table } from './charts'
import { usePalette } from './palette'

const pct = (x: number | null | undefined) => (x == null ? '-' : `${Math.round(x * 100)}%`)
const num = (x: number | null | undefined) => (x == null ? '-' : Math.round(x).toLocaleString())

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
  const base = bench?.configs?.baseline
  const full = bench?.configs?.full
  const sets = Array.from(new Set([...Object.keys(base?.by_set ?? {}), ...Object.keys(full?.by_set ?? {})]))
  const benchRows = full
    ? [...sets, 'overall'].map((set) => ({
        type: set,
        baseline: set === 'overall' ? base?.overall : base?.by_set[set],
        system: set === 'overall' ? full.overall : full.by_set[set],
      }))
    : []
  const curve = bench?.routing?.in_sample_curve ?? []
  const test = bench?.routing?.test
  const gh = m.graph_health
  const s = m.safety
  const refused = s ? Object.values(s.refused).reduce((a, b) => a + b, 0) : 0
  const safetyRows = s
    ? [
        { kind: 'questions with instruction-like text', count: s.injection_questions },
        { kind: 'answers the instructions changed (re-answered)', count: s.answers_steered },
        { kind: 'user-stated claims kept out of shared memory', count: s.tainted_claims },
        { kind: 'facts held for review (low source trust)', count: s.facts_held_low_trust },
        { kind: 'questions with private data', count: s.private_data_questions },
        { kind: 'stored facts ignored (carry instructions)', count: s.facts_ignored },
        { kind: 'new facts quarantined (carry instructions)', count: s.facts_quarantined },
        { kind: 'answers withheld (repeated the prompt)', count: s.answers_withheld },
        { kind: 'requests over the rate limit', count: s.refused.rate_limited },
        { kind: 'posts from other sites refused', count: s.refused.forbidden_origin },
        { kind: 'admin writes refused', count: s.refused.admin_denied },
        { kind: 'questions over the daily budget', count: s.refused.budget_exhausted },
      ]
    : []
  const redteam = bench?.safety
  const holdout = redteam?.detector?.injection_holdout

  return (
    <div className="metrics">
      <div className="metrics-head">
        <p className="card-note">
          Live numbers come from every answered question. Benchmark numbers come from{' '}
          <code>python -m app.bench</code>
          {bench?.meta ? `, last run ${new Date(bench.meta.created_at * 1000).toLocaleString()} (details on the Results tab)` : ' (not run yet)'}.
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
        <Tile label="Stopped instead of guessing" value={num(t.stopped)}
          sub={Object.entries(m.stops).map(([k, v]) => `${k.replaceAll('_', ' ')} ${v}`).join(', ') || 'no stops yet'} />
        <Tile label="Trimming check" value={`${num(m.context_check?.redrafted)} undone`}
          sub={`of ${num(m.context_check?.trimmed_answers)} trimmed answers · ${num(m.context_check?.history_expanded)} used full history`} />
        <Tile label="Graph health" value={gh ? `${gh.counts.error ?? 0} errors` : 'not audited'}
          sub={gh ? `${gh.counts.warning ?? 0} warnings · audited ${new Date(gh.ran_at * 1000).toLocaleTimeString()}` : 'see /audit/summary'} />
        <Tile label="Injection attempts seen" value={num(s?.injection_questions)}
          sub={`${num((s?.facts_ignored ?? 0) + (s?.facts_quarantined ?? 0))} poisoned facts held back · ${num(refused)} requests refused`} />
      </div>

      <div className="grid">
        <Card
          title="Benchmark: hallucination rate by set"
          note={
            full
              ? `Share of graded answers that were wrong (declining is counted separately as abstaining), from results/latest.json: ${bench?.meta?.turns ?? '?'} turns.`
              : 'Run python -m app.bench to compare a plain LLM with this system.'
          }
          table={
            <Table
              head={['set', 'baseline acc', 'baseline halluc.', 'system acc', 'system halluc.', 'system abstain', 'tokens base / sys']}
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
          {full ? (
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
              ? `In-sample (learned and scored on the same items). Held-out test: learned ${pct(test?.learned)}, specialist only ${pct(test?.static)} (n=${test?.n ?? 0}).`
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
          title="Safety checks, live"
          note={
            `What the safety checks saw in ${num(s?.questions_checked)} answered question(s), plus refusals that never became a question. ` +
            (redteam
              ? `Latest red-team run: ${holdout ? `${holdout.direct.caught}/${holdout.direct.n} held-out injection attempts caught, ${holdout.paraphrase.caught}/${holdout.paraphrase.n} paraphrases, ${holdout.benign.flagged}/${holdout.benign.n} benign questions flagged` : ''}` +
                `${redteam.end_to_end ? `; ${redteam.end_to_end.passed}/${redteam.end_to_end.n} end-to-end attacks resisted` : '; end-to-end attacks not run (no model)'} (details on the Results tab).`
              : 'Run python -m app.bench_safety for the red-team numbers.')
          }
          table={<Table head={['what', 'count']} rows={safetyRows.map((r) => [r.kind, r.count])} />}
        >
          {safetyRows.some((r) => r.count > 0) ? (
            <ResponsiveContainer width="100%" height={380}>
              <BarChart data={safetyRows} layout="vertical" margin={{ left: 8, right: 16 }}>
                <CartesianGrid stroke={p.grid} horizontal={false} />
                <XAxis type="number" {...axis} allowDecimals={false} />
                <YAxis type="category" dataKey="kind" {...axis} width={200} tick={{ fill: p.ink, fontSize: 11 }} />
                {tooltip}
                <Bar dataKey="count" name="count" fill={p.s1} radius={[0, 4, 4, 0]} maxBarSize={18} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="empty-chart">Nothing flagged or refused yet.</p>
          )}
        </Card>

        <section className="card">
          <div className="card-head">
            <h2>Adversarial routing and source trust</h2>
          </div>
          <p className="card-note">
            Questions with instruction-like text are answered normally or in verified-only mode (graph facts only), whichever
            has scored better on graded flagged questions; with no evidence it starts verified-only. Now:{' '}
            {s?.adversarial_policy.policy.replace('_', '-') ?? '-'}.
          </p>
          <div className="table-wrap">
            <Table
              head={['policy', 'score', 'weighted outcomes']}
              rows={Object.entries(s?.adversarial_policy.scores ?? {}).map(([k, v]) => [
                k.replace('_', '-'),
                v.toFixed(2),
                s?.adversarial_policy.evidence[k] ?? 0,
              ])}
            />
          </div>
          <p className="card-note">
            Sources whose facts the write gate could check, least trusted first. An untrusted source below 50% trust after 3
            checked facts has its new facts sent to review.
          </p>
          <div className="table-wrap">
            {s?.sources.length ? (
              <Table
                head={['source', 'trust', 'confirmed', 'contradicted']}
                rows={s.sources.map((x) => [x.source + (x.trusted ? ' (trusted)' : ''), pct(x.trust), x.confirmed, x.contradicted])}
              />
            ) : (
              <p className="empty-chart">No source has a checked fact yet.</p>
            )}
          </div>
        </section>

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
            <h2>Caught, by risk</h2>
          </div>
          <p className="card-note">
            Live. Outcomes so far: {m.outcomes.correct} correct, {m.outcomes.abstained} abstained, {m.outcomes.hallucinated} rated
            wrong, {m.outcomes.unknown} unknown.
          </p>
          <div className="table-wrap">
            <Table head={['risk', 'what it means', 'caught']} rows={m.caught_by_risk.map((r) => [r.risk, r.label, r.count])} />
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>Tokens by kind of reuse</h2>
          </div>
          <p className="card-note">
            Live, measured per answer. "state answer" = answered from a similar question's verified facts; "template" =
            adapted a similar verified calculation; "similar state" = a similar state was shown to the model.
          </p>
          <div className="table-wrap">
            <Table
              head={['reuse', 'answers', 'avg tokens', 'verified']}
              rows={(m.by_reuse ?? []).map((r) => [r.kind.replaceAll('_', ' '), r.turns, num(r.avg_tokens), pct(r.verified_rate)])}
            />
          </div>
        </section>

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
            Score = P(correct) minus a penalty for P(hallucinated) and a small cost for P(abstained), blended with a prior
            that favours the specialist, minus a token cost. Outcomes are weighted by signal: benchmark ground truth and
            your feedback count; the system's own confidence does not. The highest score per risk type is used next.
            {m.router_frozen ? ' The router is frozen: new answers do not change it.' : ''}
          </p>
          <div className="table-wrap">
            <Table
              head={['risk type', 'tier', 'weighted n', 'correct', 'abstained', 'hallucinated', 'avg tokens', 'score']}
              rows={m.router.map((r) => {
                const best = Math.max(...m.router.filter((x) => x.type === r.type).map((x) => x.score))
                return [r.type, r.score === best ? `${r.tier} (chosen)` : r.tier, r.n, pct(r.success_rate), pct(r.abstain_rate),
                  pct(r.hallucination_rate), num(r.avg_tokens), r.score.toFixed(2)]
              })}
            />
          </div>
        </section>
      </div>
    </div>
  )
}
