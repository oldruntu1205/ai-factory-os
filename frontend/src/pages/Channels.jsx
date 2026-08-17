import React, { useEffect, useState } from 'react'
import api from '../api'

const FIELDS = {
  wecom_robot: [
    { key: 'wecom_robot_key', label: '群机器人 Webhook Key', secret: true, ph: '在企微群 → 添加群机器人 → 复制 Key' },
  ],
  wecom_bot: [
    { key: 'wecom_bot_id', label: '智能机器人 Bot ID', secret: false, ph: '企微 → 智能机器人 → 长连接模式 → BotID' },
    { key: 'wecom_bot_secret', label: '长连接 Secret', secret: true, ph: '智能机器人 → 长连接模式 → Secret（与自建应用 Secret 不同）' },
  ],
  whatsapp: [
    { key: 'whatsapp_token', label: 'Meta API Token', secret: true, ph: 'Meta for Developers → WhatsApp' },
    { key: 'whatsapp_phone_id', label: '业务号 Phone ID', secret: false, ph: 'WhatsApp 业务号码 ID' },
    { key: 'whatsapp_verify_token', label: 'Webhook 验证令牌', secret: false, ph: '自定义，回调验证用' },
    { key: 'whatsapp_app_secret', label: 'App Secret（可选）', secret: true, ph: 'Meta App Secret，用于签名校验' },
  ],
  email: [
    { key: 'smtp_host', label: 'SMTP 服务器', secret: false, ph: 'smtp.example.com' },
    { key: 'smtp_port', label: 'SMTP 端口', secret: false, ph: '465' },
    { key: 'smtp_user', label: 'SMTP 账号', secret: false, ph: 'your@email.com' },
    { key: 'smtp_password', label: 'SMTP 密码/授权码', secret: true, ph: '邮箱授权码' },
    { key: 'smtp_from', label: '发件人', secret: false, ph: 'sales@yourcompany.com' },
  ],
}

const CHANNEL_META = {
  wecom_robot: { icon: '💬', title: '企业微信 · 群机器人', desc: '把 AI 日报、报价单推送到你的企微群' },
  wecom_bot:   { icon: '🤖', title: '企业微信 · 智能机器人', desc: '长连接 AI 同事：员工在企微找机器人对话，AI 员工实时回复（需启动长连接守护进程）' },
  whatsapp:    { icon: '📱', title: 'WhatsApp 业务号', desc: '客户 WhatsApp 来消息，AI 员工自动回复' },
  email:       { icon: '✉️', title: '邮件 SMTP', desc: 'AI 开发信、跟进序列、报价 PDF 发送' },
}

const SECTION_TITLE = {
  wecom: '企业微信',
  whatsapp: 'Whatsapp',
  email: '企业邮件',
}

const SECTION_DESC = {
  wecom: '对接企业微信，把 AI 员工接入群机器人与智能机器人对话',
  whatsapp: '对接 WhatsApp 业务号，触达海外客户',
  email: '配置企业邮件，自动发送开发信与跟进序列',
}

