import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, streamChat } from '../api/client'
import { useClub } from '../context/ClubContext'
import cannonUrl from '../assets/cannon.png'
import './chat.css'

type Conv = { id: string; title: string | null; updated_at: string }

type Stage = 'idle' | 'reading' | 'routing' | 'gathering' | 'writing'

type Msg = {
  id: string
  role: 'user' | 'assistant'
  content: string
  tool?: string
  query_id?: string
  parameters?: Record<string, unknown>
  reasoning?: string
  unsupported_numbers?: number[]
  evidence?: unknown
  retrieved?: unknown
  streaming?: boolean
  stage?: Stage
}

const STARTERS = [
  'Was Arsenal a high-pressing side in 2025/26?',
  'How many points did Arsenal take in 2025/26?',
  'Who created the most chances against Manchester City?',
  "Which chance was Arsenal's best against Liverpool?",
  'Write a technical report for Arsenal vs Leeds, 2025/26',
]

const STAGE_LABEL: Record<Exclude<Stage, 'idle'>, string> = {
  reading: 'Reading the question…',
  routing: 'Choosing a tool…',
  gathering: 'Gathering evidence…',
  writing: 'Writing…',
}

function CannonBuffer({ label }: { label: string }) {
  return (
    <div className="cannon-buffer" role="status" aria-live="polite">
      <div className="cannon-orbit">
        <img src={cannonUrl} alt="" className="cannon-logo" width={40} height={40} />
      </div>
      <div className="cannon-copy">
        <span className="cannon-label">{label}</span>
        <span className="cannon-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
      </div>
    </div>
  )
}

function AnswerBody({
  content,
  unsupported,
  streaming,
}: {
  content: string
  unsupported: number[]
  streaming?: boolean
}) {
  return (
    <>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      {streaming && content ? <span className="stream-caret" aria-hidden="true" /> : null}
      {unsupported.length > 0 ? (
        <p className="verify warn-line">
          Unsupported figures vs evidence: {unsupported.join(', ')}.
        </p>
      ) : null}
    </>
  )
}

