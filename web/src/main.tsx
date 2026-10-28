import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ClubProvider } from './context/ClubContext'
import { Layout } from './components/Layout'
import { SeasonOverview } from './pages/SeasonOverview'
import { MatchesPage } from './pages/MatchesPage'
import { MatchDetailPage } from './pages/MatchDetailPage'
import { ChatPage } from './pages/ChatPage'
import './styles/global.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ClubProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<SeasonOverview />} />
            <Route path="matches" element={<MatchesPage />} />
          </Route>
          <Route element={<Layout wide />}>
            <Route path="matches/:id" element={<MatchDetailPage />} />
            <Route path="matches/:id/:tab" element={<MatchDetailPage />} />
          </Route>
          <Route element={<Layout chat />}>
            <Route path="chat" element={<ChatPage />} />
            <Route path="chat/:conversationId" element={<ChatPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </ClubProvider>
  </StrictMode>,
)