export default function Channels({ section = 'wecom' }) {
  const [configs, setConfigs] = useState({})
  const [meta, setMeta] = useState({})
  const [saving, setSaving] = useState('')
  const [msg, setMsg] = useState({})
  const [wecomMsg, setWecomMsg] = useState('### 📊 今日经营快报\n- 销售额 ¥120 万\n- 新客户 8 家')
  const [wecomResult, setWecomResult] = useState('')
  const [seqInput, setSeqInput] = useState({ company: '太阳玻璃仪器', product: '硼硅酸盐玻璃烧杯' })
  const [sequence, setSequence] = useState(null)
  const [seqLang, setSeqLang] = useState('zh')
  const [seqLoading, setSeqLoading] = useState(false)
  const [logs, setLogs] = useState([])

  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    try {
      const { data } = await api.get('/channels/config', { params: { company_id: companyId } })
      setConfigs((prev) => {
        const c = {}
        for (const ch of Object.keys(FIELDS)) {
          c[ch] = {}
          for (const f of FIELDS[ch]) {
            const serverVal = data[ch]?.[f.key]
            const localVal = prev[ch]?.[f.key]
            // 服务器返回脱敏值（如 ********L8zW）时，如果本地已有用户输入的真实值，保留真实值，
            // 避免保存后测试按钮误把脱敏值发出去导致 853000
            if (f.secret && typeof serverVal === 'string' && serverVal.startsWith('*') && localVal && !localVal.startsWith('*')) {
              c[ch][f.key] = localVal
            } else {
              c[ch][f.key] = serverVal || ''
            }
          }
        }
        return c
      })
      setMeta(data._meta || {})
      const l = await api.get(`/channels/email/logs?company_id=${companyId}`)
      setLogs(l.data.logs || [])
    } catch (e) { /* 忽略 */ }
  }

  useEffect(() => { load() }, [])

  const setField = (ch, key, val) =>
    setConfigs((s) => ({ ...s, [ch]: { ...s[ch], [key]: val } }))

  const save = async (ch) => {
    setSaving(ch); setMsg((m) => ({ ...m, [ch]: '' }))
    try {
      await api.put('/channels/config', { company_id: companyId, channel: ch, config: configs[ch] })
      setMsg((m) => ({ ...m, [ch]: '✅ 已保存' }))
      load()
    } catch (e) {
      setMsg((m) => ({ ...m, [ch]: `❌ ${e.response?.data?.detail || '保存失败'}` }))
    } finally { setSaving('') }
  }

  const testSend = async (ch) => {
    setMsg((m) => ({ ...m, [ch]: '测试中...' }))
    try {
      if (ch === 'wecom_robot') {
        const { data } = await api.post('/channels/wecom/robot', { content: '🔔 渠道测试消息：您的企业微信已对接成功！', company_id: companyId })
        setMsg((m) => ({ ...m, [ch]: data.mock ? `✅ 配置已保存（未配置 Key 时为演示模式）` : `✅ 已发送到企微群` }))
      } else if (ch === 'whatsapp') {
        const { data } = await api.post('/channels/whatsapp/send', { to: '10086', message: 'test', company_id: companyId })
        setMsg((m) => ({ ...m, [ch]: data.mock ? `✅ 配置已保存（未配置 token 时为演示模式）` : `✅ 发送成功` }))
      } else if (ch === 'email') {
        const { data } = await api.post('/channels/email/send', { receiver: 'test@company.com', company: '测试', product: '测试' })
        setMsg((m) => ({ ...m, [ch]: `✅ 邮件接口正常：${data.status}` }))
      } else if (ch === 'wecom_bot') {
        const cfg = configs[ch] || {}
        if (!cfg.wecom_bot_id || !cfg.wecom_bot_secret) {
          setMsg((m) => ({ ...m, [ch]: '请先填写 Bot ID 和 Secret 再测试' }))
          return
        }
        if (cfg.wecom_bot_secret.startsWith('*')) {
          setMsg((m) => ({ ...m, [ch]: 'Secret 当前为脱敏显示，请重新输入真实 Secret 后再测试' }))
          return
        }
        const { data } = await api.post('/channels/wecom/bot/test', { bot_id: cfg.wecom_bot_id.trim(), secret: cfg.wecom_bot_secret.trim() })
        setMsg((m) => ({ ...m, [ch]: data.ok
          ? '✅ 连接与鉴权成功（启动长连接守护进程后，在企微找机器人对话即可）'
          : `❌ ${data.error}` }))
      }
    } catch (e) {
      setMsg((m) => ({ ...m, [ch]: `❌ ${e.response?.data?.detail || '测试失败'}` }))
    }
  }

  const pushWecom = async () => {
    setWecomResult('')
    try {
      const { data } = await api.post('/channels/wecom/robot', { content: wecomMsg, company_id: companyId })
      setWecomResult(data.mock ? '✅ 已模拟推送（未配置群机器人 Key）' : '✅ 已推送到企微群')
    } catch (e) { setWecomResult(`❌ ${e.response?.data?.detail || '推送失败'}`) }
  }

  const genSequence = async (language) => {
    setSeqLoading(true)
    setSeqLang(language); setSequence(null)
    try {
      const { data } = await api.post('/channels/email/sequence', {
        ...seqInput, language,
      })
      setSequence(data.sequence)
    } catch (e) { alert('生成失败') }
    finally { setSeqLoading(false) }
  }

  const clearSequence = () => {
    setSequence(null)
  }

  const badge = (ok) => ({
    display: 'inline-block', padding: '2px 10px', borderRadius: 999, fontSize: 12,
    background: ok ? '#dcfce7' : 'var(--border)', color: ok ? '#15803d' : 'var(--text-muted)',
  })

  // 单个渠道配置卡片（智能机器人 / 群机器人 / WhatsApp / 邮件 SMTP）
  const renderConfigCard = (ch) => {
    const metaInfo = CHANNEL_META[ch]
    const fields = FIELDS[ch]
    const configured = meta[ch]?.configured
    return (
      <div className="card" key={ch}>
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 4 }}>
          <h3 style={{ fontSize: 15 }}>{metaInfo.icon} {metaInfo.title}</h3>
          <span style={{ marginLeft: 'auto' }}>
            <span style={badge(configured)}>{configured ? '✓ 已配置' : '未配置'}</span>
          </span>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 12 }}>{metaInfo.desc}</div>
        {fields.map((f) => (
          <div className="form-group" key={f.key} style={{ marginBottom: 10 }}>
            <label>{f.label}</label>
            <input
              type={f.secret ? 'password' : 'text'}
              value={configs[ch]?.[f.key] || ''}
              onChange={(e) => setField(ch, f.key, e.target.value)}
              placeholder={f.ph}
              autoComplete="off"
            />
            {f.secret && configs[ch]?.[f.key] && (
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                已保存：{configs[ch][f.key].startsWith('*')
                  ? '（脱敏显示，留空保存则不变）' : '输入新值将覆盖'}
              </div>
            )}
          </div>
        ))}
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
          <button className="btn btn-primary btn-sm" onClick={() => save(ch)} disabled={saving === ch}>
            {saving === ch ? '保存中...' : '💾 保存'}
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => testSend(ch)}>
            测试
          </button>
          {msg[ch] && <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{msg[ch]}</span>}
        </div>
      </div>
    )
  }

  // 渠道配置卡片区（网格）
  const renderConfigGrid = (chs) => (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 24 }}>
      {chs.map(renderConfigCard)}
    </div>
  )

  // ====== 企业微信（二级菜单）======
  const renderWecom = () => (
    <>
      {renderConfigGrid(['wecom_bot', 'wecom_robot'])}
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>💬 推送 AI 日报到企微群</h3>
        <textarea
          value={wecomMsg}
          onChange={(e) => setWecomMsg(e.target.value)}
          rows={5}
          style={{ width: '100%', marginBottom: 12 }}
        />
        <button className="btn btn-primary" onClick={pushWecom}>🚀 推送到企微群</button>
        {wecomResult && <div style={{ marginTop: 10, fontSize: 13 }}>{wecomResult}</div>}
      </div>
    </>
  )

  // ====== Whatsapp（二级菜单）======
  const renderWhatsapp = () => renderConfigGrid(['whatsapp'])

  // 邮件发送日志卡片
  const renderEmailLogCard = () => (
    <div className="card">
      <h3 style={{ marginBottom: 12 }}>📋 邮件发送日志</h3>
      <div style={{ maxHeight: 420, overflowY: 'auto' }}>
        <table className="table">
          <thead>
            <tr><th>收件人</th><th>主题</th><th>状态</th><th>时间</th></tr>
          </thead>
          <tbody>
            {logs.map((l) => (
              <tr key={l.id}>
                <td>{l.recipient}</td>
                <td style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{l.subject}</td>
                <td>
                  <span className={`badge ${
                    l.status === 'sent' ? 'badge-green' :
                    l.status === 'bounced' ? 'badge-red' :
                    l.status === 'error' ? 'badge-red' : 'badge-gray'}`}>
                    {l.status === 'sent' ? '已发送' : l.status === 'bounced' ? '退信' :
                     l.status === 'error' ? '失败' : l.status === 'simulated' ? '模拟' : l.status}
                  </span>
                </td>
                <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{(l.created_at || '').slice(0, 19).replace('T', ' ')}</td>
              </tr>
            ))}
            {logs.length === 0 && (
              <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无邮件记录</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )

  // ====== 企业邮件（二级菜单）======
  // 顶部：邮件 SMTP 与 邮件发送日志 并列；下方：AI 邮件跟进序列 整宽
  const renderEmail = () => (
    <>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 24, alignItems: 'start' }}>
        {renderConfigCard('email')}
        {renderEmailLogCard()}
      </div>
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>✉️ AI 邮件跟进序列（D0/D3/D7）</h3>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          <div className="form-group" style={{ marginBottom: 0 }}>
            <label>公司 / Buyer</label>
            <input value={seqInput.company} onChange={(e) => setSeqInput({ ...seqInput, company: e.target.value })} />
          </div>
          <div className="form-group" style={{ marginBottom: 0 }}>
            <label>产品 / Product</label>
            <input value={seqInput.product} onChange={(e) => setSeqInput({ ...seqInput, product: e.target.value })} />
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <button
            className="btn btn-primary btn-sm"
            onClick={() => genSequence('zh')}
            disabled={seqLoading}
          >
            {seqLoading && seqLang === 'zh' ? '生成中...' : '🇨🇳 生成中文跟进邮件'}
          </button>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => genSequence('en')}
            disabled={seqLoading}
            style={{ background: seqLang === 'en' ? 'var(--primary-soft)' : 'var(--bg-subtle)' }}
          >
            {seqLoading && seqLang === 'en' ? '生成中...' : '🇺🇸 生成英文跟进邮件'}
          </button>
          <button
            className="btn btn-secondary btn-sm"
            onClick={clearSequence}
            disabled={!sequence}
          >
            🗑️ 清空邮件内容
          </button>
          {sequence && (
            <span style={{ fontSize: 12, color: 'var(--text-muted)', marginLeft: 'auto' }}>
              {seqLang === 'en' ? '📧 英文外贸格式（Subject / Dear... / Best regards）' : '📧 中文商务邮件'}
            </span>
          )}
        </div>
        {sequence && (
          <div style={{ marginTop: 12, maxHeight: 240, overflowY: 'auto', fontSize: 12 }}>
            {Object.entries(sequence).map(([k, v]) => (
              <SequenceCard key={k} k={k} v={v} lang={seqLang} />
            ))}
          </div>
        )}
      </div>
    </>
  )

  const SECTION_RENDER = {
    wecom: renderWecom,
    whatsapp: renderWhatsapp,
    email: renderEmail,
  }

  return (
    <div>
      <h1 style={{ fontSize: 22, marginBottom: 6 }}>📡 {SECTION_TITLE[section] || '渠道中心'}</h1>
      <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 24 }}>
        {SECTION_DESC[section] || '在这里自助对接外部渠道，保存后立即生效（每个企业独立配置）'}
      </p>
      {(SECTION_RENDER[section] || renderWecom)()}
    </div>
  )
}

