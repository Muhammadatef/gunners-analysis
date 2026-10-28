import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type MatchRow } from '../api/client'
import { useClub } from '../context/ClubContext'
import './season.css'

export function MatchesPage() {
  const { club, season } = useClub()
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [q, setQ] = useState('')
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api
      .matches(club, season)
      .then(setMatches)
      .catch((e) => setErr(String(e)))
  }, [club, season])

  const filtered = matches.filter((m) => {
    if (!q.trim()) return true
    const hay = `${m.home_team} ${m.away_team}`.toLowerCase()
    return hay.includes(q.toLowerCase())
  })

  return (
    <div className="season-page">
      <p className="eyebrow">All matches · {season}</p>
      <h1>Match gallery</h1>
      <input
        className="filter-input"
        placeholder="Filter by opponent…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        aria-label="Filter matches"
      />
      {err && <p className="error">{err}</p>}
      <div className="fixture-list" style={{ marginTop: 16 }}>
        {filtered.map((m) => (
          <Link key={m.match_id} to={`/matches/${m.match_id}`} className="fixture-row">
            <span className="mw tabular">
              {m.matchweek != null ? `MW${m.matchweek}` : '—'}
            </span>
            <span className="teams">
              {m.home_team} <span className="muted">vs</span> {m.away_team}
            </span>
            <span className="score tabular">
              {m.home_score != null ? `${m.home_score}–${m.away_score}` : 'vs'}
            </span>
            <span className="date muted">{String(m.match_date).slice(0, 10)}</span>
          </Link>
        ))}
      </div>
    </div>
  )
}