export function ChatPage() {
  const { conversationId } = useParams()
  const [params] = useSearchParams()
  const nav = useNavigate()
  const { club, season } = useClub()
  const [convs, setConvs] = useState<Conv[]>([])
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [tier, setTier] = useState('fallback')
  const [streaming, setStreaming] = useState(false)
  const [route, setRoute] = useState<Record<string, unknown> | null>(null)
  const [stage, setStage] = useState<Stage>('idle')
  const routeRef = useRef<Record<string, unknown> | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const listRef = useRef<HTMLDivElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  // Prevents the conversation-load effect from wiping the in-flight stream
  // when we navigate /chat → /chat/:id on the first SSE `start` event.
  const streamingRef = useRef(false)

  const matchId = params.get('match_id')
    ? Number(params.get('match_id'))
    : undefined
  const opponent = params.get('opponent') || undefined
  const seasonParam = params.get('season') || season

  const refreshConvs = useCallback(() => {
    api.conversations(club).then(setConvs).catch(() => setConvs([]))
  }, [club])

  useEffect(() => {
    refreshConvs()
  }, [refreshConvs])

  useEffect(() => {
    if (!conversationId) {
      if (!streamingRef.current) setMessages([])
      return
    }
    if (streamingRef.current) return
    let cancelled = false
    api
      .conversation(conversationId, club)
      .then((c) => {
        if (cancelled || streamingRef.current) return
        setMessages(
          (c.messages || []).map((m) => ({
            id: String(m.id),
            role: m.role as 'user' | 'assistant',
            content: String(m.content || ''),
            tool: m.tool as string | undefined,
            query_id: m.query_id as string | undefined,
            parameters: m.parameters as Record<string, unknown> | undefined,
            reasoning: m.reasoning as string | undefined,
            unsupported_numbers: (m.unsupported_numbers as number[]) || [],
            evidence: m.evidence,
            retrieved: m.retrieved,
          })),
        )
      })
      .catch(() => {
        if (!cancelled && !streamingRef.current) setMessages([])
      })
    return () => {
      cancelled = true
    }
  }, [conversationId, club])

  useEffect(() => {
    if (stickRef.current) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, route, stage])

  function onScroll() {
    const el = listRef.current
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    stickRef.current = nearBottom
  }

  function resizeComposer() {
    const el = taRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  }

  async function send(question: string) {
    if (!question.trim() || streaming) return
    setInput('')
    if (taRef.current) {
      taRef.current.style.height = 'auto'
    }
    setStreaming(true)
    streamingRef.current = true
    setRoute(null)
    routeRef.current = null
    setStage('reading')
    const userMsg: Msg = {
      id: `u-${Date.now()}`,
      role: 'user',
      content: question,
    }
    const assistantId = `a-${Date.now()}`
    setMessages((prev) => [
      ...prev,
      userMsg,
      {
        id: assistantId,
        role: 'assistant',
        content: '',
        streaming: true,
        stage: 'reading',
      },
    ])

    const ac = new AbortController()
    abortRef.current = ac
    let cid = conversationId || null
    let unsupported: number[] = []
    let gotToken = false

    try {
      await streamChat(
        {
          question,
          conversation_id: cid,
          club,
          tier,
          season: seasonParam,
          match_id: matchId,
          opponent,
          intent: undefined,
        },
        {
          onEvent(event, data) {
            if (event === 'start' && data.conversation_id) {
              cid = String(data.conversation_id)
              if (!conversationId) nav(`/chat/${cid}`, { replace: true })
              setStage('routing')
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId ? { ...m, stage: 'routing' } : m,
                ),
              )
            }
            if (event === 'route') {
              setRoute(data)
              routeRef.current = data
              setStage('gathering')
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        stage: 'gathering',
                        tool: data.tool as string | undefined,
                        query_id: data.query_id as string | undefined,
                        parameters: data.parameters as Record<string, unknown> | undefined,
                        reasoning: data.reasoning as string | undefined,
                      }
                    : m,
                ),
              )
            }
            if (event === 'evidence') {
              setStage('gathering')
              const r = routeRef.current
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        stage: 'gathering',
                        evidence: data,
                        retrieved: data.documents,
                        tool: (r?.tool as string) || m.tool,
                        query_id: (r?.query_id as string) || m.query_id,
                        parameters:
                          (r?.parameters as Record<string, unknown>) || m.parameters,
                      }
                    : m,
                ),
              )
            }
            if (event === 'token') {
              const text = String(data.text || '')
              if (!gotToken) {
                gotToken = true
                setStage('writing')
              }
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: m.content + text, stage: 'writing' }
                    : m,
                ),
              )
            }
            if (event === 'check') {
              unsupported = (data.unsupported_numbers as number[]) || []
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, unsupported_numbers: unsupported }
                    : m,
                ),
              )
            }
            if (event === 'done') {
              const r = routeRef.current
              setStage('idle')
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        streaming: false,
                        stage: 'idle',
                        tool: (r?.tool as string) || m.tool,
                        query_id: (r?.query_id as string) || m.query_id,
                        parameters:
                          (r?.parameters as Record<string, unknown>) || m.parameters,
                        unsupported_numbers: unsupported,
                      }
                    : m,
                ),
              )
              refreshConvs()
            }
            if (event === 'error') {
              setStage('idle')
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        streaming: false,
                        stage: 'idle',
                        content:
                          m.content ||
                          `Could not answer: ${data.message || 'unknown error'}`,
                      }
                    : m,
                ),
              )
            }
          },
        },
        ac.signal,
      )
    } catch (e) {
      setStage('idle')
      if ((e as Error).name !== 'AbortError') {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, streaming: false, stage: 'idle', content: `Error: ${e}` }
              : m,
          ),
        )
      } else {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  streaming: false,
                  stage: 'idle',
                  content: m.content || '_Stopped._',
                }
              : m,
          ),
        )
      }
    } finally {
      streamingRef.current = false
      setStreaming(false)
      setStage('idle')
      abortRef.current = null
    }
  }

  function stop() {
    abortRef.current?.abort()
    streamingRef.current = false
    setStreaming(false)
    setStage('idle')
  }

  return (
    <div className="chat-layout">
      <aside className="chat-rail">
        <button
          type="button"
          className="new-chat"
          onClick={() => {
            nav('/chat')
            setMessages([])
            setRoute(null)
            setStage('idle')
          }}
        >
          New chat
        </button>
        <ul>
          {convs.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className={c.id === conversationId ? 'active' : ''}
                onClick={() => nav(`/chat/${c.id}`)}
              >
                {c.title || 'Untitled'}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="chat-centre">
        <div className="chat-scroll" ref={listRef} onScroll={onScroll}>
          {!messages.length && (
            <div className="empty">
              <img src={cannonUrl} alt="" className="empty-cannon" width={56} height={56} />
              <h1>Analyst</h1>
              <p className="lede">
                Ask about the warehouse, or request a technical match report.
                {matchId ? (
                  <>
                    {' '}
                    Pre-seeded for match <span className="tabular">{matchId}</span>
                    {opponent ? ` vs ${opponent}` : ''}.
                  </>
                ) : null}
              </p>
              <div className="starters">
                {STARTERS.map((s) => (
                  <button key={s} type="button" onClick={() => send(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m) => {
            const waiting =
              m.role === 'assistant' && m.streaming && !m.content
            const liveStage = (m.stage || stage) as Stage
            const stageLabel =
              liveStage !== 'idle' ? STAGE_LABEL[liveStage] : 'Working…'

            return (
              <article
                key={m.id}
                className={`msg msg-${m.role}${m.streaming ? ' streaming' : ''}`}
              >
                {m.role === 'user' ? (
                  <div className="bubble">{m.content}</div>
                ) : (
                  <>
                    {(m.streaming || m.query_id) && (
                      <div
                        className={`reasoning-strip${m.streaming ? ' live' : ' settled'}`}
                      >
                        {m.streaming ? (
                          <img
                            src={cannonUrl}
                            alt=""
                            className="strip-cannon"
                            width={18}
                            height={18}
                          />
                        ) : (
                          <span className="dot" />
                        )}
                        <code>{String(route?.tool || m.tool || (m.streaming ? '…' : '—'))}</code>
                        <code>
                          {String(route?.query_id || m.query_id || '')}
                        </code>
                        {(route?.parameters || m.parameters)
                          ? Object.entries(
                              (route?.parameters ||
                                m.parameters) as Record<string, unknown>,
                            ).map(([k, v]) => (
                              <span key={k} className="chip">
                                {k}: {String(v)}
                              </span>
                            ))
                          : null}
                      </div>
                    )}

                    {waiting ? (
                      <CannonBuffer label={stageLabel} />
                    ) : (
                      <div className="answer md">
                        <AnswerBody
                          content={m.content}
                          unsupported={m.unsupported_numbers || []}
                          streaming={m.streaming}
                        />
                      </div>
                    )}

                    {!m.streaming &&
                      m.content &&
                      !(m.unsupported_numbers?.length) &&
                      !m.content.startsWith('Could not answer:') &&
                      !m.content.startsWith('Error:') && (
                      <p className="verify">Every figure traces to the evidence.</p>
                    )}
                    {!m.streaming && (m.evidence || m.retrieved) && (
                      <details className="evidence-drawer">
                        <summary>Evidence</summary>
                        <pre>
                          {JSON.stringify(m.evidence || m.retrieved, null, 2)}
                        </pre>
                        <a
                          href="http://127.0.0.1:5000"
                          target="_blank"
                          rel="noreferrer"
                        >
                          Open MLflow
                        </a>
                      </details>
                    )}
                    {!m.streaming && m.content && (
                      <button
                        type="button"
                        className="copy-btn"
                        onClick={() => navigator.clipboard.writeText(m.content)}
                      >
                        Copy
                      </button>
                    )}
                  </>
                )}
              </article>
            )
          })}
          <div ref={bottomRef} />
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault()
            send(input)
          }}
        >
          <select
            value={tier}
            onChange={(e) => setTier(e.target.value)}
            aria-label="Model tier"
            disabled={streaming}
          >
            <option value="fast">fast</option>
            <option value="fallback">fallback</option>
            <option value="cheap">cheap</option>
            <option value="quality">quality</option>
          </select>
          <textarea
            ref={taRef}
            rows={1}
            value={input}
            placeholder={streaming ? 'Cannon is working…' : 'Ask the analyst…'}
            disabled={streaming}
            onChange={(e) => {
              setInput(e.target.value)
              resizeComposer()
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                send(input)
              }
            }}
          />
          {streaming ? (
            <button type="button" className="btn-stop" onClick={stop}>
              Stop
            </button>
          ) : (
            <button type="submit" disabled={!input.trim()}>
              Send
            </button>
          )}
        </form>
      </section>
    </div>
  )
}