// 邮件预览卡片：中文简洁展示，英文按外贸格式分区
function SequenceCard({ k, v, lang }) {
  const stepLabel = k === 'email1' ? '首封 / Email 1 (Day 0)'
                  : k === 'email2' ? 'D+3 跟进 / Follow-up (Day 3)'
                  : 'D+7 提醒 / Final Reminder (Day 7)'

  if (lang === 'en') {
    // 英文：按 Subject / Salutation / Body / CTA / Signature 分块展示
    const lines = v.split('\n')
    let subject = '', salutation = '', body = '', signature = ''
    let mode = 'subject'
    for (const line of lines) {
      if (mode === 'subject' && line.startsWith('Subject:')) {
        subject = line.replace(/^Subject:\s*/, '')
        mode = 'header'
        continue
      }
      if (mode === 'header' && (line.trim() === '' || line.startsWith('Dear'))) {
        if (line.trim() === '') continue
        salutation = line
        mode = 'body'
        continue
      }
      if (mode === 'body') {
        if (line.startsWith('Best regards,')) { mode = 'signature'; continue }
        body += (body ? '\n' : '') + line
      }
      if (mode === 'signature') {
        signature += (signature ? '\n' : '') + line
      }
    }
    return (
      <div style={{ marginBottom: 10, padding: 12, background: 'var(--bg-subtle)', borderRadius: 6, border: '1px solid var(--border)' }}>
        <b style={{ color: 'var(--primary)', fontSize: 12 }}>{stepLabel}</b>
        {subject && (
          <div style={{ marginTop: 8, padding: '6px 10px', background: 'var(--bg-elevated)', borderRadius: 4, borderLeft: '3px solid var(--primary)' }}>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 2 }}>📧 SUBJECT</div>
            <div style={{ fontWeight: 600, color: 'var(--text)' }}>{subject}</div>
          </div>
        )}
        {salutation && <div style={{ marginTop: 8, color: 'var(--text)', fontWeight: 600 }}>{salutation}</div>}
        {body && (
          <pre style={{ whiteSpace: 'pre-wrap', marginTop: 6, fontSize: 12, color: 'var(--text)', lineHeight: 1.7 }}>{body}</pre>
        )}
        {signature && (
          <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px dashed var(--border)', color: 'var(--text-muted)' }}>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, fontFamily: 'inherit', lineHeight: 1.5 }}>{signature}</pre>
          </div>
        )}
      </div>
    )
  }

  // 中文：简洁展示
  return (
    <div style={{ marginBottom: 10, padding: 10, background: 'var(--bg-subtle)', borderRadius: 6, border: '1px solid var(--border)' }}>
      <b style={{ color: 'var(--primary)', fontSize: 12 }}>{stepLabel}</b>
      <pre style={{ whiteSpace: 'pre-wrap', marginTop: 6, fontSize: 12, color: 'var(--text)', lineHeight: 1.7 }}>{v}</pre>
    </div>
  )
}
