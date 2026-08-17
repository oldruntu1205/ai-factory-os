import React, { useEffect, useState } from 'react'
import api from '../api'

const roleEmoji = {
  customer_agent: '💬',
  sales_agent: '📈',
  quotation_agent: '📄',
  production_agent: '⚙️',
  quality_agent: '🔬',
  process_agent: '📐',
  ceo_agent: '🧠',
}

export default function Hermes() {
  const [agents, setAgents] = useState([])
  const [tools, setTools] = useState([])
  const [selected, setSelected] = useState('sales_agent')
  const [message, setMessage] = useState('')
  const [result, setResult] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [memory, setMemory] = useState([])
  const [loading, setLoading] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [newAgent, setNewAgent] = useState({
    name: '', version: '1.0', industry: 'glassware',
    label: '', description: '', tools: 'crm.get_customer, knowledge.search',
  })
  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const [a, t] = await Promise.all([
      api.get('/hermes/agent/list'),
      api.get('/hermes/tool/list'),
    ])
    setAgents(a.data.agents)
    setTools(t.data.tools)
  }

  useEffect(() => { load() }, [])

  const execute = async () => {
    setLoading(true)
    setResult('')
    try {
      const { data } = await api.post('/hermes/execute', {
        agent_name: selected,
        message,
        company_id: companyId,
      })
      setResult(data.result)
      setSessionId(data.session_id)
      const mem = await api.get('/hermes/memory/short', {
        params: { session_id: data.session_id },
      })
      setMemory(mem.data.entries)
    } catch (e) {
      setResult(`⚠️ ${e.response?.data?.detail || '执行失败'}`)
    } finally {
      setLoading(false)
    }
  }

  const registerAgent = async () => {
    try {
      await api.post('/hermes/agent/register', {
        ...newAgent,
        tools: newAgent.tools.split(',').map((t) => t.trim()).filter(Boolean),
      })
      setShowRegister(false)
      setNewAgent({
        name: '', version: '1.0', industry: 'glassware',
        label: '', description: '', tools: 'crm.get_customer, knowledge.search',
      })
      load()
    } catch (e) {
      alert(e.response?.data?.detail || '注册失败')
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <h1 style={{ fontSize: 22 }}>Hermes Agent Runtime</h1>
        <button className="btn btn-primary" onClick={() => setShowRegister(!showRegister)}>
          {showRegister ? '取消' : '+ 注册 Agent'}
        </button>
      </div>
      <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 24 }}>
        AI 员工操作系统内核：Agent 注册中心 / Tool 调用 / 三层记忆 / RBAC 权限 / 消息队列
      </p>

      {showRegister && (
        <div className="card" style={{ marginBottom: 24 }}>
          <h3 style={{ marginBottom: 12 }}>注册新 AI 员工 Agent（动态加载）</h3>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
            <div className="form-group"><label>Agent 名称</label>
              <input value={newAgent.name} onChange={(e) => setNewAgent({ ...newAgent, name: e.target.value })}
                placeholder="如 procurement_agent" /></div>
            <div className="form-group"><label>展示名</label>
              <input value={newAgent.label} onChange={(e) => setNewAgent({ ...newAgent, label: e.target.value })}
                placeholder="如 AI采购" /></div>
            <div className="form-group"><label>行业</label>
              <input value={newAgent.industry} onChange={(e) => setNewAgent({ ...newAgent, industry: e.target.value })} /></div>
            <div className="form-group"><label>Tools（逗号分隔）</label>
              <input value={newAgent.tools} onChange={(e) => setNewAgent({ ...newAgent, tools: e.target.value })} /></div>
          </div>
          <div className="form-group">
            <label>描述</label>
            <input value={newAgent.description} onChange={(e) => setNewAgent({ ...newAgent, description: e.target.value })} />
          </div>
          <button className="btn btn-success" onClick={registerAgent}>注册到 Hermes</button>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '3fr 2fr', gap: 24 }}>
        {/* 左：Agent 执行控制台 */}
        <div>
          <div className="card" style={{ marginBottom: 16 }}>
            <h3 style={{ marginBottom: 12 }}>Agent 执行控制台</h3>
            <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
              {agents.map((a) => (
                <button key={a.name}
                  onClick={() => setSelected(a.name)}
                  style={{
                    padding: '8px 14px', borderRadius: 8, border: '1px solid #cbd5e1',
                    background: selected === a.name ? 'var(--primary)' : '#fff',
                    color: selected === a.name ? '#fff' : 'var(--text)',
                    fontSize: 13, cursor: 'pointer',
                  }}>
                  {roleEmoji[a.name] || '🤖'} {a.label || a.name}
                </button>
              ))}
              {/* 占位：更多 Agent 升级中 */}
              <button disabled
                style={{
                  padding: '8px 14px', borderRadius: 8, border: '1px dashed #cbd5e1',
                  background: 'var(--bg-subtle)', color: 'var(--text-muted)', fontSize: 13,
                  cursor: 'not-allowed',
                }}>
                🔒 更多 Agent 升级中...
              </button>
            </div>
            <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'stretch' }}>
              <input value={message} onChange={(e) => setMessage(e.target.value)}
                placeholder="示例：我们需要采购 1000 个 500ml 玻璃烧杯"
                style={{ flex: 1 }} />
              <button className="btn btn-primary" onClick={execute} disabled={loading}
                style={{ marginLeft: 'auto' }}>
                {loading ? 'Hermes 执行中...' : '🚀 执行'}
              </button>
            </div>
            {result && (
              <pre style={{
                background: 'var(--bg-subtle)', padding: 16, borderRadius: 8,
                fontSize: 13, whiteSpace: 'pre-wrap', lineHeight: 1.8,
              }}>
                {result}
              </pre>
            )}
          </div>

          {/* Tool 清单 */}
          <div className="card">
            <h3 style={{ marginBottom: 12 }}>Tool 调用系统（{tools.length}）</h3>
            <table className="table">
              <thead>
                <tr><th>Tool</th><th>权限点</th><th>说明</th></tr>
              </thead>
              <tbody>
                {tools.map((t) => (
                  <tr key={t.name}>
                    <td><code>{t.name}</code></td>
                    <td>
                      {t.permissions.map((p) => <span key={p} className="badge badge-blue" style={{ marginRight: 4 }}>{p}</span>)}
                    </td>
                    <td style={{ fontSize: 12 }}>{t.description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* 右：Agent 注册中心 + 记忆 */}
        <div>
          <div className="card" style={{ marginBottom: 16 }}>
            <h3 style={{ marginBottom: 12 }}>Agent 注册中心（{agents.length}）</h3>
            {agents.map((a) => (
              <div key={a.name} style={{
                padding: 12, border: '1px solid #e2e8f0', borderRadius: 8, marginBottom: 8,
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontWeight: 600 }}>{roleEmoji[a.name] || '🤖'} {a.label}</span>
                  <span className="badge badge-gray">v{a.version}</span>
                  <span className="badge badge-green" style={{ marginLeft: 'auto' }}>{a.status}</span>
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>{a.name} · {a.industry}</div>
                <div style={{ fontSize: 12, marginTop: 6 }}>
                  {a.tools.map((t) => <span key={t} className="badge badge-blue" style={{ marginRight: 4 }}>{t}</span>)}
                </div>
              </div>
            ))}
          </div>

          <div className="card">
            <h3 style={{ marginBottom: 12 }}>短期记忆（当前对话）</h3>
            {memory.length === 0 && (
              <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 13, padding: 12 }}>
                执行 Agent 后展示对话记忆
              </div>
            )}
            {memory.map((m, i) => (
              <div key={i} style={{
                background: 'var(--bg-subtle)', borderRadius: 8, padding: 10, marginBottom: 8, fontSize: 13,
              }}>
                <span className="badge badge-gray" style={{ marginRight: 6 }}>
                  {m.user ? '客户' : 'AI'}
                </span>
                {String(m.user || m.agent).slice(0, 80)}...
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
