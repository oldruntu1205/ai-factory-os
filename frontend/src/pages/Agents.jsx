import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api'
import PageHeader from '../components/PageHeader'

const ROLE_OPTIONS = [
  { value: 'customer', label: 'AI客服', hermes: 'customer_agent' },
  { value: 'sales', label: 'AI销售', hermes: 'sales_agent' },
  { value: 'quotation', label: 'AI报价', hermes: 'quotation_agent' },
  { value: 'production', label: 'AI生产计划', hermes: 'production_agent' },
  { value: 'quality', label: 'AI质量工程师', hermes: 'quality_agent' },
  { value: 'process', label: '工艺/图纸助手', hermes: 'process_agent',
    desc: '解析 CAD/PDF 图纸，查询型号公差/材质标准，提示防误操作要点' },
  { value: 'ceo', label: 'CEO企业大脑', hermes: 'ceo_agent' },
]

// 各角色默认职责描述（创建时预填，可改）
const DEFAULT_DESC = {
  process: '解析 CAD/PDF 图纸，回答型号公差、材质标准与防误操作要点',
}

// role → hermes agent 名（用于 Soul 编辑）
const hermesOf = (role) => (ROLE_OPTIONS.find(r => r.value === role) || {}).hermes || `${role}_agent`

export default function Agents() {
  const navigate = useNavigate()
  const [agents, setAgents] = useState([])
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ name: '', role: 'customer', description: '' })
  const [error, setError] = useState('')
  const [soulEditing, setSoulEditing] = useState(null)  // 当前编辑的 AI 员工
  const [soulContent, setSoulContent] = useState('')
  const [soulMeta, setSoulMeta] = useState({ is_custom: false, source: 'none' })
  const [soulMsg, setSoulMsg] = useState('')
  const [showSoulWarn, setShowSoulWarn] = useState(false)  // Soul 修改确认弹窗
  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const { data } = await api.get('/agent/list', { params: { company_id: companyId } })
    setAgents(data.agents)
  }

  useEffect(() => { load() }, [])

  const create = async () => {
    try {
      await api.post('/agent/create', { ...form, company_id: companyId })
      setShowForm(false)
      setForm({ name: '', role: 'customer', description: '' })
      load()
    } catch (e) {
      setError(e.response?.data?.detail || '创建失败')
    }
  }

  const remove = async (id) => {
    if (!window.confirm('确认删除该 AI 员工？')) return
    await api.delete(`/agent/${id}`, { params: { company_id: companyId } })
    load()
  }

  const openSoulEditor = async (agent) => {
    setSoulEditing(agent)
    setSoulMsg('')
    const hermesName = hermesOf(agent.role)
    try {
      const { data } = await api.get(`/hermes/agent/${hermesName}/soul`, {
        params: { company_id: companyId } })
      setSoulContent(data.content || '')
      setSoulMeta({ is_custom: data.is_custom, source: data.source, name: hermesName })
    } catch (e) {
      setSoulContent('')
      setSoulMsg(`❌ ${e.response?.data?.detail || '读取 Soul 失败'}`)
    }
  }

  const saveSoul = async () => {
    if (!soulEditing) return
    setSoulMsg('')
    try {
      await api.put(`/hermes/agent/${soulMeta.name}/soul`,
        { content: soulContent }, { params: { company_id: companyId } })
      setSoulMsg('✅ Soul 已保存到后端 soul.md 文件')
      setSoulMeta({ ...soulMeta, is_custom: true, source: 'company' })
    } catch (e) {
      setSoulMsg(`❌ ${e.response?.data?.detail || '保存失败'}`)
    }
  }

  const resetSoul = async () => {
    if (!soulEditing) return
    if (!window.confirm('确认清除企业定制 Soul？将恢复为平台默认')) return
    try {
      await api.delete(`/hermes/agent/${soulMeta.name}/soul`,
        { params: { company_id: companyId } })
      // 重新拉取默认
      const { data } = await api.get(`/hermes/agent/${soulMeta.name}/soul`,
        { params: { company_id: companyId } })
      setSoulContent(data.content || '')
      setSoulMeta({ is_custom: false, source: data.source, name: soulMeta.name })
      setSoulMsg('✅ 已恢复平台默认 Soul')
    } catch (e) {
      setSoulMsg(`❌ ${e.response?.data?.detail || '重置失败'}`)
    }
  }

  return (
    <div>
      <PageHeader title="AI 员工管理">
        <button className="btn btn-primary btn-sm" onClick={() => setShowForm(!showForm)}>
          {showForm ? '取消' : '+ 创建 AI 员工'}
        </button>
      </PageHeader>

      {error && <div className="alert alert-error">{error}</div>}

      {showForm && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 2fr auto', gap: 12, alignItems: 'end' }}>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label>员工姓名</label>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如：Lisa" />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label>岗位角色</label>
              <select value={form.role} onChange={(e) => {
                const role = e.target.value
                setForm({ ...form, role, description: DEFAULT_DESC[role] || '' })
              }}>
                {ROLE_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
              </select>
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label>职责描述</label>
              <input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
            </div>
            <button className="btn btn-success" onClick={create}>创建</button>
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 16 }}>
        {agents.map((a) => (
          <div key={a.id} className="card">
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
              <div style={{
                width: 44, height: 44, borderRadius: 22, background: 'var(--primary)',
                color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 18, fontWeight: 700,
              }}>
                {a.name[0]}
              </div>
              <div>
                <div style={{ fontWeight: 700 }}>{a.name}</div>
                <span className="badge badge-blue">{a.role_label}</span>
              </div>
            </div>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', minHeight: 36, marginBottom: 12 }}>
              {a.description || '暂无描述'}
            </p>
            <div style={{ display: 'flex', gap: 8 }}>
              <span className={`badge ${a.status === 'active' ? 'badge-green' : 'badge-gray'}`}>
                {a.status === 'active' ? '工作中' : '已停用'}
              </span>
              <button className="btn btn-primary btn-sm" style={{ marginLeft: 'auto' }}
                onClick={() => navigate(`/chat?agent_id=${a.id}`)}>
                💬 对话
              </button>
              <button className="btn btn-secondary btn-sm"
                onClick={() => openSoulEditor(a)}>
                🎭 性格设定
              </button>
              <button className="btn btn-danger btn-sm" onClick={() => remove(a.id)}>
                删除
              </button>
            </div>
          </div>
        ))}
        {agents.length === 0 && (
          <div className="card" style={{ textAlign: 'center', color: 'var(--text-muted)' }}>
            暂无 AI 员工，点击右上角创建
          </div>
        )}
      </div>

      {/* Soul 编辑模态框 */}
      {soulEditing && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }} onClick={() => setSoulEditing(null)}>
          <div className="card" style={{ width: 720, maxHeight: '85vh', display: 'flex', flexDirection: 'column' }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
              <div style={{ fontSize: 28 }}>🎭</div>
              <div>
                <h3 style={{ fontSize: 18 }}>「{soulEditing.name}」的性格设定</h3>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  Hermes Agent: <code>{soulMeta.name}</code> ·
                  来源：<span className={`badge ${soulMeta.source === 'company' ? 'badge-green' : 'badge-gray'}`}>
                    {soulMeta.source === 'company' ? '企业定制' : soulMeta.source === 'default' ? '平台默认' : '无'}
                  </span>
                </div>
              </div>
              <button className="btn btn-secondary btn-sm" style={{ marginLeft: 'auto' }}
                onClick={() => setSoulEditing(null)}>✕</button>
            </div>

            <div className="alert alert-info" style={{ fontSize: 12 }}>
              性格设定 = AI 员工的「语气/角色/业务边界」，以 Markdown 格式注入到 LLM prompt 头部。
              修改后立即生效，下次该 Agent 执行任务时会按新性格回复。
            </div>

            {soulMsg && (
              <div className={`alert ${soulMsg.startsWith('✅') ? 'alert-success' : 'alert-error'}`}>
                {soulMsg}
              </div>
            )}

            <textarea
              value={soulContent}
              onChange={(e) => setSoulContent(e.target.value)}
              style={{
                flex: 1, minHeight: 320, padding: 12, fontFamily: 'ui-monospace, monospace',
                fontSize: 13, lineHeight: 1.6, resize: 'vertical', border: '1px solid #cbd5e1',
                borderRadius: 8, outline: 'none',
              }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 8 }}>
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                {soulContent.length} 字符 · 后端路径: <code>hermes/registry/souls/company_{companyId}/{soulMeta.name}.md</code>
              </span>
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="btn btn-secondary" onClick={resetSoul}
                  disabled={!soulMeta.is_custom}
                  style={{ whiteSpace: 'nowrap' }}>
                  恢复默认
                </button>
                <button className="btn btn-primary" onClick={() => setShowSoulWarn(true)}
                  style={{ whiteSpace: 'nowrap' }}>
                  💾 保存
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Soul 修改确认弹窗 */}
      {showSoulWarn && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200,
        }} onClick={() => setShowSoulWarn(false)}>
          <div className="card" style={{ width: 460, padding: 24 }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ fontSize: 15, lineHeight: 1.7, marginBottom: 20 }}>
              <div style={{ fontWeight: 600, marginBottom: 10 }}>
                警告(｡ŏ_ŏ)⚠️：频繁修改 AI 员工的性格（soul），会影响其自进化能力
              </div>
              <div style={{ fontSize: 13, color: '#475569', marginBottom: 10 }}>
                平台采用「双层 Soul」机制：AI 员工自主进化（GEPA）会持续改进
                <b>底座层</b>；而你在这里手动修改写入的是<b>覆盖层</b>，优先于底座层生效。
              </div>
              <ul style={{ fontSize: 13, color: '#475569', margin: '0 0 10px 18px', padding: 0 }}>
                <li>覆盖层会遮蔽底座层的进化成果——手动改得越多，AI 员工「自己变聪明」的部分越难体现；</li>
                <li>每次手动重写 Soul，会打断 GEPA 基于近期对话轨迹的收敛信号，进化更容易偏离你的真实意图；</li>
                <li>建议：先让 AI 员工跑一段时间（自动产出候选，待你在「进化候选」里审批），确需调整基调时再手动改。</li>
              </ul>
              <div>确认仍要修改吗？</div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button className="btn btn-secondary" onClick={() => setShowSoulWarn(false)}>
                算了
              </button>
              <button className="btn btn-primary"
                onClick={() => { setShowSoulWarn(false); saveSoul(); }}>
                确定
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}