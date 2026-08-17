import React, { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
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

export default function IntegrationDetail() {
  const { system } = useParams()
  const [systems, setSystems] = useState([])
  const [config, setConfig] = useState(emptyConfig())
  const [loaded, setLoaded] = useState(false)
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setLoaded(false)
    setMsg('')
    Promise.all([
      api.get('/integrations/systems'),
      api.get('/integrations'),
    ]).then(([s, r]) => {
      const sysList = s.data.systems || []
      setSystems(sysList)
      const found = sysList.find((it) => it.key === system)
      if (!found) {
        setMsg('❌ 未知的业务系统类型')
        setLoaded(true)
        return
      }
      const map = {}
      ;(r.data.integrations || []).forEach((it) => { map[it.system_type] = it })
      setConfig({ ...emptyConfig(), ...(map[system] || {}) })
      setLoaded(true)
    }).catch((e) => setMsg('❌ 加载失败：' + (e.response?.data?.detail || e.message)))
  }, [system])

  const systemMeta = systems.find((s) => s.key === system)
  const st = STATUS_META[config.status] || STATUS_META.unconfigured

  const saveSystem = async () => {
    setBusy('save')
    setMsg('')
    try {
      const body = { company_id: 0, ...config }
      const { data } = await api.put(`/integrations/${system}`, body)
      setConfig((c) => ({ ...c, ...data.integration }))
      setMsg(`✅ ${system.toUpperCase()} 对接配置已保存`)
    } catch (e) {
      setMsg('❌ 保存失败：' + (e.response?.data?.detail || e.message))
    } finally {
      setBusy(false)
    }
  }

  const testSystem = async () => {
    setBusy('test')
    setMsg('')
    try {
      const body = { company_id: 0, ...config }
      const { data } = await api.post(`/integrations/${system}/test`, body)
      setConfig((c) => ({ ...c, ...data.integration }))
      setMsg(data.ok ? `✅ ${system.toUpperCase()} 连接成功（${data.message}）`
                     : `⚠️ ${system.toUpperCase()} 连接失败：${data.message}`)
    } catch (e) {
      setMsg('❌ 测试失败：' + (e.response?.data?.detail || e.message))
    } finally {
      setBusy(false)
    }
  }

  const fieldStyle = {
    width: '100%', padding: '9px 12px', border: '1px solid var(--border-strong)',
    borderRadius: 8, fontSize: 14, background: 'var(--bg-elevated)', color: 'var(--text)',
    outline: 'none',
  }
  const labelStyle = { display: 'block', fontSize: 13, fontWeight: 600, margin: '10px 0 6px', color: 'var(--text-muted)' }

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <h1 style={{ fontSize: 22, marginBottom: 0 }}>🔌 {systemMeta?.name || system.toUpperCase()}</h1>
          <span className={`badge ${st.cls}`}>{st.text}</span>
        </div>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: '6px 0 0' }}>
          {systemMeta?.desc || '配置本系统，让 AI 员工直接读取业务数据。'}
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

      {loaded && systemMeta && (
        <div className="card" style={{ maxWidth: 760 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ fontSize: 32 }}>{systemMeta.icon}</span>
              <div>
                <div style={{ fontWeight: 700, fontSize: 16 }}>{systemMeta.name}</div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>类型：{system.toUpperCase()}</div>
              </div>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: 'var(--text-muted)' }}>
              <input type="checkbox" checked={!!config.enabled}
                onChange={(e) => setConfig((c) => ({ ...c, enabled: e.target.checked }))} /> 启用
            </label>
          </div>

          <label style={labelStyle}>系统访问地址（API Base URL）</label>
          <input style={fieldStyle} placeholder="https://erp.example.com/api"
            value={config.endpoint} onChange={(e) => setConfig((c) => ({ ...c, endpoint: e.target.value }))} />

          <label style={labelStyle}>鉴权方式</label>
          <select style={fieldStyle} value={config.auth_type}
            onChange={(e) => setConfig((c) => ({ ...c, auth_type: e.target.value }))}>
            {Object.entries(AUTH_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>

          {(config.auth_type === 'apikey' || config.auth_type === 'bearer') && (
            <>
              <label style={labelStyle}>{config.auth_type === 'bearer' ? 'Token' : 'API Key'}</label>
              <input style={fieldStyle} type="password" placeholder="粘贴密钥 / Token"
                value={config.api_key} onChange={(e) => setConfig((c) => ({ ...c, api_key: e.target.value }))} />
            </>
          )}
          {config.auth_type === 'basic' && (
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <div style={{ flex: 1, minWidth: 140 }}>
                <label style={labelStyle}>用户名</label>
                <input style={fieldStyle} value={config.username}
                  onChange={(e) => setConfig((c) => ({ ...c, username: e.target.value }))} />
              </div>
              <div style={{ flex: 1, minWidth: 140 }}>
                <label style={labelStyle}>密码</label>
                <input style={fieldStyle} type="password" value={config.password}
                  onChange={(e) => setConfig((c) => ({ ...c, password: e.target.value }))} />
              </div>
            </div>
          )}

          {config.last_error && config.status === 'error' && (
            <div style={{ fontSize: 12, color: '#dc2626', marginTop: 8 }}>
              最近错误：{config.last_error}
            </div>
          )}

          <div style={{ display: 'flex', gap: 10, marginTop: 18 }}>
            <button className="btn btn-primary" onClick={saveSystem} disabled={busy}>
              {busy === 'save' ? '保存中…' : '💾 保存'}
            </button>
            <button className="btn btn-secondary" onClick={testSystem} disabled={busy}>
              {busy === 'test' ? '测试中…' : '🔍 测试连接'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
