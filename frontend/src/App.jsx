import React, { useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Login from './pages/Login'
import Register from './pages/Register'
import Dashboard from './pages/Dashboard'
import Agents from './pages/Agents'
import Knowledge from './pages/Knowledge'
import Chat from './pages/Chat'
import CRM from './pages/CRM'
import Quotations from './pages/Quotations'
import Leads from './pages/Leads'
import Hermes from './pages/Hermes'
import Marketplace from './pages/Marketplace'
import CompanySettings from './pages/CompanySettings'
import Billing from './pages/Billing'
import AccountSettings from './pages/AccountSettings'
import PlatformAdmin from './pages/PlatformAdmin'
import Channels from './pages/Channels'
import BigData from './pages/BigData'
import Site from './pages/Site'
import SiteAdmin from './pages/SiteAdmin'
import IntegrationAdmin from './pages/IntegrationAdmin'
import IntegrationDetail from './pages/IntegrationDetail'
import BusinessSystems from './pages/BusinessSystems'
import Layout from './components/Layout'

function RequireAuth({ children }) {
  const token = localStorage.getItem('token')
  if (!token) return <Navigate to="/login" replace />
  return children
}

function useApplyStoredTheme() {
  useEffect(() => {
    const mode = localStorage.getItem('theme_mode') || 'light'
    const color = localStorage.getItem('theme_color') || 'blue'
    document.documentElement.setAttribute('data-theme', mode)
    document.documentElement.setAttribute('data-color', color)
  }, [])
}

export default function App() {
  useApplyStoredTheme()
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      {/* 公开官网展示页（无需登录） */}
      <Route path="/site" element={<Site />} />
      <Route path="/" element={<Navigate to="/site" replace />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/hermes" element={<Hermes />} />
        <Route path="/marketplace" element={<Marketplace />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/chat" element={<Chat />} />
        <Route path="/crm" element={<CRM />} />
        <Route path="/quotations" element={<Quotations />} />
        <Route path="/leads" element={<Leads />} />
        <Route path="/billing" element={<Billing />} />
        <Route path="/integrations" element={<Navigate to="/integrations/crm" replace />} />
        <Route path="/integrations/:system" element={<IntegrationDetail />} />
        <Route path="/systems" element={<BusinessSystems />} />
        <Route path="/settings" element={<CompanySettings />} />
        <Route path="/account" element={<AccountSettings />} />
        <Route path="/channels" element={<Navigate to="/channels/wecom" replace />} />
        <Route path="/channels/wecom" element={<Channels section="wecom" />} />
        <Route path="/channels/whatsapp" element={<Channels section="whatsapp" />} />
        <Route path="/channels/email" element={<Channels section="email" />} />
        <Route path="/admin" element={<PlatformAdmin />} />
        <Route path="/admin/verify" element={<PlatformAdmin />} />
        <Route path="/admin/finance" element={<PlatformAdmin />} />
        <Route path="/admin/stats" element={<PlatformAdmin />} />
        <Route path="/admin/llm" element={<PlatformAdmin />} />
        <Route path="/admin/site" element={<SiteAdmin />} />
        <Route path="/admin/integrations" element={<IntegrationAdmin />} />
      </Route>
      {/* 大数据看板：独立全屏页，无侧边栏，新标签页打开 */}
      <Route path="/bigdata" element={<RequireAuth><BigData /></RequireAuth>} />
    </Routes>
  )
}
