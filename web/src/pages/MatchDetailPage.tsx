import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useClub } from '../context/ClubContext'
import './match.css'

const PANEL_ROWS: [string, string, string][] = [
  ['01_home_pass_network.png', '02_shot_map.png', '03_away_pass_network.png'],
  ['04_home_defensive_block.png', '05_gk_performance.png', '06_away_defensive_block.png'],
  ['07_home_progressive_passes.png', '08_match_momentum.png', '09_away_progressive_passes.png'],
  ['10_home_danger_zones.png', '11_match_stats.png', '12_away_danger_zones.png'],
  ['13_home_pass_end_zones.png', '14_high_turnovers.png', '15_away_pass_end_zones.png'],
  ['16_home_chance_creating.png', '17_crosses.png', '18_away_chance_creating.png'],
]

const TABS = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'build-up', label: 'Build-up' },
  { id: 'attack', label: 'Attack' },
  { id: 'defence', label: 'Defence' },
  { id: 'context', label: 'Context' },
  { id: 'data', label: 'Data' },
] as const

type TabId = (typeof TABS)[number]['id']

function pick(panels: string[], names: string[]) {
  return names.filter((n) => panels.includes(n))
}

export function MatchDetailPage() {
  const { id, tab } = useParams()
  const matchId = Number(id)
  const activeTab = (tab as TabId) || 'dashboard'
  const { club, season } = useClub()
  const nav = useNavigate()
  const [data, setData] = useState<Awaited<ReturnType<typeof api.matchPanels>> | null>(
    null,
  )
  const [teams, setTeams] = useState<Record<string, unknown>[]>([])
  const [timeline, setTimeline] = useState<Record<string, unknown>[]>([])
  const [shots, setShots] = useState<Record<string, unknown>[]>([])
  const [lightbox, setLightbox] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!matchId) return
    Promise.all([
      api.matchPanels(matchId, club),
      api.match(matchId, club),
      api.matchTimeline(matchId, club).catch(() => []),
      api.matchShots(matchId, club).catch(() => []),
    ])
      .then(([panels, detail, tl, sh]) => {
        setData(panels)
        setTeams(detail.teams || [])
        setTimeline(tl)
        setShots(sh)
      })
      .catch((e) => setErr(String(e)))
  }, [matchId, club])

  const match = data?.match
  const prefix = data?.url_prefix || ''
  const panels = data?.panels || []

  const homeStats = teams.find((t) => t.is_home) || teams[0]
  const awayStats = teams.find((t) => !t.is_home) || teams[1]

  const opponent = useMemo(() => {
    if (!match) return ''
    const ht = String(match.home_team || '')
    return ht.toLowerCase().includes('arsenal')
      ? String(match.away_team)
      : ht
  }, [match])

  const askHref = `/chat?match_id=${matchId}&opponent=${encodeURIComponent(opponent)}&season=${encodeURIComponent(String(match?.season || season))}`

  function panelUrl(name: string) {
    if (!prefix) return ''
    if (name.startsWith('00_')) return `${prefix}/${name}`
    return `${prefix}/panels/${name}`
  }

  function setTab(t: TabId) {
    nav(`/matches/${matchId}/${t}`)
  }

  const buildUpRows = rowsFor(panels, [0, 2, 4])

  if (err) return <p className="error">{err}</p>
  if (!match) return <p className="muted">Loading match…</p>

  return (
    <div className="match-page">
      <header className="match-header">
        <div className="scoreline">
          <div className="side">
            <span className="team-name">{match.home_team}</span>
          </div>
          <div className="score tabular">
            {match.home_score ?? '–'}
            <span className="sep">:</span>
            {match.away_score ?? '–'}
          </div>
          <div className="side away">
            <span className="team-name">{match.away_team}</span>
          </div>
        </div>
        <p className="meta eyebrow">
          {match.competition || 'Premier League'} · MW{match.matchweek ?? '—'} ·{' '}
          {String(match.match_date).slice(0, 10)}
          {match.venue_name ? ` · ${match.venue_name}` : ''}
        </p>
        <div className="stat-strip">
          <Opposed
            label="xG"
            home={num(match.home_xg)}
            away={num(match.away_xg)}
          />
          <Opposed
            label="Poss %"
            home={num(homeStats?.possession_pct)}
            away={num(awayStats?.possession_pct)}
          />
          <Opposed
            label="Shots"
            home={num(homeStats?.shots)}
            away={num(awayStats?.shots)}
          />
          <Opposed
            label="xT"
            home={num(homeStats?.xt)}
            away={num(awayStats?.xt)}
          />
        </div>
        <div className="match-actions">
          <Link className="btn-primary" to={askHref}>
            Ask about this match
          </Link>
        </div>
      </header>

      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={activeTab === t.id}
            className={activeTab === t.id ? 'active' : ''}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === 'dashboard' && data?.dashboard && (
        <figure className="dashboard-fig">
          <img
            src={panelUrl(data.dashboard)}
            alt="Match dashboard"
            loading="eager"
            onClick={() => setLightbox(panelUrl(data.dashboard!))}
          />
        </figure>
      )}

      {activeTab === 'build-up' && (
        <PanelMirror rows={buildUpRows} prefix={prefix} onOpen={setLightbox} />
      )}
      {activeTab === 'attack' && (
        <PanelGrid
          names={pick(panels, [
            '02_shot_map.png',
            '16_home_chance_creating.png',
            '18_away_chance_creating.png',
            '17_crosses.png',
          ])}
          prefix={prefix}
          onOpen={setLightbox}
        />
      )}
      {activeTab === 'defence' && (
        <PanelGrid
          names={pick(panels, [
            '04_home_defensive_block.png',
            '06_away_defensive_block.png',
            '10_home_danger_zones.png',
            '12_away_danger_zones.png',
            '14_high_turnovers.png',
          ])}
          prefix={prefix}
          onOpen={setLightbox}
        />
      )}
      {activeTab === 'context' && (
        <PanelGrid
          names={pick(panels, [
            '08_match_momentum.png',
            '11_match_stats.png',
            '05_gk_performance.png',
          ])}
          prefix={prefix}
          onOpen={setLightbox}
        />
      )}
      {activeTab === 'data' && (
        <div className="data-tab">
          <h3>Timeline</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>Min</th>
                <th>Type</th>
                <th>Player</th>
                <th>Team</th>
              </tr>
            </thead>
            <tbody>
              {timeline.map((e, i) => (
                <tr key={i}>
                  <td className="tabular">{String(e.minute ?? '')}</td>
                  <td>{String(e.type ?? e.card_type ?? '')}</td>
                  <td>{String(e.player ?? '')}</td>
                  <td>{String(e.team ?? '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Shots</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>Min</th>
                <th>Player</th>
                <th>Outcome</th>
                <th>x</th>
                <th>y</th>
              </tr>
            </thead>
            <tbody>
              {shots.map((s, i) => (
                <tr key={i}>
                  <td className="tabular">{String(s.minute ?? '')}</td>
                  <td>{String(s.player ?? '')}</td>
                  <td>{String(s.outcome ?? '')}</td>
                  <td className="tabular">{String(s.x ?? '')}</td>
                  <td className="tabular">{String(s.y ?? '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {lightbox && (
        <div
          className="lightbox"
          role="dialog"
          onClick={() => setLightbox(null)}
          onKeyDown={(e) => e.key === 'Escape' && setLightbox(null)}
        >
          <img src={lightbox} alt="" />
        </div>
      )}
    </div>
  )
}

function num(v: unknown): number | null {
  if (v == null || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function Opposed({
  label,
  home,
  away,
}: {
  label: string
  home: number | null
  away: number | null
}) {
  const h = home ?? 0
  const a = away ?? 0
  const total = h + a || 1
  return (
    <div className="opposed">
      <span className="eyebrow">{label}</span>
      <div className="opposed-bar">
        <div className="home" style={{ width: `${(h / total) * 100}%` }} />
        <div className="away" style={{ width: `${(a / total) * 100}%` }} />
      </div>
      <div className="opposed-vals tabular">
        <span>{home ?? '—'}</span>
        <span>{away ?? '—'}</span>
      </div>
    </div>
  )
}

function rowsFor(panels: string[], idxs: number[]) {
  return idxs
    .map((i) => PANEL_ROWS[i])
    .map(([h, s, a]) => [h, s, a].filter((n) => panels.includes(n)))
}

function PanelMirror({
  rows,
  prefix,
  onOpen,
}: {
  rows: string[][]
  prefix: string
  onOpen: (u: string) => void
}) {
  return (
    <div className="panel-mirror">
      {rows.map((row, i) => (
        <div key={i} className="panel-row">
          {row.map((name) => {
            const url = name.startsWith('00_')
              ? `${prefix}/${name}`
              : `${prefix}/panels/${name}`
            return (
              <button
                key={name}
                className="panel-cell"
                onClick={() => onOpen(url)}
                type="button"
              >
                <img src={url} alt={name} loading="lazy" />
                <span className="eyebrow">{name.replace('.png', '')}</span>
              </button>
            )
          })}
        </div>
      ))}
    </div>
  )
}

function PanelGrid({
  names,
  prefix,
  onOpen,
}: {
  names: string[]
  prefix: string
  onOpen: (u: string) => void
}) {
  return (
    <div className="panel-grid">
      {names.map((name) => {
        const url = `${prefix}/panels/${name}`
        return (
          <button key={name} className="panel-cell" onClick={() => onOpen(url)} type="button">
            <img src={url} alt={name} loading="lazy" />
            <span className="eyebrow">{name.replace('.png', '')}</span>
          </button>
        )
      })}
    </div>
  )
}
