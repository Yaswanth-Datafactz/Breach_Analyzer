import { BrowserRouter, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { NAV_ITEMS, resolvePageTitle } from './components/layout/nav'
import { DashboardPage } from './pages/Dashboard'
import { ExposurePage } from './pages/Exposure'
import { PersonDetailPage } from './pages/PersonDetail'
import { ReviewPage } from './pages/Review'
import { AgentsPage } from './pages/Agents'
import { AgentRunDetailPage } from './pages/AgentRunDetail'

/** The one place this use case wires its own nav/branding/page-title into
 * the shared AppShell (docs/plan.md D7) -- AppShell, Sidebar, TopBar, and
 * ThemeToggle are byte-identical copies of Use Case 1/2's and stay that
 * way. Per-route titles are resolved here via useLocation() rather than
 * adding a prop to AppShell. */
function Layout() {
  const location = useLocation()

  return (
    <AppShell
      navItems={NAV_ITEMS}
      productName="Breach Analytics"
      pageTitle={resolvePageTitle(location.pathname)}
    >
      <Outlet />
    </AppShell>
  )
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/exposure" element={<ExposurePage />} />
          <Route path="/persons/:personId" element={<PersonDetailPage />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/agents/runs/:agentRunId" element={<AgentRunDetailPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
