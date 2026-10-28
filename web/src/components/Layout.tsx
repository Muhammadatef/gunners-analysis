import { NavLink, Outlet } from 'react-router-dom'
import { useClub, useTheme } from '../context/ClubContext'
import './layout.css'

export function Layout({ wide = false, chat = false }: { wide?: boolean; chat?: boolean }) {
  const { club, setClub, season, setSeason } = useClub()
  const { theme, setTheme } = useTheme()

  return (
    <div className="app-shell">
      <header className="app-header">
        <NavLink to="/" className="brand">
          GunnersAnalysis
        </NavLink>
        <nav className="nav">
          <NavLink to="/" end>
            Season
          </NavLink>
          <NavLink to="/matches">Matches</NavLink>
          <NavLink to="/chat">Chat</NavLink>
        </nav>
        <div className="header-controls">
          <label className="eyebrow">
            Club
            <select value={club} onChange={(e) => setClub(e.target.value)} aria-label="Club">
              <option value="gunners">Arsenal</option>
            </select>
          </label>
          <label className="eyebrow">
            Season
            <select
              value={season}
              onChange={(e) => setSeason(e.target.value)}
              aria-label="Season"
            >
              <option value="2025/2026">2025/26</option>
              <option value="2026/2027">2026/27</option>
            </select>
          </label>
          <label className="eyebrow">
            Theme
            <select
              value={theme}
              onChange={(e) => setTheme(e.target.value as 'system' | 'light' | 'dark')}
              aria-label="Theme"
            >
              <option value="system">System</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </label>
        </div>
      </header>
      <main className={`app-main${wide ? ' wide' : ''}${chat ? ' chat-main' : ''}`}>
        <Outlet />
      </main>
    </div>
  )
}
