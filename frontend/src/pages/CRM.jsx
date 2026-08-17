import React, { useEffect, useState } from 'react'
import api from '../api'
import PageHeader from '../components/PageHeader'

const STATUS_MAP = {
  new: ['badge-blue', '新线索'],
  following: ['badge-yellow', '跟进中'],
  quoted: ['badge-blue', '已报价'],
  won: ['badge-green', '已成交'],
  lost: ['badge-red', '已流失'],
}

export default function CRM() {
  const [customers, setCustomers] = useState([])
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', country: '', email: '', whatsapp: '', requirement: '' })
  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const { data } = await api.get('/crm/customers', { params: { company_id: companyId } })
    setCustomers(data.customers)
  }

  useEffect(() => { load() }, [])

  const create = async () => {
    await api.post('/crm/customer', { ...form, company_id: companyId })
    setShowForm(false)
    setForm({ name: '', country: '', email: '', whatsapp: '', requirement: '' })
    load()
  }

  const updateStatus = async (id, status) => {
    await api.put(`/crm/customer/${id}`, { status }, { params: { company_id: companyId } })
    load()
  }

  return (
    <div>
      <PageHeader title="CRM 客户管理">
        <button className="btn btn-primary btn-sm" onClick={() => setShowForm(!showForm)}>
          {showForm ? '取消' : '+ 添加客户'}
        </button>
      </PageHeader>

      {showForm && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 12 }}>
            <div className="form-group"><label>客户名称</label>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
            <div className="form-group"><label>国家</label>
              <input value={form.country} onChange={(e) => setForm({ ...form, country: e.target.value })} /></div>
            <div className="form-group"><label>邮箱</label>
              <input value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></div>
            <div className="form-group"><label>客户 Whatsapp</label>
              <input value={form.whatsapp} onChange={(e) => setForm({ ...form, whatsapp: e.target.value })} /></div>
            <div className="form-group"><label>需求</label>
              <input value={form.requirement} onChange={(e) => setForm({ ...form, requirement: e.target.value })} /></div>
          </div>
          <button className="btn btn-success" onClick={create}>保存客户</button>
        </div>
      )}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>客户</th>
              <th>国家</th>
              <th>需求</th>
              <th>来源</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {customers.map((c) => {
              const [cls, label] = STATUS_MAP[c.status] || STATUS_MAP.new
              return (
                <tr key={c.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{c.name}</div>
                    <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      {c.email}
                      {c.whatsapp && <span style={{ marginLeft: 8 }}>WhatsApp: {c.whatsapp}</span>}
                    </div>
                  </td>
                  <td>{c.country}</td>
                  <td style={{ fontSize: 13 }}>{c.requirement}</td>
                  <td><span className="badge badge-gray">{c.source}</span></td>
                  <td><span className={`badge ${cls}`}>{label}</span></td>
                  <td>
                    <select
                      value={c.status}
                      onChange={(e) => updateStatus(c.id, e.target.value)}
                      style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 12 }}
                    >
                      <option value="new">新线索</option>
                      <option value="following">跟进中</option>
                      <option value="quoted">已报价</option>
                      <option value="won">已成交</option>
                      <option value="lost">已流失</option>
                    </select>
                  </td>
                </tr>
              )
            })}
            {customers.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无客户，AI 销售对话后会自动创建线索</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
