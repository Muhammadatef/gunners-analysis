import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type MatchRow } from '../api/client'
import { useClub } from '../context/ClubContext'
import './season.css'

function formFromMatches(matches: MatchRow[], team = 'Arsenal') {
  const recent = [...matches]
    .filter((m) => m.home_score != null)
    .slice(0, 6)
    .reverse()
  return recent.map((m) => {
    const home = (m.home_team || '').toLowerCase().includes(team.toLowerCase())
    const gf = home ? m.home_score! : m.away_score!
    const ga = home ? m.away_score! : m.home_score!
    if (gf > ga) return 'W'
    if (gf < ga) return 'L'
    return 'D'
  })
}

export function SeasonOverview() {
  const { club, season } = useClub()
  const [table, setTable] = useState<Record<string, unknown>[]>([])
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [t, m] = await Promise.all([
          api.seasonTable(club),
          api.matches(club, season),
        ])
        if (!cancelled) {
          setTable(t)
          setMatches(m)
          setErr(null)
        }
      } catch (e) {
        if (!cancelled) setErr(String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [club, season])

  const arsenal = useMemo(
    () =>
      table.find((r) =>
        String(r.team_name || r.name || '')
          .toLowerCase()
          .includes('arsenal'),
      ),
    [table],
  )
  const form = formFromMatches(matches)
  const pos = arsenal
    ? table.findIndex((r) => r === arsenal) + 1
    : null

  return (
    <div className="season-page">
      <p className="eyebrow">Season overview · {season}</p>
      <h1>GunnersAnalysis</h1>
      <p className="lede">
        League position, recent form, and the fixture list for the season
        warehouse.
      </p>
      {err && <p className="error">{err}</p>}

      <section className="hero-stats">
        <div>
          <span className="eyebrow">Position</span>
          <strong className="big tabular">{pos ?? '—'}</strong>
        </div>
        <div>
          <span className="eyebrow">Points</span>
          <strong className="big tabular">
            {arsenal ? String(arsenal.points ?? '—') : '—'}
          </strong>
        </div>
        <div>
          <span className="eyebrow">Form (last 6)</span>
          <div className="form">
            {form.length
              ? form.map((r, i) => (
                  <span key={i} className={`form-pill form-${r}`}>
                    {r}
                  </span>
                ))
              : '—'}
          </div>
        </div>
        <div>
          <span className="eyebrow">GF / GA</span>
          <strong className="big tabular">
            {arsenal
              ? `${arsenal.goals_for ?? '—'} / ${arsenal.goals_against ?? '—'}`
              : '—'}
          </strong>
        </div>
      </section>

      <section>
        <h2>Fixtures</h2>
        <div className="fixture-list">
          {matches.map((m) => (
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
      </section>
    </div>
  )
}
