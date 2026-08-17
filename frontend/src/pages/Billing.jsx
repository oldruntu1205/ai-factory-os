import React, { useEffect, useRef, useState } from 'react'
import api from '../api'

const PLAN_BADGES = {
  starter: 'badge-gray',
  professional: 'badge-blue',
  enterprise: 'badge-green',
}

const PAY_STATUS_MAP = {
  unpaid: ['badge-yellow', '待支付'],
  pending_review: ['badge-blue', '待财务审核'],
  paid: ['badge-green', '已支付'],
  rejected: ['badge-red', '审核驳回'],
  expired: ['badge-red', '缓冲过期'],
}

export default function Billing() {
  const [plans, setPlans] = useState({})
  const [usage, setUsage] = useState(null)
  const [invoices, setInvoices] = useState([])
  const [installed, setInstalled] = useState([])
  const [msg, setMsg] = useState('')
  const [invReqs, setInvReqs] = useState([])
  const [payAccount, setPayAccount] = useState(null)
  const [payAccountDemo, setPayAccountDemo] = useState(true)

  // 付款弹窗（对公转账）
  const [payBill, setPayBill] = useState(null)
  const [voucherFile, setVoucherFile] = useState(null)
  const [voucherNote, setVoucherNote] = useState('')
  const [paying, setPaying] = useState(false)
  const [copied, setCopied] = useState(false)
  const voucherRef = useRef(null)

  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const [p, u, i, inst, acc] = await Promise.all([
      api.get('/billing/plans'),
      api.get('/billing/usage', { params: { company_id: companyId } }),
      api.get('/billing/invoices', { params: { company_id: companyId } }),
      api.get('/marketplace/installed', { params: { company_id: companyId } }),
      api.get('/billing/pay-account').catch(() => ({ data: { account: null, is_demo: true } })),
    ])
    setPlans(p.data.plans)
    setUsage(u.data)
    setInvoices(i.data.invoices)
    setInstalled(inst.data.installed || [])
    setPayAccount(acc.data.account)
    setPayAccountDemo(acc.data.is_demo !== false)
    return i.data.invoices
  }

  const loadInvoice = async () => {
    try {
      const reqs = await api.get('/billing/invoice-requests', { params: { company_id: companyId } })
      setInvReqs(reqs.data.requests || [])
    } catch (e) { /* 忽略 */ }
  }

  useEffect(() => { load(); loadInvoice() }, [])

  const upgrade = async (planKey) => {
    if (!window.confirm(`确认升级到 ${plans[planKey].name} 套餐？`)) return
    try {
      const { data } = await api.post('/billing/upgrade', {
        company_id: companyId, plan: planKey,
      })
      setMsg(`✅ ${data.message}（账单号 ${data.bill_no}，¥${data.amount}）`)
      const fresh = (await load()).find((b) => b.bill_no === data.bill_no)
      if (fresh) openPay(fresh)
    } catch (e) {
      setMsg(`❌ ${e.response?.data?.detail || '升级失败'}`)
    }
  }

  const openPay = (bill) => {
    setPayBill(bill)
    setVoucherFile(null)
    setVoucherNote('')
    setCopied(false)
    if (voucherRef.current) voucherRef.current.value = ''
  }

  const closePay = () => setPayBill(null)

  const copyAccount = async () => {
    if (!payAccount) return
    const text = `收款单位：${payAccount.account_name}\n开户银行：${payAccount.bank_name}\n账号：${payAccount.account_no}\n${payAccount.note || ''}`
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (e) {
      // 兼容非 HTTPS 环境：降级选中提示
      window.prompt('请复制以下付款信息：', text)
    }
  }

  const submitPay = async () => {
    if (!voucherFile) {
      setMsg('❌ 请先上传转账凭证截图')
      return
    }
    setPaying(true)
    try {
      const fd = new FormData()
      fd.append('bill_no', payBill.bill_no)
      fd.append('voucher', voucherFile)
      fd.append('note', voucherNote)
      const { data } = await api.post('/billing/pay', fd)
      setMsg(`✅ ${data.message}`)
      setPayBill(null)
      load()
    } catch (e) {
      setMsg(`❌ ${e.response?.data?.detail || '提交失败'}`)
    } finally {
      setPaying(false)
    }
  }

  const applyInvoice = async (billNo) => {
    if (!window.confirm('对该已支付账单提交开票申请？（开票类别：服务费）')) return
    try {
      const { data } = await api.post('/billing/invoice-request', { bill_no: billNo })
      setMsg(`✅ ${data.message}`)
      loadInvoice()
    } catch (e) {
      const detail = e.response?.data?.detail || '申请失败'
      if (detail.includes('开票信息')) {
        setMsg(`❌ ${detail}，请先到「企业设置 → 开票信息」填写`)
      } else {
        setMsg(`❌ ${detail}`)
      }
    }
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
          <thead><tr><th>账单号</th><th>套餐</th><th>金额</th><th>状态</th><th>开票状态</th><th>日期</th><th>操作</th></tr></thead>
          <tbody>
            {invoices.map((b) => {
              const hasReq = invReqs.some((r) => r.bill_no === b.bill_no)
              const inv = b.invoice || {}
              const paySt = PAY_STATUS_MAP[b.status] || ['badge-gray', b.status]
              return (
                <tr key={b.id}>
                  <td style={{ fontSize: 12 }}>{b.bill_no}</td>
                  <td><span className="badge badge-blue">{b.plan}</span></td>
                  <td style={{ fontWeight: 600 }}>{b.currency} {b.amount.toLocaleString()}</td>
                  <td>
                    <span className={`badge ${paySt[0]}`}>{paySt[1]}</span>
                    {b.status === 'pending_review' && b.grace_expires_at && (
                      <div style={{ fontSize: 11, color: '#b45309', marginTop: 2 }}>
                        缓冲至 {new Date(b.grace_expires_at).toLocaleString('zh-CN')}
                      </div>
                    )}
                    {(b.status === 'rejected' || b.status === 'expired') && b.review_note && (
                      <div style={{ fontSize: 11, color: '#b91c1c', marginTop: 2, maxWidth: 150 }}>
                        {b.review_note}
                      </div>
                    )}
                    {b.status === 'pending_review' && b.voucher_path && (
                      <div style={{ marginTop: 4 }}>
                        <a href={b.voucher_path} target="_blank" rel="noreferrer"
                          style={{ fontSize: 11, color: 'var(--primary)' }}>查看凭证</a>
                      </div>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${inv.status === 'issued' ? 'badge-green' : inv.status === 'rejected' ? 'badge-red' : inv.status === 'pending' ? 'badge-yellow' : 'badge-gray'}`}>
                      {inv.status_label || '未申请'}
                    </span>
                    {inv.status === 'rejected' && inv.reject_reason && (
                      <div style={{ fontSize: 11, color: '#b91c1c', marginTop: 2, maxWidth: 140 }}>
                        {inv.reject_reason}
                      </div>
                    )}
                  </td>
                  <td style={{ fontSize: 12 }}>{new Date(b.created_at).toLocaleDateString('zh-CN')}</td>
                  <td style={{ display: 'flex', gap: 6 }}>
                    {b.status === 'unpaid' && (
                      <button className="btn btn-success btn-sm" onClick={() => openPay(b)}>
                        去付款
                      </button>
                    )}
                    {b.status === 'pending_review' && (
                      <span className="badge badge-gray" style={{ alignSelf: 'center' }}>财务审核中</span>
                    )}
                    {(b.status === 'rejected' || b.status === 'expired') && (
                      <span className="badge badge-gray" style={{ alignSelf: 'center' }}>未生效</span>
                    )}
                    {b.status === 'paid' && !hasReq && (
                      <button className="btn btn-primary btn-sm" onClick={() => applyInvoice(b.bill_no)}>
                        申请开票
                      </button>
                    )}
                    {b.status === 'paid' && hasReq && (
                      <span className="badge badge-gray" style={{ alignSelf: 'center' }}>已申请开票</span>
                    )}
                  </td>
                </tr>
              )
            })}
            {invoices.length === 0 && (
              <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无账单，升级套餐后生成</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 开票信息入口提示（开票资料维护已统一收敛到「企业设置 → 开票信息」） */}
      <div className="card" style={{ marginBottom: 24 }}>
        <h3 style={{ marginBottom: 6 }}>开票信息</h3>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.8, marginBottom: 0 }}>
          开票资料请在
          <a href="/settings?tab=invoice" style={{ color: 'var(--primary)', fontWeight: 600 }}> 企业设置 → 开票信息 </a>
          中维护；填写后可对已支付账单提交开票申请，电子发票开具后可在下方申请记录中下载。
        </p>
      </div>

      {/* 开票申请记录 */}
      <div className="card">
        <h3 style={{ marginBottom: 16 }}>开票申请记录</h3>
        {invReqs.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>
            暂无开票申请。对已支付账单点击「申请开票」即可提交。
          </div>
        ) : (
          <table className="table">
            <thead><tr><th>申请单号</th><th>关联账单</th><th>金额</th><th>类别</th><th>发票类型</th><th>状态</th><th>发票号</th><th>申请时间</th></tr></thead>
            <tbody>
              {invReqs.map((r) => (
                <tr key={r.id}>
                  <td style={{ fontSize: 12 }}>{r.req_no}</td>
                  <td style={{ fontSize: 12 }}>{r.bill_no}</td>
                  <td style={{ fontWeight: 600 }}>¥{r.amount.toLocaleString()}</td>
                  <td><span className="badge badge-blue">{r.category}</span></td>
                  <td style={{ fontSize: 12 }}>{r.invoice_type === 'special' ? '电子专票' : '电子普票'}</td>
                  <td>
                    <span className={`badge ${r.status === 'issued' ? 'badge-green' : r.status === 'rejected' ? 'badge-red' : 'badge-yellow'}`}>
                      {r.status === 'issued' ? '已开票' : r.status === 'rejected' ? '已驳回' : '待开票'}
                    </span>
                  </td>
                  <td style={{ fontSize: 12 }}>
                    {r.invoice_no || '—'}
                    {r.status === 'issued' && r.pdf_url && (
                      <div style={{ marginTop: 2 }}>
                        <a href={r.pdf_url} target="_blank" rel="noreferrer"
                          style={{ color: 'var(--primary)', fontSize: 11 }}>下载电子发票</a>
                      </div>
                    )}
                  </td>
                  <td style={{ fontSize: 12 }}>{new Date(r.created_at).toLocaleDateString('zh-CN')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {invReqs.some((r) => r.status === 'rejected' && r.reject_reason) && (
          <div style={{ marginTop: 10, fontSize: 12, color: '#b91c1c' }}>
            驳回原因：{invReqs.filter((r) => r.status === 'rejected' && r.reject_reason).map((r) => r.reject_reason).join('；')}
          </div>
        )}
      </div>

      {/* 对公转账付款弹窗 */}
      {payBill && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }} onClick={closePay}>
          <div className="card" style={{ width: 520, maxHeight: '90vh', overflowY: 'auto' }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
              <h3 style={{ margin: 0 }}>💳 对公转账付款</h3>
              <button className="btn btn-secondary btn-sm" onClick={closePay}>✕</button>
            </div>

            <div style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 14, lineHeight: 1.7 }}>
              账单 <b>{payBill.bill_no}</b> · {payBill.plan} 套餐 · 金额
              <b style={{ color: 'var(--primary)' }}> ¥{payBill.amount.toLocaleString()}</b>
            </div>

            {/* 付款账号（演示数据）+ 复制按钮 */}
            <div style={{ background: 'var(--bg-subtle)', borderRadius: 10, padding: 14, marginBottom: 14 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <div style={{ fontSize: 13, fontWeight: 700 }}>对公收款账户</div>
                {payAccountDemo && <span className="badge badge-gray" style={{ fontSize: 10 }}>演示数据</span>}
              </div>
              {payAccount ? (
                <>
                  <div style={{ fontSize: 13, lineHeight: 1.9 }}>
                    <div>收款单位：<b>{payAccount.account_name}</b></div>
                    <div>开户银行：{payAccount.bank_name}</div>
                    <div>账号：<b style={{ color: 'var(--primary)', letterSpacing: 0.5 }}>{payAccount.account_no}</b></div>
                    {payAccount.note && <div style={{ fontSize: 12, color: '#b45309' }}>⚠ {payAccount.note}</div>}
                  </div>
                  <button className="btn btn-secondary btn-sm" style={{ marginTop: 10 }}
                    onClick={copyAccount}>
                    {copied ? '✅ 已复制' : '📋 复制付款账号'}
                  </button>
                  {payAccountDemo && (
                    <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-muted)' }}>
                      当前为<b>演示收款账户</b>，正式收费启用前不会产生真实扣款；请按平台通知使用。
                    </div>
                  )}
                </>
              ) : (
                <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>付款账户信息加载中...</div>
              )}
            </div>

            {/* 上传付款凭证 */}
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 6 }}>
                上传转账凭证截图 <span style={{ color: '#b91c1c' }}>*</span>
              </div>
              <input
                ref={voucherRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                onChange={(e) => setVoucherFile(e.target.files[0] || null)}
                style={{ fontSize: 12, width: '100%' }}
              />
              {voucherFile && (
                <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 10 }}>
                  <img src={URL.createObjectURL(voucherFile)} alt="凭证预览"
                    style={{ width: 72, height: 72, objectFit: 'cover', borderRadius: 8, border: '1px solid var(--border)' }} />
                  <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                    {voucherFile.name}（{(voucherFile.size / 1024 / 1024).toFixed(1)} MB）
                  </div>
                </div>
              )}
            </div>

            <div className="form-group" style={{ marginBottom: 16 }}>
              <label>转账备注（选填，如转账时间/流水号）</label>
              <input value={voucherNote} onChange={(e) => setVoucherNote(e.target.value)}
                placeholder="例：2026-08-18 上午转账，备注 BILL 单号" />
            </div>

            <div className="alert alert-info" style={{ fontSize: 12, lineHeight: 1.8, marginBottom: 14 }}>
              <b>24 小时缓冲期：</b>上传凭证后套餐<b>立即临时生效</b>；平台财务核对到账后正式确认。
              若 24 小时内未获审核通过，将自动取消本次升级（套餐回退），请留意审核结果。
            </div>

            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary" onClick={closePay}>取消</button>
              <button className="btn btn-success" onClick={submitPay} disabled={paying || !voucherFile}>
                {paying ? '提交中...' : '提交凭证'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}