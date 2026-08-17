import React, { useEffect, useState } from 'react'
import api from '../api'

const PLAN_BADGES = {
  starter: 'badge-gray',
  professional: 'badge-blue',
  enterprise: 'badge-green',
}

export default function Billing() {
  const [plans, setPlans] = useState({})
  const [usage, setUsage] = useState(null)
  const [invoices, setInvoices] = useState([])
  const [installed, setInstalled] = useState([])
  const [msg, setMsg] = useState('')
  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const [p, u, i, inst] = await Promise.all([
      api.get('/billing/plans'),
      api.get('/billing/usage', { params: { company_id: companyId } }),
      api.get('/billing/invoices', { params: { company_id: companyId } }),
      api.get('/marketplace/installed', { params: { company_id: companyId } }),
    ])
    setPlans(p.data.plans)
    setUsage(u.data)
    setInvoices(i.data.invoices)
    setInstalled(inst.data.installed || [])
  }

  useEffect(() => { load() }, [])

  const upgrade = async (planKey) => {
    if (!window.confirm(`确认升级到 ${plans[planKey].name} 套餐？`)) return
    try {
      const { data } = await api.post('/billing/upgrade', {
        company_id: companyId, plan: planKey,
      })
      setMsg(`✅ ${data.message}（账单号 ${data.bill_no}，¥${data.amount}）`)
      load()
    } catch (e) {
      setMsg(`❌ ${e.response?.data?.detail || '升级失败'}`)
    }
  }

  const pay = async (billNo) => {
    if (!window.confirm('模拟支付该账单？')) return
    await api.post(`/billing/pay?bill_no=${billNo}`)
    setMsg('✅ 支付成功')
    load()
  }

  if (!usage) return <div>加载中...</div>

  return (
    <div>
      <h1 style={{ fontSize: 22, marginBottom: 6 }}>💳 套餐与账单</h1>
      <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 12 }}>
        SaaS 计费：套餐升级、用量统计、账单管理
      </p>
      {/* 两层计费模型说明 */}
      <div className="alert alert-info" style={{ fontSize: 13, marginBottom: 20, lineHeight: 1.7 }}>
        <b>计费模型：底座套餐 + 技能包</b><br />
        • <b>底座套餐</b>（下方「套餐选择」）决定你的 <b>AI 员工席位数量</b> 与平台级能力（容量、部署、支持），是平台使用权。<br />
        • <b>技能包</b>（Agent 商城）在席位内一键部署行业 AI 员工与工具，单独按月计费。<br />
        • 两者是「容器（容量）」与「内容（能力）」的关系：底座决定能装多少，技能包决定装什么，互不冲突。
      </div>

      {msg && (msg.startsWith('✅')
        ? <div className="alert alert-success">{msg}</div>
        : <div className="alert alert-error">{msg}</div>)}

      {/* 当前用量 */}
      <div className="card" style={{ marginBottom: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <h3 style={{ margin: 0 }}>当前用量</h3>
            <span className={`badge ${PLAN_BADGES[usage.plan] || 'badge-gray'}`}>
              当前套餐：{usage.plan_name}
            </span>
          </div>
          {/* 套餐时间范围 + 剩余天数 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12, color: 'var(--text-muted)' }}>
            {usage.plan_started_at && usage.plan_expires_at ? (
              <>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                  📅 {usage.plan_started_at.slice(0, 10)} ~ {usage.plan_expires_at.slice(0, 10)}
                </span>
                {usage.expired ? (
                  <span className="badge badge-red">⛔ 已过期 {Math.abs(usage.remaining_days || 0)} 天</span>
                ) : (
                  <span className="badge badge-green">⏳ 剩余 {usage.remaining_days} 天</span>
                )}
              </>
            ) : (
              <span className="badge badge-gray">套餐永久有效</span>
            )}
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 16 }}>
          <div style={{ background: 'var(--bg-subtle)', borderRadius: 10, padding: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>AI 对话（本月）</div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>
              {usage.conversations.used}<span style={{ fontSize: 13, color: 'var(--text-muted)' }}> / {usage.conversations.limit}</span>
            </div>
            <div style={{ height: 6, background: 'var(--border)', borderRadius: 3, marginTop: 8 }}>
              <div style={{ height: 6, width: `${usage.percent}%`, background: usage.percent > 80 ? '#ef4444' : 'var(--primary)', borderRadius: 3 }} />
            </div>
          </div>
          <div style={{ background: 'var(--bg-subtle)', borderRadius: 10, padding: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>AI 员工席位</div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>
              {usage.agents.used}<span style={{ fontSize: 13, color: 'var(--text-muted)' }}> / {usage.agents.limit}</span>
            </div>
            {usage.agents.over && (
              <div style={{ fontSize: 11, color: '#b91c1c', marginTop: 4 }}>
                已超出席位，请升级底座套餐或卸载技能包
              </div>
            )}
          </div>
          <div style={{ background: 'var(--bg-subtle)', borderRadius: 10, padding: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>知识文档</div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{usage.documents}</div>
          </div>
          <div style={{ background: 'var(--bg-subtle)', borderRadius: 10, padding: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>客户数</div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{usage.customers}</div>
          </div>
          <div style={{ background: 'var(--bg-subtle)', borderRadius: 10, padding: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>报价单</div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{usage.quotations}</div>
          </div>
        </div>
      </div>

      {/* 套餐选择 */}
      <h3 style={{ marginBottom: 12 }}>套餐选择</h3>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 16, marginBottom: 24 }}>
        {Object.entries(plans).map(([key, p]) => {
          const isCurrent = usage.plan === key
          const isEnterprise = key === 'enterprise'
          return (
            <div key={key} className="card" style={{
              border: isCurrent ? '2px solid #2563eb' : '1px solid #e2e8f0',
              position: 'relative',
              display: 'flex',
              flexDirection: 'column',
              minHeight: 240,
            }}>
              {isCurrent && (
                <span style={{
                  position: 'absolute', top: -10, right: 12, background: 'var(--primary)',
                  color: '#fff', fontSize: 11, padding: '2px 10px', borderRadius: 999,
                }}>
                  当前套餐
                </span>
              )}
              <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 4 }}>{p.name}</div>
              <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--primary)', marginBottom: 12 }}>
                ¥{p.price.toLocaleString()}
                <span style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 400 }}>/{p.period}</span>
              </div>
              <ul style={{ listStyle: 'none', fontSize: 13, color: 'var(--text-muted)', marginBottom: 16, flex: 1 }}>
                {p.features.map((f) => (
                  <li key={f} style={{ padding: '4px 0' }}>✓ {f}</li>
                ))}
              </ul>
              {!isCurrent && (
                <button className={`btn ${isEnterprise ? 'btn-success' : 'btn-primary'}`}
                  style={{ width: '100%', marginTop: 'auto' }} onClick={() => upgrade(key)}>
                  {isEnterprise ? '联系销售' : `升级到 ${p.name}`}
                </button>
              )}
            </div>
          )
        })}
      </div>

      {/* 已订阅技能包（两层模型：在底座席位内填充能力） */}
      <h3 style={{ margin: '8px 0 12px' }}>已订阅技能包</h3>
      <div className="card" style={{ marginBottom: 24 }}>
        {installed.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>
            尚未订阅任何技能包。前往 <a href="/marketplace" style={{ color: 'var(--primary)' }}>Agent 商城</a> 在席位内部署行业 AI 员工。
          </div>
        ) : (
          <>
            <table className="table">
              <thead><tr><th>技能包</th><th>AI 员工</th><th>月费</th><th>安装时间</th></tr></thead>
              <tbody>
                {installed.map((p) => (
                  <tr key={p.package_id}>
                    <td>
                      <span style={{ marginRight: 6 }}>{p.icon}</span>
                      {p.package_name}
                    </td>
                    <td>{p.agent_count} 个</td>
                    <td style={{ fontWeight: 600 }}>¥{(p.price || 0).toLocaleString()}/月</td>
                    <td style={{ fontSize: 12 }}>{new Date(p.installed_at).toLocaleDateString('zh-CN')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
              <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
                技能包月度合计（在底座席位内，不另占平台容量）
              </span>
              <span style={{ fontSize: 16, fontWeight: 700, color: 'var(--primary)' }}>
                ¥{installed.reduce((s, p) => s + (p.price || 0), 0).toLocaleString()}/月
              </span>
            </div>
          </>
        )}
      </div>

      {/* 账单历史 */}
      <div className="card">
        <h3 style={{ marginBottom: 16 }}>账单历史</h3>
        <table className="table">
          <thead><tr><th>账单号</th><th>套餐</th><th>金额</th><th>状态</th><th>日期</th><th>操作</th></tr></thead>
          <tbody>
            {invoices.map((b) => (
              <tr key={b.id}>
                <td style={{ fontSize: 12 }}>{b.bill_no}</td>
                <td><span className="badge badge-blue">{b.plan}</span></td>
                <td style={{ fontWeight: 600 }}>{b.currency} {b.amount.toLocaleString()}</td>
                <td>
                  <span className={`badge ${b.status === 'paid' ? 'badge-green' : 'badge-yellow'}`}>
                    {b.status === 'paid' ? '已支付' : '待支付'}
                  </span>
                </td>
                <td style={{ fontSize: 12 }}>{new Date(b.created_at).toLocaleDateString('zh-CN')}</td>
                <td>
                  {b.status !== 'paid' && (
                    <button className="btn btn-success btn-sm" onClick={() => pay(b.bill_no)}>
                      模拟支付
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {invoices.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无账单，升级套餐后生成</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}