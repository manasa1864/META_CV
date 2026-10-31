/** Shared chart pieces: a chart card with a table view, and a table. */
import { useState, type ReactNode } from 'react'

export function Card({ title, note, table, children }: { title: string; note?: string; table: ReactNode; children: ReactNode }) {
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

export function Table({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
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
