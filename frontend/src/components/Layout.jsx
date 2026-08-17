import React, { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import api from '../api'
import ThemeSwitcher from './ThemeSwitcher'
import FloatingSupport from './FloatingSupport'

// ============ 菜单配置 ============
// 企业侧：业务 / 企业（Hermes Runtime 已从企业侧移除，仅平台超管可见）
const NAV_GROUPS = [
  {
    key: 'business',
    label: '业务',
    items: [
      { href: '/bigdata', label: '大数据看板', icon: '📺' },
      { to: '/dashboard', label: '驾驶舱', icon: '📊' },
      { to: '/agents', label: 'AI员工', icon: '🤖' },
      { to: '/evolution', label: '自进化', icon: '🧬' },
      { to: '/chat', label: 'AI对话', icon: '💬' },
      { to: '/drawing-chat', label: 'AI图纸助手', icon: '📐' },
      { to: '/knowledge', label: '知识库', icon: '📚' },
      { to: '/crm', label: '客户CRM', icon: '👥' },
      { to: '/quotations', label: '报价单', icon: '📄' },
      { to: '/leads', label: 'AI获客', icon: '🎯' },
      { to: '/systems', label: '业务系统数据', icon: '🗂️' },
      {
        label: '渠道中心', icon: '📡',
        children: [
          { to: '/channels/wecom', label: '企业微信', icon: '💬' },
          { to: '/channels/whatsapp', label: 'Whatsapp', icon: '📱' },
          { to: '/channels/email', label: '企业邮件', icon: '✉️' },
        ],
      },
    ],
  },
  {
    key: 'enterprise',
    label: '企业',
    items: [
      { to: '/marketplace', label: 'Agent商城', icon: '🛍️' },
      { to: '/billing', label: '套餐与账单', icon: '💳' },
      {
        label: '对接业务系统', icon: '🔌',
        children: [
          { to: '/integrations/crm', label: 'CRM 客户关系管理', icon: '👥' },
          { to: '/integrations/erp', label: 'ERP 系统', icon: '📦' },
          { to: '/integrations/mes', label: 'MES 制造执行系统', icon: '⚙️' },
          { to: '/integrations/qms', label: 'QMS 质量管理系统', icon: '✅' },
        ],
      },
      { to: '/settings', label: '企业设置', icon: '⚙️' },
      { to: '/account', label: '账户设置', icon: '👤' },
      { to: '/help', label: '使用帮助', icon: '📖' },
    ],
  },
]

// 平台超管专属菜单
const PLATFORM_GROUPS = [
  {
    key: 'platform',
    label: '平台管理',
    items: [
      { to: '/admin', label: '企业管理', icon: '🏢' },
      { to: '/admin/verify', label: '认证审核', icon: '📋' },
      { to: '/admin/finance', label: '财务管理', icon: '💰' },
      { to: '/admin/stats', label: '平台统计', icon: '📈' },
      { to: '/admin/llm', label: '模型设置', icon: '🧠' },
      { to: '/admin/site', label: '官网内容', icon: '🌐' },
      { to: '/admin/integrations', label: '对接管理', icon: '🔌' },
      { to: '/hermes', label: 'Hermes Runtime', icon: '⚙️' },
    ],
  },
]

// 未认证企业可访问的白名单
const ALLOWED_WHEN_UNVERIFIED = new Set(['/dashboard', '/settings', '/account'])

export default function Layout() {
  const navigate = useNavigate()
  const location = useLocation()
  const [companyName, setCompanyName] = useState(localStorage.getItem('company') || '我的企业')
  const [verified, setVerified] = useState(false)
  const [verifiedLoaded, setVerifiedLoaded] = useState(false)
  const [userRole, setUserRole] = useState(localStorage.getItem('role') || 'admin')
  const [userEmail, setUserEmail] = useState(localStorage.getItem('email') || '')
  const [expandedMenus, setExpandedMenus] = useState(new Set())

  const toggleMenu = (label) => {
    setExpandedMenus((prev) => {
      const next = new Set(prev)
      if (next.has(label)) {
        next.delete(label)
      } else {
        next.add(label)
      }
      return next
    })
  }

  // 当前路由命中某个子菜单时，自动展开其父菜单
  useEffect(() => {
    const next = new Set()
    groups.forEach((group) => {
      group.items.forEach((item) => {
        if (item.children && item.children.some((c) => isActivePath(c.to))) {
          next.add(item.label)
        }
      })
    })
    setExpandedMenus(next)
  }, [location.pathname])

  const loadMe = async () => {
    try {
      const { data } = await api.get('/auth/me')
      setVerified(Boolean(data.license_verified))
      setUserRole(data.role || 'admin')
      setUserEmail(data.email || '')
      localStorage.setItem('role', data.role || 'admin')
    } catch (e) { /* 忽略 */ } finally {
      setVerifiedLoaded(true)
    }
  }

  useEffect(() => {
    loadMe()
    const sync = () => {
      setCompanyName(localStorage.getItem('company') || '我的企业')
      loadMe()
    }
    window.addEventListener('storage', sync)
    window.addEventListener('focus', sync)
    return () => {
      window.removeEventListener('storage', sync)
      window.removeEventListener('focus', sync)
    }
  }, [])

  const logout = () => {
    localStorage.removeItem('token')
    localStorage.removeItem('company')
    localStorage.removeItem('role')
    navigate('/login')
  }

  // 平台超管：只显示平台管理菜单；企业用户：业务+企业
  const isPlatform = userRole === 'platform_admin'
  const groups = isPlatform ? PLATFORM_GROUPS : NAV_GROUPS

  // 把 isActivePath 提前，供展开状态 useEffect 使用
  const isActivePath = (to) => location.pathname === to || location.pathname.startsWith(to + '/')

  const renderNavItem = (item, level = 0) => {
    // 外链 / 新标签页打开（如大数据看板）
    if (item.href) {
      return (
        <a
          key={item.label}
          href={item.href}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: 'flex', alignItems: 'center', gap: 10,
            padding: '10px 20px', fontSize: 14, textDecoration: 'none',
            color: 'var(--sidebar-text-muted)',
          }}
        >
          <span>{item.icon}</span>
          {item.label}
        </a>
      )
    }
    if (item.children) {
      const anyActive = item.children.some((c) => isActivePath(c.to))
      const isExpanded = expandedMenus.has(item.label) || anyActive
      return (
        <div key={item.label}>
          <div
            onClick={() => toggleMenu(item.label)}
            style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '10px 20px', fontSize: 14, cursor: 'pointer',
              color: anyActive ? 'var(--sidebar-active-text)' : 'var(--sidebar-text-muted)',
              background: anyActive ? 'var(--sidebar-active)' : 'transparent',
              borderLeft: anyActive ? '3px solid var(--primary)' : '3px solid transparent',
              userSelect: 'none',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span>{item.icon}</span>
              {item.label}
            </div>
            <span style={{ fontSize: 10, transition: 'transform 0.2s', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)' }}>
              ▶
            </span>
          </div>
          {isExpanded && (
            <div style={{ paddingLeft: level === 0 ? 12 : 0 }}>
              {item.children.map((child) => renderNavItem(child, level + 1))}
            </div>
          )}
        </div>
      )
    }
    return (
      <NavLink
        key={item.label}
        to={item.to}
        end={item.to === '/'}
        style={({ isActive }) => ({
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '10px 20px',
          fontSize: 14,
          color: isActive ? 'var(--sidebar-active-text)' : 'var(--sidebar-text-muted)',
          background: isActive ? 'var(--sidebar-active)' : 'transparent',
          borderLeft: isActive ? '3px solid var(--primary)' : '3px solid transparent',
        })}
      >
        <span>{item.icon}</span>
        {item.label}
      </NavLink>
    )
  }

  // 未认证守卫（仅企业用户）
  const needVerify = verifiedLoaded && !isPlatform && !verified &&
    !ALLOWED_WHEN_UNVERIFIED.has(location.pathname)
  const gotoVerify = () => navigate('/settings?tab=license')

  return (
    <div style={{ display: 'flex', minHeight: '100vh' }}>
      {/* 侧边栏 */}
      <aside style={{
        width: 220,
        background: 'var(--sidebar-bg)',
        color: 'var(--sidebar-text)',
        display: 'flex',
        flexDirection: 'column',
        position: 'fixed',
        top: 0,
        bottom: 0,
        left: 0,
        transition: 'background 0.25s',
      }}>
        <div style={{ padding: '20px 16px', borderBottom: '1px solid var(--sidebar-active)' }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-inverse)' }}>🏭 制造业AI员工平台</div>
          <div style={{ fontSize: 11, color: 'var(--sidebar-text-muted)', marginTop: 6, display: 'flex', alignItems: 'center', gap: 6 }}>
            {isPlatform ? '平台管理控制台' : companyName}
            {!isPlatform && (verified ? (
              <span style={{
                display: 'inline-flex', alignItems: 'center', gap: 3,
                background: '#dcfce7', color: '#15803d', padding: '1px 7px',
                borderRadius: 999, fontSize: 10, fontWeight: 700,
              }}>
                ✓ 已认证
              </span>
            ) : (
              <span style={{
                display: 'inline-flex', alignItems: 'center', gap: 3,
                background: '#fee2e2', color: '#b91c1c', padding: '1px 7px',
                borderRadius: 999, fontSize: 10, fontWeight: 700,
              }}>
                ⚠ 未认证
              </span>
            ))}
          </div>
        </div>
        <nav style={{ flex: 1, padding: '12px 0', overflowY: 'auto' }}>
          {groups.map((group) => (
            <div key={group.key} style={{ marginBottom: 8 }}>
              {group.label && (
                <div style={{
                  padding: '6px 20px', fontSize: 11, fontWeight: 600,
                  color: 'var(--text-muted)', letterSpacing: 1,
                }}>
                  {group.label}
                </div>
              )}
              {group.items.map((item) => renderNavItem(item))}
            </div>
          ))}
        </nav>
        {/* 底部：用户信息 + 退出 */}
        <div style={{ padding: 12, borderTop: '1px solid var(--border)' }}>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '4px 8px', marginBottom: 4 }}>
            {userEmail && <span style={{ display: 'block', marginBottom: 2 }}>👤 {userEmail}</span>}
            <span style={{
              display: 'inline-block', padding: '1px 6px', borderRadius: 4,
              background: isPlatform ? 'var(--primary)' : userRole === 'admin' ? 'var(--primary-hover)' : 'var(--border-strong)',
              color: '#fff', fontSize: 10,
            }}>
              {isPlatform ? '平台管理员' : userRole === 'admin' ? '企业管理员' : '员工'}
            </span>
          </div>
          <button
            onClick={logout}
            style={{
              width: '100%', padding: '6px 12px', borderRadius: 6,
              background: 'transparent', border: '1px solid var(--sidebar-text-muted)',
              color: 'var(--sidebar-text)', fontSize: 13, cursor: 'pointer',
              transition: 'all 0.15s',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.background = 'var(--sidebar-hover)'
              e.currentTarget.style.borderColor = 'var(--sidebar-text)'
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = 'transparent'
              e.currentTarget.style.borderColor = 'var(--sidebar-text-muted)'
            }}
          >
            退出登录
          </button>
        </div>
      </aside>

      {/* 主内容 */}
      <main style={{ flex: 1, marginLeft: 220, padding: 24, minHeight: '100vh' }}>
        <Outlet />
      </main>

      {/* 右上角主题切换器 */}
      <div style={{ position: 'fixed', top: 14, right: 24, zIndex: 999 }}>
        <ThemeSwitcher />
      </div>

      {/* 右下角悬浮在线客服（数据来自官网内容 social/contact 配置） */}
      <FloatingSupport />

      {/* 未认证守卫 modal */}
      {needVerify && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }}>
          <div className="card" style={{ width: 440, textAlign: 'center' }}>
            <div style={{ fontSize: 56, marginBottom: 8 }}>🔒</div>
            <h2 style={{ fontSize: 20, marginBottom: 10 }}>请先进行企业认证</h2>
            <p style={{ color: 'var(--text-muted)', fontSize: 14, lineHeight: 1.7, marginBottom: 20 }}>
              上传营业执照并通过平台审核后，即可解锁全部功能（AI 员工对话、知识库、报价、销售等）。
            </p>
            <button className="btn btn-primary" style={{ width: '100%' }} onClick={gotoVerify}>
              📋 前去认证
            </button>
          </div>
        </div>
      )}
    </div>
  )
}