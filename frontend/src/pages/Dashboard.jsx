import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api'

export default function Dashboard() {
  const [overview, setOverview] = useState(null)
  const [sales, setSales] = useState(null)
  const [report, setReport] = useState('')
  const [loading, setLoading] = useState(true)
  const [reportLoading, setReportLoading] = useState(false)
  const [verified, setVerified] = useState(false)
  const [message, setMessage] = useState('')

  const navigate = useNavigate()
  const companyId = Number(localStorage.getItem('company_id'))

  useEffect(() => {
    const load = async () => {
      try {
        const me = await api.get('/auth/me')
        setVerified(Boolean(me.data.license_verified))
      } catch {}
      try {
        const [ov] = await Promise.all([
          api.get('/dashboard/overview', { params: { company_id: companyId } }),
        ])
        setOverview(ov.data)
        // 销售驾驶舱需认证，未认证时静默失败
        if (ov.data) {
          try {
            const sl = await api.get('/dashboard/sales', { params: { company_id: companyId } })
            setSales(sl.data)
          } catch { setSales(null) }
        }
      } catch (e) {
        console.error(e)
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [companyId])

  const loadReport = async () => {
    if (!verified) {
      setMessage('⚠️ 请先完成企业认证后再生成经营日报')
      return
    }
    setReportLoading(true)
    try {
      const { data } = await api.get('/dashboard/daily-report', { params: { company_id: companyId } })
      setReport(data.report)
    } catch (e) {
      setMessage(`❌ ${e.response?.data?.detail || '生成失败'}`)
    } finally {
      setReportLoading(false)
    }
  }

  if (loading) return <div>加载中...</div>

  const stats = overview ? [
    { label: '销售额', value: `¥${(overview.revenue).toLocaleString()}`, icon: '💰', path: '/quotations', title: '查看报价与销售额详情' },
    { label: '订单数', value: overview.orders, icon: '📦', path: '/quotations', title: '查看报价与订单详情' },
    { label: '生产效率', value: `${overview.production_efficiency}%`, icon: '⚙️', path: '/systems?tab=mes', title: '查看生产详情（MES 数据中心）' },
    { label: '质量良率', value: `${overview.quality_yield}%`, icon: '✅', path: '/systems?tab=qms', title: '查看质量详情（QMS 数据中心）' },
    { label: '客户数', value: overview.customers, icon: '👥', path: '/crm', title: '查看 CRM 客户列表' },
    { label: 'AI对话量', value: overview.conversations, icon: '💬', path: '/chat', title: '进入 AI 对话中心' },
  ] : []

  return (
    <div>
      <h1 style={{ fontSize: 22, marginBottom: 24 }}>企业 AI 驾驶舱</h1>

      {/* 指标卡 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 16, marginBottom: 24 }}>
        {stats.map((s) => (
          <div
            key={s.label}
            className="card"
            title={s.title}
            onClick={() => navigate(s.path)}
            style={{
              textAlign: 'center',
              cursor: 'pointer',
              transition: 'transform 0.15s, box-shadow 0.15s, border-color 0.15s',
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.transform = 'translateY(-3px)'
              e.currentTarget.style.boxShadow = '0 8px 24px rgba(0,0,0,0.12)'
              e.currentTarget.style.borderColor = 'var(--primary)'
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'translateY(0)'
              e.currentTarget.style.boxShadow = 'none'
              e.currentTarget.style.borderColor = 'var(--border)'
            }}
          >
            <div style={{ fontSize: 28 }}>{s.icon}</div>
            <div style={{ fontSize: 22, fontWeight: 700, margin: '8px 0 4px' }}>{s.value}</div>
            <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>{s.label}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        {/* AI 建议 */}
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>AI 经营建议</h3>
          {overview?.ai_suggestions?.map((s, i) => (
            <div key={i} className={`alert alert-${s.level === 'danger' ? 'error' : s.level === 'warning' ? 'yellow' : s.level === 'success' ? 'success' : 'info'}`}>
              {s.text}
            </div>
          ))}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
            <button className="btn btn-primary btn-sm" onClick={loadReport}
              disabled={reportLoading || !verified}>
              {!verified ? '🔒 请先认证' : '🤖 生成 CEO 经营日报'}
            </button>
            {reportLoading && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color: 'var(--text-muted)', fontSize: 13 }}>
                <span style={{
                  width: 14, height: 14, border: '2px solid #cbd5e1',
                  borderTopColor: 'var(--primary)', borderRadius: '50%',
                  display: 'inline-block',
                  animation: 'spin 0.8s linear infinite',
                }} />
                生成中...
              </span>
            )}
          </div>
          {message && (
            <div style={{ marginTop: 12, fontSize: 13, color: 'var(--danger, #dc2626)' }}>{message}</div>
          )}
          {report && (
            <pre style={{
              marginTop: 16, background: 'var(--bg-subtle)', padding: 16, borderRadius: 8,
              fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.8,
              maxHeight: 420, overflowY: 'auto',
            }}>
              {report}
            </pre>
          )}
        </div>

        {/* AI 销售中心 */}
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>AI 销售中心</h3>
          {sales && (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              {[
                { label: '今日发现客户', value: sales.discovered_today },
                { label: 'A级客户', value: sales.grade_a },
                { label: '发送邮件', value: sales.emails_sent },
                { label: '回复', value: sales.replies },
                { label: '商机', value: sales.opportunities },
                { label: '预计金额', value: `$${(sales.estimated_amount).toLocaleString()}` },
              ].map((s) => (
                <div key={s.label} style={{ background: 'var(--bg-subtle)', padding: 12, borderRadius: 8 }}>
                  <div style={{ fontSize: 18, fontWeight: 700 }}>{s.value}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{s.label}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
