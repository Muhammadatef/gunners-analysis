const ORCH = import.meta.env.VITE_ORCHESTRATOR_URL || ''
const ANALYST = import.meta.env.VITE_ANALYST_URL || 'http://127.0.0.1:8100'

function qs(params: Record<string, string | number | undefined | null>) {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export type MatchRow = {
  match_id: number
  match_date: string
  matchweek: number | null
  competition: string | null
  home_score: number | null
  away_score: number | null
  home_xg: number | null
  away_xg: number | null
  home_team: string
  away_team: string
  season: string
  venue?: string | null
}

export type SeasonSummary = Record<string, unknown>
export type SeasonProgress = Record<string, unknown>

export const api = {
  orchBase: ORCH,
  analystBase: ANALYST,

  seasons(club: string) {
    return getJson<{ season_id: number; name: string }[]>(
      `${ORCH}/seasons${qs({ club })}`,
    )
  },
  clubs() {
    return getJson<{ club: string; schema: string; label: string }[]>(
      `${ORCH}/clubs`,
    )
  },
  seasonTable(club: string) {
    return getJson<SeasonSummary[]>(`${ORCH}/season/table${qs({ club })}`)
  },
  seasonProgress(club: string) {
    return getJson<SeasonProgress[]>(`${ORCH}/season/progress${qs({ club })}`)
  },
  matches(club: string, season?: string) {
    return getJson<MatchRow[]>(`${ORCH}/matches${qs({ club, season })}`)
  },
  match(id: number, club: string) {
    return getJson<{
      match: MatchRow & Record<string, unknown>
      teams: Record<string, unknown>[]
      panels: { panel: string; file_path: string; bytes?: number }[]
    }>(`${ORCH}/matches/${id}${qs({ club })}`)
  },
  matchPanels(id: number, club: string) {
    return getJson<{
      match_id: number
      match: MatchRow & Record<string, unknown>
      match_dir: string | null
      url_prefix: string | null
      dashboard: string | null
      panels: string[]
    }>(`${ORCH}/matches/${id}/panels${qs({ club })}`)
  },
  matchTimeline(id: number, club: string) {
    return getJson<Record<string, unknown>[]>(
      `${ORCH}/matches/${id}/timeline${qs({ club })}`,
    )
  },
  matchShots(id: number, club: string) {
    return getJson<Record<string, unknown>[]>(
      `${ORCH}/matches/${id}/shots${qs({ club })}`,
    )
  },
  conversations(club: string) {
    return getJson<
      { id: string; title: string | null; created_at: string; updated_at: string }[]
    >(`${ANALYST}/conversations${qs({ club })}`)
  },
  conversation(id: string, club: string) {
    return getJson<{
      id: string
      title: string | null
      messages: Record<string, unknown>[]
    }>(`${ANALYST}/conversations/${id}${qs({ club })}`)
  },
  createConversation(club: string) {
    return fetch(`${ANALYST}/conversations${qs({ club })}`, { method: 'POST' }).then(
      async (res) => {
        if (!res.ok) throw new Error(await res.text())
        return res.json() as Promise<{ id: string }>
      },
    )
  },
  deleteConversation(id: string, club: string) {
    return fetch(`${ANALYST}/conversations/${id}${qs({ club })}`, {
      method: 'DELETE',
    })
  },
}

export type ChatBody = {
  question: string
  conversation_id?: string | null
  club: string
  tier: string
  season?: string
  match_id?: number
  opponent?: string
  intent?: 'qa' | 'report'
}

export type SseHandler = {
  onEvent: (event: string, data: Record<string, unknown>) => void
  onError?: (err: Error) => void
}

export async function streamChat(body: ChatBody, handler: SseHandler, signal?: AbortSignal) {
  const res = await fetch(`${ANALYST}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) {
    throw new Error(`chat failed: ${res.status}`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventName = 'message'
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // Normalise CRLF from proxies; keep incomplete trailing line in buffer.
    const parts = buffer.replace(/\r\n/g, '\n').split('\n')
    buffer = parts.pop() || ''
    for (const line of parts) {
      if (line.startsWith('event:')) {
        eventName = line.slice(6).trim()
      } else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim()
        try {
          handler.onEvent(eventName, JSON.parse(raw || '{}'))
        } catch {
          handler.onEvent(eventName, { raw })
        }
        eventName = 'message'
      } else if (line === '') {
        eventName = 'message'
      }
    }
  }
}
