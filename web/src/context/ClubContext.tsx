import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react'

type ClubCtx = {
  club: string
  setClub: (c: string) => void
  season: string
  setSeason: (s: string) => void
}

const Ctx = createContext<ClubCtx | null>(null)

export function ClubProvider({ children }: { children: ReactNode }) {
  const [club, setClubState] = useState(
    () => localStorage.getItem('ga.club') || 'gunners',
  )
  const [season, setSeasonState] = useState(
    () => localStorage.getItem('ga.season') || '2025/2026',
  )

  const setClub = (c: string) => {
    localStorage.setItem('ga.club', c)
    setClubState(c)
  }
  const setSeason = (s: string) => {
    localStorage.setItem('ga.season', s)
    setSeasonState(s)
  }

  return (
    <Ctx.Provider value={{ club, setClub, season, setSeason }}>
      {children}
    </Ctx.Provider>
  )
}

export function useClub() {
  const v = useContext(Ctx)
  if (!v) throw new Error('useClub outside provider')
  return v
}

type Theme = 'system' | 'light' | 'dark'

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(
    () => (localStorage.getItem('ga.theme') as Theme) || 'system',
  )

  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
    localStorage.setItem('ga.theme', theme)
  }, [theme])

  return { theme, setTheme: setThemeState }
}
