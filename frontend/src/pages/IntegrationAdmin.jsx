import React, { useEffect, useState } from 'react'
import api from '../api'

const SYSTEM_LABELS = {
  crm: 'CRM',
  erp: 'ERP',
  mes: 'MES',
  qms: 'QMS',
}

export default function IntegrationAdmin() {
  const [overview, setOverview] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    api.get('/integrations/overview')
      .then((res) => {
        setOverview(res.data.overview || [])
        setLoaded(true)
      })
      .catch((e) => {
        setMsg('❌ 加载失败：' + (e.response?.data?.detail || e.message))
        setLoaded(true)
      })
  }, [])

  const connectedTotal = overview.reduce((sum, o) => sum + (o.connected_count || 0), 0)

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <h1 style={{ fontSize: 22, marginBottom: 4 }}>🔌 业务系统对接概览</h1>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: 0 }}>
          查看各企业对接自有 CRM / ERP / MES / QMS 系统的情况。企业管理员可在「对接业务系统」中自行配置。
        </p>
      </div>

      {msg && (
        <div className="alert alert-error" style={{ marginBottom: 16 }}>{msg}</div>
      )}

      <div className="card" style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>已对接企业</div>
            <div style={{ fontSize: 24, fontWeight: 800, color: 'var(--primary)' }}>{overview.length}</div>
          </div>
          <div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>已连通系统</div>
            <div style={{ fontSize: 24, fontWeight: 800, color: 'var(--primary)' }}>{connectedTotal}</div>
          </div>
        </div>
      </div>

      {!loaded ? (
        <div style={{ padding: 30, color: 'var(--text-muted)' }}>加载中…</div>
      ) : overview.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', padding: 40, color: 'var(--text-muted)' }}>
          暂无企业配置业务系统对接
        </div>
      ) : (
        <div className="card" style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                <th style={{ textAlign: 'left', padding: '12px 10px', color: 'var(--text-muted)', fontWeight: 600 }}>企业</th>
                <th style={{ textAlign: 'left', padding: '12px 10px', color: 'var(--text-muted)', fontWeight: 600 }}>已配置系统</th>
                <th style={{ textAlign: 'center', padding: '12px 10px', color: 'var(--text-muted)', fontWeight: 600 }}>已连通</th>
                <th style={{ textAlign: 'left', padding: '12px 10px', color: 'var(--text-muted)', fontWeight: 600 }}>状态</th>
              </tr>
            </thead>
            <tbody>
              {overview.map((o) => (
                <tr key={o.company_id} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '12px 10px' }}>
                    <div style={{ fontWeight: 700 }}>{o.company_name}</div>
                    <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>ID: {o.company_id}</div>
                  </td>
                  <td style={{ padding: '12px 10px' }}>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {o.systems.map((s) => (
                        <span key={s} className="badge badge-blue">{SYSTEM_LABELS[s] || s.toUpperCase()}</span>
                      ))}
                    </div>
                  </td>
                  <td style={{ padding: '12px 10px', textAlign: 'center' }}>
                    <span className={o.connected_count > 0 ? 'badge badge-green' : 'badge badge-gray'}>
                      {o.connected_count} / {o.total_count}
                    </span>
                  </td>
                  <td style={{ padding: '12px 10px' }}>
                    {o.connected_count === o.total_count && o.total_count > 0 ? (
                      <span className="badge badge-green">全部连通</span>
                    ) : o.connected_count > 0 ? (
                      <span className="badge badge-blue">部分连通</span>
                    ) : (
                      <span className="badge badge-gray">未连通</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
