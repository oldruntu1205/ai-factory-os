import React, { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import api from '../api'

const STATUS_MAP = {
  none: { label: '未提交', cls: 'badge-gray' },
  pending: { label: '审核中', cls: 'badge-yellow' },
  approved: { label: '已认证', cls: 'badge-green' },
  rejected: { label: '已拒绝', cls: 'badge-red' },
}

export default function PlatformAdmin() {
  const location = useLocation()
  const navigate = useNavigate()
  const tab = location.pathname.includes('/llm') ? 'llm'
    : location.pathname.includes('/verify') ? 'verify'
    : location.pathname.includes('/finance') ? 'finance'
    : location.pathname.includes('/stats') ? 'stats' : 'companies'

  const [companies, setCompanies] = useState([])
  const [verifications, setVerifications] = useState([])
  const [stats, setStats] = useState(null)
  const [finance, setFinance] = useState(null)
  const [msg, setMsg] = useState('')
  const [rejectInput, setRejectInput] = useState({})

  // 开票申请管理
  const [invReqs, setInvReqs] = useState([])
  const [issueNoMap, setIssueNoMap] = useState({})

  // 对公转账凭证审核
  const [payBills, setPayBills] = useState([])
  const [reviewNote, setReviewNote] = useState({})

  // 模型设置
  const [llm, setLlm] = useState(null)
  const [gateway, setGateway] = useState('deepseek')
  const [providers, setProviders] = useState({})
  const [saving, setSaving] = useState(false)

  const loadCompanies = async () => {
    const { data } = await api.get('/platform/companies')
    setCompanies(data.companies)
  }
  const loadVerify = async () => {
    const { data } = await api.get('/platform/verifications')
    setVerifications(data.verifications)
  }
  const loadStats = async () => {
    const { data } = await api.get('/platform/stats')
    setStats(data)
  }
  const loadFinance = async () => {
    const { data } = await api.get('/platform/finance')
    setFinance(data)
    try {
      const reqs = await api.get('/platform/invoice-requests')
      setInvReqs(reqs.data.requests || [])
    } catch (e) { /* 忽略 */ }
    try {
      const pb = await api.get('/platform/bills/pending-review')
      setPayBills(pb.data.bills || [])
    } catch (e) { /* 忽略 */ }
  }

  const reviewBill = async (billNo, action) => {
    const note = action === 'reject'
      ? (window.prompt('请输入驳回原因（客户可见）：', '凭证与到账不符，请核对后重新提交') || '')
      : (reviewNote[billNo] || '')
    if (action === 'reject' && note === null) return
    try {
      const { data } = await api.post(`/platform/bills/${billNo}/review`,
        { action, note })
      setMsg(`✅ ${data.message}`)
      loadFinance()
    } catch (e) {
      setMsg(`❌ ${e.response?.data?.detail || '审核失败'}`)
    }
  }

  const issueInvoice = async (reqId) => {
    const invoiceNo = (issueNoMap[reqId] || '').trim()
    if (!invoiceNo) {
      setMsg('❌ 请先填写发票号')
      return
    }
    try {
      const { data } = await api.post(`/platform/invoice-requests/${reqId}/issue`, null,
        { params: { invoice_no: invoiceNo } })
      setMsg(`✅ ${data.message}`)
      loadFinance()
    } catch (e) {
      setMsg(`❌ ${e.response?.data?.detail || '操作失败'}`)
    }
  }

  const rejectInvoice = async (reqId) => {
    const reason = window.prompt('请输入驳回原因（客户可见）：', '开票信息有误，请联系客服')
    if (reason === null) return
    try {
      const { data } = await api.post(`/platform/invoice-requests/${reqId}/reject`, null,
        { params: { reason } })
      setMsg(`✅ ${data.message}`)
      loadFinance()
    } catch (e) {
      setMsg(`❌ ${e.response?.data?.detail || '操作失败'}`)
    }
  }
  const loadLLM = async () => {
    try {
      const { data } = await api.get('/platform/llm-config')
      setLlm(data)
      setGateway(data.gateway)
      const provs = {}
      for (const gw of data.available) {
        const p = data.providers[gw] || {}
        provs[gw] = {
          api_key: '',
          base_url: p.base_url || '',
          model: p.model || '',
        }
      }
      setProviders(provs)
    } catch (e) {
      setMsg(`❌ 加载模型配置失败：${e.response?.data?.detail || e.message}`)
    }
  }

  useEffect(() => {
    if (tab === 'companies') loadCompanies()
    if (tab === 'verify') loadVerify()
    if (tab === 'stats') loadStats()
    if (tab === 'finance') loadFinance()
    if (tab === 'llm') loadLLM()
  }, [tab])

  const updateProvider = (gw, field, value) => {
    setProviders((prev) => ({ ...prev, [gw]: { ...prev[gw], [field]: value } }))
  }

  const saveLLM = async () => {
    setSaving(true)
    try {
      await api.put('/platform/llm-config', { gateway, providers })
      setMsg('✅ 模型配置已保存并即时生效（无需重启）')
      loadLLM()
    } catch (e) {
      setMsg(`❌ 保存失败：${e.response?.data?.detail || e.message}`)
    } finally {
      setSaving(false)
    }
  }

  const verify = async (id, approve, reason = '') => {
    try {
      const { data } = await api.post(`/platform/companies/${id}/verify`,
        { approve, reject_reason: reason })
      setMsg(`✅ ${data.message}`)
      loadVerify(); loadCompanies()
    } catch (e) {
      setMsg(`❌ ${e.response?.data?.detail || '操作失败'}`)
    }
  }

  const suspend = async (id, suspend) => {
    if (!window.confirm(suspend ? '确认挂起该企业（全员停用）？' : '确认恢复该企业？')) return
    await api.post(`/platform/companies/${id}/suspend`, { suspend })
    loadCompanies()
  }

  const tabs = [
    { key: 'companies', label: '企业管理', path: '/admin' },
    { key: 'verify', label: '认证审核', path: '/admin/verify' },
    { key: 'finance', label: '财务管理', path: '/admin/finance' },
    { key: 'stats', label: '平台统计', path: '/admin/stats' },
    { key: 'llm', label: '模型设置', path: '/admin/llm' },
  ]

  return (
    <div>
      <h1 style={{ fontSize: 22, marginBottom: 6 }}>🛡️ 平台管理</h1>
      <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 20 }}>
        平台超级管理员控制台：企业管理 · 企业认证审核 · 平台全局统计
      </p>

      {/* Tab 导航 */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 20, borderBottom: '2px solid #e2e8f0', paddingBottom: 12 }}>
        {tabs.map((t) => (
          <button key={t.key} onClick={() => navigate(t.path)}
            style={{
              padding: '8px 18px', borderRadius: 8, border: 'none', cursor: 'pointer',
              background: tab === t.key ? '#7c3aed' : 'var(--border)',
              color: tab === t.key ? '#fff' : 'var(--text-muted)', fontSize: 14, fontWeight: 600,
            }}>
            {t.label}
          </button>
        ))}
      </div>

      {msg && (msg.startsWith('✅')
        ? <div className="alert alert-success">{msg}</div>
        : <div className="alert alert-error">{msg}</div>)}

      {/* ===== 企业管理 ===== */}
      {tab === 'companies' && (
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>企业列表（{companies.length}）</h3>
          <table className="table">
            <thead><tr><th>企业</th><th>套餐</th><th>认证</th><th>成员/AI员工</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>
              {companies.map((c) => {
                const st = STATUS_MAP[c.license_status] || STATUS_MAP.none
                return (
                  <tr key={c.id}>
                    <td style={{ fontWeight: 600 }}>{c.name}</td>
                    <td><span className="badge badge-blue">{c.plan}</span></td>
                    <td><span className={`badge ${st.cls}`}>{st.label}</span></td>
                    <td>{c.members} / {c.agents}</td>
                    <td style={{ fontSize: 12 }}>{new Date(c.created_at).toLocaleDateString('zh-CN')}</td>
                    <td>
                      <button className="btn btn-secondary btn-sm"
                        onClick={() => suspend(c.id, true)}>挂起</button>
                      <button className="btn btn-secondary btn-sm" style={{ marginLeft: 6 }}
                        onClick={() => suspend(c.id, false)}>恢复</button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* ===== 认证审核 ===== */}
      {tab === 'verify' && (
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>营业执照审核队列（{verifications.length}）</h3>
          {verifications.length === 0 && (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
              暂无待审核企业 🎉
            </div>
          )}
          {verifications.map((v) => (
            <div key={v.id} style={{
              padding: 16, border: '1px solid #e2e8f0', borderRadius: 10, marginBottom: 12,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                <span style={{ fontWeight: 600, fontSize: 15 }}>🏢 {v.name}</span>
                <span className={`badge ${STATUS_MAP[v.license_status].cls}`}>
                  {STATUS_MAP[v.license_status].label}
                </span>
                <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-muted)' }}>
                  上传于 {v.uploaded_at ? new Date(v.uploaded_at).toLocaleString('zh-CN') : '-'}
                </span>
              </div>
              {v.license_status === 'rejected' && v.license_reject_reason && (
                <div style={{ fontSize: 12, color: '#b91c1c', marginBottom: 8 }}>
                  上次拒绝原因：{v.license_reject_reason}
                </div>
              )}
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <button className="btn btn-success btn-sm" onClick={() => verify(v.id, true)}>
                  ✅ 通过认证
                </button>
                <input
                  value={rejectInput[v.id] || ''}
                  onChange={(e) => setRejectInput({ ...rejectInput, [v.id]: e.target.value })}
                  placeholder="拒绝原因（可选）"
                  style={{ padding: '6px 10px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 12, width: 220 }}
                />
                <button className="btn btn-danger btn-sm"
                  onClick={() => verify(v.id, false, rejectInput[v.id] || '资料不清晰，请重新上传')}>
                  ❌ 拒绝
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ===== 平台统计 ===== */}
      {tab === 'stats' && stats && (
        <div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 16, marginBottom: 24 }}>
            {[
              { label: '入驻企业', value: stats.companies, icon: '🏢' },
              { label: '平台用户', value: stats.users, icon: '👥' },
              { label: 'AI 员工总数', value: stats.agents, icon: '🤖' },
              { label: 'AI 对话量', value: stats.conversations, icon: '💬' },
              { label: '已认证企业', value: stats.verified_companies, icon: '✅' },
              { label: '平台营收(¥)', value: stats.paid_revenue.toLocaleString(), icon: '💰' },
            ].map((s) => (
              <div key={s.label} className="card" style={{ textAlign: 'center' }}>
                <div style={{ fontSize: 26 }}>{s.icon}</div>
                <div style={{ fontSize: 22, fontWeight: 700, margin: '8px 0 4px' }}>{s.value}</div>
                <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>{s.label}</div>
              </div>
            ))}
          </div>
          <div className="card">
            <h3 style={{ marginBottom: 12 }}>平台数据总览</h3>
            <div style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 2 }}>
              · 未认证企业 {stats.unverified_companies} 家（待引导上传营业执照）
              <br />· 认证转化率 {stats.companies ? Math.round(stats.verified_companies / stats.companies * 100) : 0}%
              <br />· 平均每家 AI 员工 {stats.companies ? (stats.agents / stats.companies).toFixed(1) : 0} 个
            </div>
          </div>
        </div>
      )}

      {/* ===== 财务管理 ===== */}
      {tab === 'finance' && finance && (
        <div>
          {/* 收入汇总 */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 16, marginBottom: 20 }}>
            {[
              { label: '平台总收入', value: `¥${finance.summary.total_revenue.toLocaleString()}`, cls: '#15803d' },
              { label: '已支付账单', value: `${finance.summary.paid_count} 笔`, cls: 'var(--primary)' },
              { label: '待支付金额', value: `¥${finance.summary.pending_amount.toLocaleString()}`, cls: '#b45309' },
              { label: '待支付账单', value: `${finance.summary.pending_count} 笔`, cls: '#b45309' },
              { label: '账单总数', value: `${finance.summary.bill_count} 笔`, cls: 'var(--text-muted)' },
            ].map((s) => (
              <div key={s.label} className="card" style={{ padding: 16, textAlign: 'center' }}>
                <div style={{ fontSize: 20, fontWeight: 700, color: s.cls, marginBottom: 4 }}>{s.value}</div>
                <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>{s.label}</div>
              </div>
            ))}
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'start' }}>
            {/* 按套餐统计 */}
            <div className="card">
              <h3 style={{ marginBottom: 12 }}>按套餐收入统计</h3>
              {finance.plan_stats.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无已支付账单</div>
              ) : (
                finance.plan_stats.map((p) => (
                  <div key={p.plan} style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid #f1f5f9' }}>
                    <span style={{ fontSize: 13 }}>
                      {p.plan === 'starter' ? 'Starter' : p.plan === 'professional' ? 'Professional' : 'Enterprise'}
                      <span style={{ color: 'var(--text-muted)', marginLeft: 6 }}>({p.count} 笔)</span>
                    </span>
                    <b style={{ fontSize: 13 }}>¥{p.amount.toLocaleString()}</b>
                  </div>
                ))
              )}
              <div style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 0 0', fontWeight: 700, fontSize: 14 }}>
                <span>合计</span>
                <span>¥{finance.summary.total_revenue.toLocaleString()}</span>
              </div>
            </div>

            {/* 按月统计 */}
            <div className="card">
              <h3 style={{ marginBottom: 12 }}>月度收入（近 12 个月）</h3>
              {finance.month_stats.length === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无收入数据</div>
              ) : (
                finance.month_stats.map((m) => (
                  <div key={m.month} style={{ marginBottom: 8 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-muted)', marginBottom: 2 }}>
                      <span>{m.month}</span>
                      <span>¥{m.amount.toLocaleString()}（{m.count} 笔）</span>
                    </div>
                    <div style={{ height: 6, background: 'var(--border)', borderRadius: 3 }}>
                      <div style={{ height: 6, width: `${Math.min(m.amount / Math.max(finance.month_stats[0].amount, 1) * 100, 100)}%`, background: 'var(--primary)', borderRadius: 3 }} />
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* 流水 */}
          <div className="card" style={{ marginTop: 20 }}>
            <h3 style={{ marginBottom: 12 }}>最近账单流水（{finance.recent_bills.length}）</h3>
            <div style={{ maxHeight: 360, overflowY: 'auto' }}>
              <table className="table">
                <thead>
                  <tr><th>账单号</th><th>企业 ID</th><th>套餐</th><th>金额</th><th>状态</th><th>时间</th></tr>
                </thead>
                <tbody>
                  {finance.recent_bills.map((b) => (
                    <tr key={b.id}>
                      <td>{b.bill_no}</td>
                      <td>{b.company_id}</td>
                      <td>{b.plan === 'starter' ? 'Starter' : b.plan === 'professional' ? 'Professional' : 'Enterprise'}</td>
                      <td>¥{b.amount.toLocaleString()}</td>
                      <td>
                        <span className={`badge ${b.status === 'paid' ? 'badge-green' : 'badge-yellow'}`}>
                          {b.status === 'paid' ? '已支付' : '待支付'}
                        </span>
                      </td>
                      <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{(b.created_at || '').slice(0, 19).replace('T', ' ')}</td>
                    </tr>
                  ))}
                  {finance.recent_bills.length === 0 && (
                    <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无账单</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* 对公转账凭证审核（24h 缓冲） */}
          <div className="card" style={{ marginTop: 20 }}>
            <h3 style={{ marginBottom: 12 }}>对公转账凭证审核（{payBills.length} 笔待审核）</h3>
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12 }}>
              客户上传转账凭证后套餐已临时生效（24 小时缓冲）。请与会计核对到账：确认到账 → 「通过」（套餐正式生效）；
              未到账/凭证不符 → 「驳回」（套餐自动回退）。缓冲期结束仍未审核将自动取消升级。
            </p>
            {payBills.length === 0 ? (
              <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无待审核的转账凭证</div>
            ) : (
              <div style={{ maxHeight: 480, overflowY: 'auto' }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>账单号</th><th>企业</th><th>套餐</th><th>金额</th>
                      <th>凭证</th><th>客户备注</th><th>缓冲剩余</th><th>审核</th>
                    </tr>
                  </thead>
                  <tbody>
                    {payBills.map((b) => (
                      <tr key={b.bill_no}>
                        <td style={{ fontSize: 12 }}>{b.bill_no}</td>
                        <td style={{ fontSize: 12 }}>{b.company_id}</td>
                        <td style={{ fontSize: 12 }}>{b.plan}</td>
                        <td style={{ fontWeight: 600 }}>¥{b.amount.toLocaleString()}</td>
                        <td>
                          {b.voucher_path ? (
                            <a href={b.voucher_path} target="_blank" rel="noreferrer"
                              style={{ color: 'var(--primary)', fontSize: 12 }}>
                              查看凭证图
                            </a>
                          ) : <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>—</span>}
                        </td>
                        <td style={{ fontSize: 12, maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={b.voucher_note}>
                          {b.voucher_note || '—'}
                        </td>
                        <td>
                          <span className={`badge ${(b.grace_left_hours ?? 24) < 4 ? 'badge-red' : 'badge-yellow'}`}>
                            {(b.grace_left_hours ?? 24)} 小时
                          </span>
                        </td>
                        <td>
                          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                            <input
                              value={reviewNote[b.bill_no] || ''}
                              onChange={(e) => setReviewNote((m) => ({ ...m, [b.bill_no]: e.target.value }))}
                              placeholder="到账备注"
                              style={{ width: 100, padding: '4px 8px', borderRadius: 6, border: '1px solid var(--border-strong)', fontSize: 12 }}
                            />
                            <button className="btn btn-success btn-sm"
                              onClick={() => reviewBill(b.bill_no, 'approve')}>
                              通过
                            </button>
                            <button className="btn btn-danger btn-sm"
                              onClick={() => reviewBill(b.bill_no, 'reject')}>
                              驳回
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* 开票申请（类别：服务费） */}
          <div className="card" style={{ marginTop: 20 }}>
            <h3 style={{ marginBottom: 12 }}>开票申请管理（类别：服务费）</h3>
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12 }}>
              企业客户对已支付账单提交开票申请；财务线下开具电子发票后，在此回填发票号完成闭环。
            </p>
            {invReqs.length === 0 ? (
              <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>暂无开票申请</div>
            ) : (
              <div style={{ maxHeight: 420, overflowY: 'auto' }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>申请单号</th><th>企业</th><th>账单</th><th>金额</th><th>抬头</th>
                      <th>税号</th><th>类型</th><th>状态</th><th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invReqs.map((r) => (
                      <tr key={r.id}>
                        <td style={{ fontSize: 12 }}>{r.req_no}</td>
                        <td style={{ fontSize: 12 }}>{r.company_id}</td>
                        <td style={{ fontSize: 12 }}>{r.bill_no}</td>
                        <td style={{ fontWeight: 600 }}>¥{r.amount.toLocaleString()}</td>
                        <td style={{ fontSize: 12, maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.title}>
                          {r.title}
                        </td>
                        <td style={{ fontSize: 12 }}>{r.tax_no}</td>
                        <td style={{ fontSize: 12 }}>{r.invoice_type === 'special' ? '电子专票' : '电子普票'}</td>
                        <td>
                          <span className={`badge ${r.status === 'issued' ? 'badge-green' : r.status === 'rejected' ? 'badge-red' : 'badge-yellow'}`}>
                            {r.status === 'issued' ? '已开票' : r.status === 'rejected' ? '已驳回' : '待开票'}
                          </span>
                        </td>
                        <td>
                          {r.status === 'pending' ? (
                            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                              <input
                                value={issueNoMap[r.id] || ''}
                                onChange={(e) => setIssueNoMap((m) => ({ ...m, [r.id]: e.target.value }))}
                                placeholder="发票号"
                                style={{ width: 110, padding: '4px 8px', borderRadius: 6, border: '1px solid var(--border-strong)', fontSize: 12 }}
                              />
                              <button className="btn btn-success btn-sm"
                                onClick={() => issueInvoice(r.id)} disabled={!(issueNoMap[r.id] || '').trim()}>
                                已开
                              </button>
                              <button className="btn btn-secondary btn-sm" onClick={() => rejectInvoice(r.id)}>
                                驳回
                              </button>
                            </div>
                          ) : (
                            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                              {r.status === 'issued' ? `发票号：${r.invoice_no || '—'}` : (r.reject_reason || '已驳回')}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ===== 模型设置（LLM 网关切换） ===== */}
      {tab === 'llm' && llm && (
        <div>
          <div className="card" style={{ marginBottom: 20 }}>
            <h3 style={{ marginBottom: 12 }}>默认大模型网关</h3>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 14 }}>
              选择平台统一调用的底层大模型。切换后实时生效，正在进行的对话将在下次请求时使用新模型。
            </p>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              {llm.available.map((gw) => {
                const active = gw === gateway
                return (
                  <button key={gw} onClick={() => setGateway(gw)}
                    style={{
                      padding: '10px 18px', borderRadius: 10, cursor: 'pointer', fontSize: 14, fontWeight: 600,
                      border: active ? '2px solid #7c3aed' : '1px solid #cbd5e1',
                      background: active ? '#f5f3ff' : '#fff',
                      color: active ? '#7c3aed' : 'var(--text-ink)',
                    }}>
                    {llm.labels[gw] || gw}
                    {active && <span style={{ marginLeft: 8 }}>✓</span>}
                  </button>
                )
              })}
            </div>
          </div>

          <div className="card" style={{ marginBottom: 20 }}>
            <h3 style={{ marginBottom: 6 }}>各模型 API 对接设置</h3>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16 }}>
              API Key 留空表示「保持原有配置不变」；若要更换 Key 请直接填入新值。Base URL 与模型名留空则使用各厂商默认值。
            </p>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 }}>
              {llm.available.map((gw) => {
                const p = providers[gw] || {}
                const info = llm.providers[gw] || {}
                return (
                  <div key={gw} style={{
                    border: gateway === gw ? '2px solid #7c3aed' : '1px solid #e2e8f0',
                    borderRadius: 12, padding: 16, background: gateway === gw ? '#faf8ff' : '#fff',
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                      <b style={{ fontSize: 15 }}>{llm.labels[gw] || gw}</b>
                      {gateway === gw && <span className="badge badge-purple" style={{ fontSize: 11 }}>当前生效</span>}
                      {info.api_key_set && (
                        <span style={{ marginLeft: 'auto', fontSize: 11, color: '#15803d' }}>
                          已配置 · {info.api_key_mask}
                        </span>
                      )}
                    </div>
                    <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                      API Key
                    </label>
                    <input type="password" value={p.api_key || ''}
                      onChange={(e) => updateProvider(gw, 'api_key', e.target.value)}
                      placeholder={info.api_key_set ? `已配置（${info.api_key_mask}），留空保持不变` : '请输入 API Key'}
                      style={fieldStyle} />
                    <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', margin: '12px 0 4px' }}>
                      Base URL（可选）
                    </label>
                    <input value={p.base_url || ''}
                      onChange={(e) => updateProvider(gw, 'base_url', e.target.value)}
                      placeholder={llm.defaults[gw]?.base_url || ''}
                      style={fieldStyle} />
                    <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', margin: '12px 0 4px' }}>
                      模型名（可选）
                    </label>
                    <input value={p.model || ''}
                      onChange={(e) => updateProvider(gw, 'model', e.target.value)}
                      placeholder={llm.defaults[gw]?.model || ''}
                      style={fieldStyle} />
                  </div>
                )
              })}
            </div>
          </div>

          <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
            <button className="btn btn-primary" onClick={saveLLM} disabled={saving}>
              {saving ? '保存中…' : '💾 保存并应用'}
            </button>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
              未配置任何 Key 时系统自动降级为内置演示模型（保证全链路可运行）
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

const fieldStyle = {
  width: '100%', padding: '9px 12px', borderRadius: 8,
  border: '1px solid #cbd5e1', fontSize: 13, boxSizing: 'border-box',
}