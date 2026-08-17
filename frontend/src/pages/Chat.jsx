import React, { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import api from '../api'

export default function Chat() {
  const [searchParams] = useSearchParams()
  const [agents, setAgents] = useState([])
  const [agentId, setAgentId] = useState(searchParams.get('agent_id') || '')
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [mockMode, setMockMode] = useState(false)
  const [sessionId, setSessionId] = useState('')          // 会话 ID（刷新后恢复上下文）
  const [historyLoaded, setHistoryLoaded] = useState(false)
  const bottomRef = useRef(null)
  const taRef = useRef(null)
  const companyId = Number(localStorage.getItem('company_id'))

  // 工艺/图纸助手（process）角色给出图纸类提问示例，其余用通用占位
  const selectedAgent = agents.find((a) => String(a.id) === String(agentId))
  const isProcess = selectedAgent && selectedAgent.role === 'process'
  const placeholder = isProcess
    ? '例如：XK-205 的外径公差是多少？材质是什么？加工时注意什么？'
    : '输入消息，Enter 发送 / Shift+Enter 换行'

  // textarea 自适应高度：1~5 行（38~110px），超出滚动
  const _autoResize = () => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    const h = Math.min(Math.max(ta.scrollHeight, 38), 110)
    ta.style.height = h + 'px'
  }

  useEffect(() => {
    api.get('/agent/list', { params: { company_id: companyId } })
      .then(({ data }) => setAgents(data.agents))
  }, [])

  // 恢复会话历史：拉取企业最近对话，按当前对话对象过滤并重建消息（修复：此前刷新即丢上下文/串话）
  useEffect(() => {
    if (!companyId || historyLoaded) return
    api.get('/agent/conversations', { params: { company_id: companyId, page_size: 100 } })
      .then(({ data }) => {
        const all = data.conversations || []
        // 排除图纸对话（drawing_chat），按当前 agent_id 过滤（未选则取全部 web 会话）
        const web = all.filter((c) => c.channel !== 'drawing_chat' &&
          (!agentId || String(c.agent_id) === String(agentId)))
        if (web.length) {
          const sid = sessionId || web[0].session_id
          setSessionId(sid)
          const msgs = []
          web.filter((c) => c.session_id === sid).reverse().forEach((c) => {
            if (c.user_message) msgs.push({ role: 'user', content: c.user_message })
            if (c.agent_answer) msgs.push({ role: 'assistant', content: c.agent_answer })
          })
          if (msgs.length) setMessages(msgs)
        }
        setHistoryLoaded(true)
      })
      .catch(() => setHistoryLoaded(true))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [companyId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const newChat = () => {
    setSessionId('')
    setMessages([])
  }

  const send = async () => {
    if (!input.trim() || loading) return
    const userMsg = input
    setInput('')
    setTimeout(_autoResize, 0)   // 发送后重置高度
    setMessages((m) => [...m, { role: 'user', content: userMsg }])
    setLoading(true)
    try {
      const { data } = await api.post('/agent/chat', {
        message: userMsg,
        company_id: companyId,
        agent_id: agentId ? Number(agentId) : null,
        session_id: sessionId || undefined,   // 携带会话 ID 保持多轮上下文
      })
      if (data.session_id) setSessionId(data.session_id)
      setMockMode(data.mock_mode)
      setMessages((m) => [...m, {
        role: 'assistant',
        content: typeof data.answer === 'string' ? data.answer : JSON.stringify(data.answer),
        agent: data.agent,
      }])
    } catch (e) {
      // FastAPI 校验错误 detail 是数组，普通错误 detail 是字符串
      const detail = e.response?.data?.detail
      const errText = Array.isArray(detail)
        ? detail.map(d => d.msg || d).join('; ')
        : (detail || e.message || '请求失败')
      setMessages((m) => [...m, { role: 'assistant', content: `⚠️ ${errText}` }])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
        <h1 style={{ fontSize: 22 }}>AI 员工对话</h1>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button className="btn btn-sm" onClick={newChat} style={{ fontSize: 13 }}>🆕 新对话</button>
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>对话对象：</span>
          <select
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid #cbd5e1', fontSize: 13 }}
          >
            <option value="">🤖 智能路由（自动判断）</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.role_label} · {a.name}</option>
            ))}
          </select>
        </div>
      </div>

      {mockMode && (
        <div className="alert alert-info" style={{ marginBottom: 12 }}>
          💡 当前为演示模式（未配置 LLM_API_KEY），使用内置模拟回复。配置 DeepSeek/OpenAI Key 后即为真实 AI 员工。
        </div>
      )}

      <div className="card" style={{ display: 'flex', flexDirection: 'column', height: '65vh' }}>
        <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
          {messages.length === 0 && (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 40 }}>
              选择 AI 员工开始对话<br />
              <span style={{ fontSize: 12 }}>
                {isProcess
                  ? '例如：XK-205 的外径公差是多少？材质标准是什么？加工注意什么？'
                  : '例如：「我们需要采购1000个500ml玻璃烧杯」'}
              </span>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} style={{
              display: 'flex',
              justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start',
              marginBottom: 12,
            }}>
              <div style={{
                maxWidth: '75%',
                padding: '10px 14px',
                borderRadius: 12,
                background: m.role === 'user' ? 'var(--primary)' : 'var(--bg-subtle)',
                color: m.role === 'user' ? '#fff' : 'var(--text)',
                fontSize: 14,
                lineHeight: 1.7,
                whiteSpace: 'pre-wrap',
                border: m.role === 'user' ? 'none' : '1px solid var(--border)',
              }}>
                {m.role === 'assistant' && m.agent && (
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>{m.agent}</div>
                )}
                {m.content}
              </div>
            </div>
          ))}
          {loading && (
            <div style={{ display: 'flex', justifyContent: 'flex-start' }}>
              <div className="card" style={{ padding: '10px 14px', fontSize: 14 }}>AI 思考中...</div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <div style={{ borderTop: '1px solid #e2e8f0', padding: 14, display: 'flex', gap: 10, alignItems: 'flex-end' }}>
          <textarea
            ref={taRef}
            value={input}
            onChange={(e) => {
              setInput(e.target.value)
              _autoResize()
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
            placeholder={placeholder}
            rows={1}
            style={{
              flex: 1,
              padding: '10px 8px',
              resize: 'none',
              lineHeight: 1.5,
              minHeight: 38,
              maxHeight: 110,                    // ≈ 5 行
              background: 'var(--bg-subtle)',             // 灰色背景（与系统其他输入框一致）
              border: '1px solid #cbd5e1',
              borderRadius: 6,
              fontSize: 14,
              fontFamily: 'inherit',
              outline: 'none',
              overflowY: 'auto',
              boxSizing: 'border-box',
            }}
          />
          <button className="btn btn-primary" onClick={send} disabled={loading || !input.trim()}>发送</button>
        </div>
      </div>
    </div>
  )
}
