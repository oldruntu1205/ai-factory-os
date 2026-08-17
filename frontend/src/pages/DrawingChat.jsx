import React, { useEffect, useRef, useState } from 'react'
import api from '../api'

const ACCEPT = ".pdf,.docx,.doc,.xlsx,.xls,.txt,.md,.csv,.dxf,.dwg,.png,.jpg,.jpeg,.gif,.bmp,.webp"

function fmtSize(bytes) {
  if (!bytes) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return (bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + u[i]
}

export default function DrawingChat() {
  const [files, setFiles] = useState([])   // 本次会话附件 [{id, file, name, size}]
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [mockMode, setMockMode] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const bottomRef = useRef(null)
  const taRef = useRef(null)
  const fileInputRef = useRef(null)
  const companyId = Number(localStorage.getItem('company_id'))

  const _autoResize = () => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    const h = Math.min(Math.max(ta.scrollHeight, 38), 110)
    ta.style.height = h + 'px'
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const addFiles = (fileList) => {
    const arr = Array.from(fileList || [])
    if (!arr.length) return
    setFiles((prev) => [
      ...prev,
      ...arr.map((f) => ({ id: Math.random().toString(36).slice(2), file: f, name: f.name, size: f.size })),
    ])
  }

  const removeFile = (id) => setFiles((prev) => prev.filter((f) => f.id !== id))
  const clearFiles = () => setFiles([])

  const send = async () => {
    if ((!input.trim() && files.length === 0) || loading) return
    const userMsg = input
    const attachNames = files.map((f) => f.name)
    setInput('')
    setTimeout(_autoResize, 0)
    setMessages((m) => [...m, {
      role: 'user', content: userMsg,
      attachments: attachNames.length ? attachNames : undefined,
    }])
    setLoading(true)
    try {
      const fd = new FormData()
      fd.append('message', userMsg)
      fd.append('company_id', String(companyId))
      files.forEach((f) => fd.append('files', f.file))
      const { data } = await api.post('/agent/drawing-chat', fd)
      setMockMode(data.mock_mode)
      setMessages((m) => [...m, {
        role: 'assistant',
        content: typeof data.answer === 'string' ? data.answer : JSON.stringify(data.answer),
        agent: data.agent,
        vision_used: data.vision_used,
        parsed_files: data.parsed_files || [],
      }])
    } catch (e) {
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
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 48px)' }}>
      {/* 左：上传附件面板（对话级，不进 RAG） */}
      <aside style={{
        width: '34%', minWidth: 280, maxWidth: 420, display: 'flex', flexDirection: 'column',
      }}>
        <div className="card" style={{ padding: 16, display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h3 style={{ fontSize: 15, margin: 0 }}>📎 上传附件</h3>
            {files.length > 0 && (
              <button className="btn btn-ghost" style={{ fontSize: 12, padding: '4px 8px' }} onClick={clearFiles}>
                清空
              </button>
            )}
          </div>

          <div
            onClick={() => fileInputRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files) }}
            style={{
              border: `2px dashed ${dragOver ? 'var(--primary)' : 'var(--border-strong)'}`,
              borderRadius: 10, padding: '22px 12px', textAlign: 'center', cursor: 'pointer',
              background: dragOver ? 'var(--bg-subtle)' : 'transparent',
              transition: 'all 0.15s', marginBottom: 12,
            }}
          >
            <div style={{ fontSize: 30, marginBottom: 6 }}>📐</div>
            <div style={{ fontSize: 13, color: 'var(--text)' }}>点击或拖拽文件到此处</div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
              图片 / CAD（DXF·DWG）/ PDF / Word / Excel / 文本
            </div>
            <input
              ref={fileInputRef} type="file" multiple accept={ACCEPT}
              style={{ display: 'none' }}
              onChange={(e) => { addFiles(e.target.files); e.target.value = '' }}
            />
          </div>

          <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
            {files.length === 0 && (
              <div style={{ fontSize: 12, color: 'var(--text-muted)', textAlign: 'center', padding: 20, lineHeight: 1.7 }}>
                还没有附件。<br />
                上传一张手绘图纸照片，或 DXF/PDF 图纸文件，<br />然后在右侧提问。
              </div>
            )}
            {files.map((f) => (
              <div key={f.id} style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px',
                border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-subtle)',
              }}>
                <span style={{ fontSize: 18 }}>📄</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{fmtSize(f.size)}</div>
                </div>
                <button
                  onClick={() => removeFile(f.id)}
                  style={{ border: 'none', background: 'transparent', color: 'var(--text-muted)', cursor: 'pointer', fontSize: 16 }}
                  title="移除"
                >✕</button>
              </div>
            ))}
          </div>

          <div style={{
            marginTop: 12, fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.6,
            borderTop: '1px dashed var(--border)', paddingTop: 10,
          }}>
            🔒 附件仅用于<b>本次对话</b>，<b>不会录入知识库（RAG）</b>；切换会话或刷新后需重新上传。
          </div>
        </div>
      </aside>

      {/* 右：AI 对话框 */}
      <section style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <div>
            <h1 style={{ fontSize: 22, margin: 0 }}>AI 图纸助手</h1>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              由「工艺/图纸助手」驱动 · 基于图纸回答公差/材质，提示防误操作
            </div>
          </div>
        </div>

        {mockMode && (
          <div className="alert alert-info" style={{ marginBottom: 12 }}>
            💡 当前为演示模式（未配置 LLM_API_KEY），使用内置模拟回复。配置 DeepSeek/OpenAI Key 后即为真实 AI 员工。
          </div>
        )}

        <div className="card" style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
          <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
            {messages.length === 0 && (
              <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 40, lineHeight: 1.9 }}>
                从左侧上传一张手绘图纸 / CAD / PDF，然后提问<br />
                <span style={{ fontSize: 12 }}>
                  例如：「这张图纸的外径公差是多少？材质是什么？加工时要注意什么？」
                </span>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} style={{
                display: 'flex', justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start', marginBottom: 12,
              }}>
                <div style={{ maxWidth: '80%' }}>
                  <div style={{
                    padding: '10px 14px', borderRadius: 12,
                    background: m.role === 'user' ? 'var(--primary)' : 'var(--bg-subtle)',
                    color: m.role === 'user' ? '#fff' : 'var(--text)',
                    fontSize: 14, lineHeight: 1.7, whiteSpace: 'pre-wrap',
                    border: m.role === 'user' ? 'none' : '1px solid var(--border)',
                  }}>
                    {m.role === 'assistant' && m.agent && (
                      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>{m.agent}</div>
                    )}
                    {m.content}
                  </div>
                  {m.role === 'user' && m.attachments && (
                    <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-muted)', textAlign: 'right' }}>
                      📎 {m.attachments.join('、')}
                    </div>
                  )}
                  {m.role === 'assistant' && m.parsed_files && m.parsed_files.length > 0 && (
                    <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                      {m.parsed_files.map((p, idx) => (
                        <span key={idx} style={{
                          fontSize: 10, padding: '2px 7px', borderRadius: 999,
                          background: 'var(--bg-subtle)', border: '1px solid var(--border)',
                          color: 'var(--text-muted)',
                        }}>
                          {p.type === 'image' ? '🖼️ ' : p.type === 'unsupported' ? '⚠️ ' : '📄 '}
                          {p.name}
                          {p.type === 'text' && p.params ? ` · ${p.params.length} 项参数` : ''}
                          {p.note ? ` · ${p.note}` : ''}
                        </span>
                      ))}
                    </div>
                  )}
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
              onChange={(e) => { setInput(e.target.value); _autoResize() }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
              }}
              placeholder={files.length ? '针对附件提问，Enter 发送 / Shift+Enter 换行' : '输入消息，或先从左侧上传图纸附件…'}
              rows={1}
              style={{
                flex: 1, padding: '10px 8px', resize: 'none', lineHeight: 1.5,
                minHeight: 38, maxHeight: 110, background: 'var(--bg-subtle)',
                border: '1px solid #cbd5e1', borderRadius: 6, fontSize: 14,
                fontFamily: 'inherit', outline: 'none', overflowY: 'auto', boxSizing: 'border-box',
              }}
            />
            <button className="btn btn-primary" onClick={send} disabled={loading || (!input.trim() && files.length === 0)}>
              发送
            </button>
          </div>
        </div>
      </section>
    </div>
  )
}
