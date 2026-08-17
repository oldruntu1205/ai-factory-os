import React, { useEffect, useState, useCallback } from 'react'
import api from '../api'

const AUTH_LABELS = {
  none: '无鉴权',
  basic: '账号密码 (Basic)',
  apikey: 'API Key',
  bearer: 'Bearer Token',
}

const STATUS_META = {
  unconfigured: { text: '未配置', cls: 'badge-gray' },
  configured: { text: '已配置', cls: 'badge-blue' },
  ok: { text: '已连通', cls: 'badge-green' },
  error: { text: '异常', cls: 'badge-red' },
}

const emptyConfig = () => ({
  enabled: false, endpoint: '', auth_type: 'none',
  api_key: '', username: '', password: '', extra: {},
  status: 'unconfigured', last_error: '', last_tested_at: null,
})

export default function IntegrationSettings() {
  const [systems, setSystems] = useState([])
  const [configs, setConfigs] = useState({})
  const [loaded, setLoaded] = useState(false)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState({})

  useEffect(() => {
    Promise.all([
      api.get('/integrations/systems'),
      api.get('/integrations'),
    ]).then(([s, r]) => {
      const sysList = s.data.systems || []
      setSystems(sysList)
      const map = {}
      ;(r.data.integrations || []).forEach((it) => { map[it.system_type] = it })
      const full = {}
      sysList.forEach((s) => { full[s.key] = { ...emptyConfig(), ...(map[s.key] || {}) } })
      setConfigs(full)
      setLoaded(true)
    }).catch((e) => setMsg('❌ 加载失败：' + (e.response?.data?.detail || e.message)))
  }, [])

  const updateCfg = (type, patch) => {
    setConfigs((c) => ({ ...c, [type]: { ...(c[type] || emptyConfig()), ...patch } }))
    setMsg('')
  }

  const saveSystem = async (type) => {
    setBusy((b) => ({ ...b, [type]: 'save' }))
    setMsg('')
    try {
      const body = { company_id: 0, ...(configs[type] || emptyConfig()) }
      const { data } = await api.put(`/integrations/${type}`, body)
      updateCfg(type, data.integration)
      setMsg(`✅ ${type.toUpperCase()} 对接配置已保存`)
    } catch (e) {
      setMsg('❌ 保存失败：' + (e.response?.data?.detail || e.message))
    } finally {
      setBusy((b) => ({ ...b, [type]: undefined }))
    }
  }

  const testSystem = async (type) => {
    setBusy((b) => ({ ...b, [type]: 'test' }))
    setMsg('')
    try {
      const body = { company_id: 0, ...(configs[type] || emptyConfig()) }
      const { data } = await api.post(`/integrations/${type}/test`, body)
      updateCfg(type, data.integration)
      setMsg(data.ok ? `✅ ${type.toUpperCase()} 连接成功（${data.message}）`
                     : `⚠️ ${type.toUpperCase()} 连接失败：${data.message}`)
    } catch (e) {
      setMsg('❌ 测试失败：' + (e.response?.data?.detail || e.message))
    } finally {
      setBusy((b) => ({ ...b, [type]: undefined }))
    }
  }

  const connectedCount = Object.values(configs).filter((c) => c.status === 'ok').length

  const fieldStyle = {
    width: '100%', padding: '9px 12px', border: '1px solid var(--border-strong)',
    borderRadius: 8, fontSize: 14, background: 'var(--bg-elevated)', color: 'var(--text)',
    outline: 'none',
  }
  const labelStyle = { display: 'block', fontSize: 13, fontWeight: 600, margin: '10px 0 6px', color: 'var(--text-muted)' }

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 4 }}>
          <h1 style={{ fontSize: 22, marginBottom: 0 }}>🔌 对接业务系统</h1>
          <span className="badge" style={{ background: 'var(--bg-subtle)', color: 'var(--text)', whiteSpace: 'nowrap', fontSize: 13, padding: '5px 12px', lineHeight: 1.4 }}>
            已连通 <strong style={{ margin: '0 3px', color: 'var(--primary)' }}>{connectedCount}</strong> / {systems.length || 4}
          </span>
        </div>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: 0 }}>
          配置本企业自有的 CRM / ERP / MES / QMS 系统，让 AI 员工直接读取业务数据。
        </p>
      </div>

      {msg && (
        <div className={msg.startsWith('✅') ? 'alert alert-success' : 'alert alert-error'} style={{ marginBottom: 16 }}>
          {msg}
        </div>
      )}

      {!loaded && (
        <div style={{ padding: 30, color: 'var(--text-muted)' }}>加载对接配置中…</div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 20 }}>
        {systems.map((s) => {
          const cfg = configs[s.key] || emptyConfig()
          const st = STATUS_META[cfg.status] || STATUS_META.unconfigured
          const isBusy = busy[s.key]
          return (
            <div className="card" key={s.key} style={{ display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{ fontSize: 26 }}>{s.icon}</span>
                  <div>
                    <div style={{ fontWeight: 700, fontSize: 16 }}>{s.name}</div>
                    <span className={`badge ${st.cls}`} style={{ marginTop: 2 }}>{st.text}</span>
                  </div>
                </div>
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: 'var(--text-muted)' }}>
                  <input type="checkbox" checked={!!cfg.enabled}
                    onChange={(e) => updateCfg(s.key, { enabled: e.target.checked })} /> 启用
                </label>
              </div>
              <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 8 }}>{s.desc}</p>

              <label style={labelStyle}>系统访问地址（API Base URL）</label>
              <input style={fieldStyle} placeholder="https://erp.example.com/api"
                value={cfg.endpoint} onChange={(e) => updateCfg(s.key, { endpoint: e.target.value })} />

              <label style={labelStyle}>鉴权方式</label>
              <select style={fieldStyle} value={cfg.auth_type}
                onChange={(e) => updateCfg(s.key, { auth_type: e.target.value })}>
                {Object.entries(AUTH_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>

              {(cfg.auth_type === 'apikey' || cfg.auth_type === 'bearer') && (
                <>
                  <label style={labelStyle}>{cfg.auth_type === 'bearer' ? 'Token' : 'API Key'}</label>
                  <input style={fieldStyle} type="password" placeholder="粘贴密钥 / Token"
                    value={cfg.api_key} onChange={(e) => updateCfg(s.key, { api_key: e.target.value })} />
                </>
              )}
              {cfg.auth_type === 'basic' && (
                <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                  <div style={{ flex: 1, minWidth: 140 }}>
                    <label style={labelStyle}>用户名</label>
                    <input style={fieldStyle} value={cfg.username}
                      onChange={(e) => updateCfg(s.key, { username: e.target.value })} />
                  </div>
                  <div style={{ flex: 1, minWidth: 140 }}>
                    <label style={labelStyle}>密码</label>
                    <input style={fieldStyle} type="password" value={cfg.password}
                      onChange={(e) => updateCfg(s.key, { password: e.target.value })} />
                  </div>
                </div>
              )}

              {cfg.last_error && cfg.status === 'error' && (
                <div style={{ fontSize: 12, color: '#dc2626', marginTop: 8 }}>
                  最近错误：{cfg.last_error}
                </div>
              )}

              <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
                <button className="btn btn-primary btn-sm" onClick={() => saveSystem(s.key)} disabled={isBusy}>
                  {isBusy === 'save' ? '保存中…' : '💾 保存'}
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => testSystem(s.key)} disabled={isBusy}>
                  {isBusy === 'test' ? '测试中…' : '🔍 测试连接'}
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
