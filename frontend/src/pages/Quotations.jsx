import React, { useEffect, useState } from 'react'
import api from '../api'
import PageHeader from '../components/PageHeader'

const STATUS_MAP = {
  draft: ['badge-gray', '草稿'],
  sent: ['badge-blue', '已发送'],
  accepted: ['badge-green', '已成交'],
  rejected: ['badge-red', '已拒绝'],
}

export default function Quotations() {
  const [quotes, setQuotes] = useState([])
  const [customers, setCustomers] = useState([])
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ requirement: '', customer_id: '' })
  const [creating, setCreating] = useState(false)
  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const [q, c] = await Promise.all([
      api.get('/quotation/list', { params: { company_id: companyId } }),
      api.get('/crm/customers', { params: { company_id: companyId } }),
    ])
    setQuotes(q.data.quotations)
    setCustomers(c.data.customers)
  }

  useEffect(() => { load() }, [])

  const create = async () => {
    setCreating(true)
    try {
      await api.post('/quotation/create', {
        requirement: form.requirement,
        company_id: companyId,
        customer_id: form.customer_id ? Number(form.customer_id) : null,
      })
      setShowForm(false)
      setForm({ requirement: '', customer_id: '' })
      load()
    } catch (e) {
      alert(e.response?.data?.detail || '生成报价失败')
    } finally {
      setCreating(false)
    }
  }

  const updateStatus = async (id, status) => {
    try {
      await api.put(`/quotation/${id}/status`, {}, { params: { status, company_id: companyId } })
      load()
    } catch (e) {
      alert(e.response?.data?.detail || '更新状态失败')
      load() // 失败也重新加载，把下拉框还原到服务器真实状态
    }
  }

  return (
    <div>
      <PageHeader title="AI 报价单">
        <button className="btn btn-primary btn-sm" onClick={() => setShowForm(!showForm)}>
          {showForm ? '取消' : '+ AI 生成报价'}
        </button>
      </PageHeader>

      {showForm && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr auto', gap: 12, alignItems: 'end' }}>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label>客户需求</label>
              <textarea rows={3} value={form.requirement}
                onChange={(e) => setForm({ ...form, requirement: e.target.value })}
                placeholder="如：需要采购1000个500ml玻璃烧杯，硼硅酸盐材质，30天内交货" />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label>关联客户（可选）</label>
              <select value={form.customer_id} onChange={(e) => setForm({ ...form, customer_id: e.target.value })}>
                <option value="">未关联</option>
                {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </div>
            <button className="btn btn-success" onClick={create} disabled={creating}>
              {creating ? 'AI 生成中...' : '生成报价单'}
            </button>
          </div>
        </div>
      )}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>报价单号</th>
              <th>客户</th>
              <th>产品</th>
              <th>数量</th>
              <th>单价</th>
              <th>总额</th>
              <th>条款/HS</th>
              <th>运费/关税</th>
              <th>交期</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {quotes.map((q) => {
              const [cls, label] = STATUS_MAP[q.status] || STATUS_MAP.draft
              return (
                <tr key={q.id}>
                  <td style={{ fontSize: 12 }}>{q.quotation_no}</td>
                  <td>{q.customer_name}</td>
                  <td>{q.product}</td>
                  <td>{q.quantity}</td>
                  <td>{q.currency} {q.unit_price?.toLocaleString?.() ?? q.unit_price}</td>
                  <td style={{ fontWeight: 600 }}>{q.currency} {q.total_amount?.toLocaleString?.() ?? q.total_amount}</td>
                  <td style={{ fontSize: 12 }}>
                    <span className="badge" style={{ background: 'rgba(77,139,255,0.12)' }}>{q.incoterms || 'FOB'}</span>
                    {q.hs_code && <div style={{ color: 'var(--text-muted)', fontSize: 11 }}>HS {q.hs_code}</div>}
                  </td>
                  <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                    {q.freight_amount > 0 ? `运 $${q.freight_amount?.toLocaleString?.() ?? q.freight_amount}` : '—'}
                    {q.tariff_pct > 0 ? ` / 税 ${q.tariff_pct}%` : ''}
                  </td>
                  <td>{q.delivery_days}天</td>
                  <td><span className={`badge ${cls}`}>{label}</span></td>
                  <td>
                    <select
                      value={q.status}
                      onChange={(e) => updateStatus(q.id, e.target.value)}
                      style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 12 }}
                    >
                      <option value="draft">草稿</option>
                      <option value="sent">已发送</option>
                      <option value="accepted">已成交</option>
                      <option value="rejected">已拒绝</option>
                    </select>
                  </td>
                </tr>
              )
            })}
            {quotes.length === 0 && (
              <tr><td colSpan={9} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无报价单，点击右上角让 AI 生成</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
