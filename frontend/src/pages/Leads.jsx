import React, { useEffect, useState } from 'react'
import api from '../api'
import PageHeader from '../components/PageHeader'

const COUNTRIES = ['Germany', 'United Kingdom', 'United States', 'France', 'Sweden',
  'Japan', 'Italy', 'Australia', 'India', 'Brazil', 'Canada', 'Netherlands', 'China']
const COUNTRY_LABELS = {
  Germany: '德国', 'United Kingdom': '英国', 'United States': '美国', France: '法国',
  Sweden: '瑞典', Japan: '日本', Italy: '意大利', Australia: '澳大利亚',
  India: '印度', Brazil: '巴西', Canada: '加拿大', Netherlands: '荷兰', China: '中国',
  DE: '德国', GB: '英国', UK: '英国', US: '美国', FR: '法国', SE: '瑞典', JP: '日本',
  IT: '意大利', AU: '澳大利亚', IN: '印度', BR: '巴西', CA: '加拿大', NL: '荷兰', CN: '中国',
}
const countryLabel = v => COUNTRY_LABELS[v] || v || '—'
const INDUSTRIES = ['实验室化学品', '科研仪器', '生物技术', '制药', '分析仪器',
  '玻璃制品', '医疗器械', '高校科研', '食品检测', '环境监测']

const STATUS_MAP = {
  discovered: ['badge-blue', '已发现'],
  contacted: ['badge-yellow', '已联系'],
  replied: ['badge-green', '已回复'],
  qualified: ['badge-purple', '已转化商机'],
  won: ['badge-green', '已成交'],
  paused: ['badge-gray', '已暂停'],
  unsubscribed: ['badge-red', '已退订'],
}
const GRADE_CLASS = { A: 'badge-gold', B: 'badge-blue', C: 'badge-gray' }

export default function Leads() {
  const companyId = Number(localStorage.getItem('company_id'))
  const [leads, setLeads] = useState([])
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(false)
  const [msg, setMsg] = useState('')

  const [keywords, setKeywords] = useState('laboratory glassware')
  const [countries, setCountries] = useState([])
  const [industries, setIndustries] = useState([])
  const [limit, setLimit] = useState(20)
  const [language, setLanguage] = useState('en')
  const [variant, setVariant] = useState('A')

  const [gradeFilter, setGradeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [detail, setDetail] = useState(null)
  // 真实询盘导入（出口型 AI 应用）
  const [showImport, setShowImport] = useState(false)
  const [importText, setImportText] = useState('')
  const [importAutoFollow, setImportAutoFollow] = useState(false)
  const [importing, setImporting] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const [l, s] = await Promise.all([
        api.get('/leads/list', { params: { company_id: companyId, grade: gradeFilter, status: statusFilter } }),
        api.get('/leads/stats', { params: { company_id: companyId } }),
      ])
      setLeads(l.data.leads)
      setStats(s.data)
    } catch (e) {
      setMsg('❌ ' + (e.response?.data?.detail || '加载失败'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [gradeFilter, statusFilter])

  const toggleChip = (list, setList, v) =>
    setList(list.includes(v) ? list.filter(x => x !== v) : [...list, v])

  // 真实询盘导入：支持 JSON 数组 或 "公司名|邮箱|国家|需求" 每行一条
  const importLeads = async () => {
    if (!importText.trim()) { setMsg('⚠️ 请粘贴询盘内容'); return }
    setImporting(true); setMsg('')
    let items = []
    try {
      const arr = JSON.parse(importText.trim())
      if (Array.isArray(arr)) items = arr
    } catch { /* 行格式 */ }
    if (!items.length) {
      items = importText.split('\n').filter(l => l.trim()).map(l => {
        const [company_name, email, country, ...rest] = l.split('|').map(s => s.trim())
        return { company_name: company_name || '', email: email || '',
                 country: country || '', requirement: rest.join('|') || '' }
      })
    }
    try {
      const { data } = await api.post('/leads/import', {
        company_id: companyId, leads: items, auto_follow: importAutoFollow,
      })
      setMsg(`✅ 已导入 ${data.count} 条询盘（自动首封 ${data.followed} 条），AI 已解析并评分`)
      setShowImport(false); setImportText('')
      load()
    } catch (e) {
      setMsg(`❌ 导入失败：${e.response?.data?.detail || e.message}`)
    } finally { setImporting(false) }
  }

  const discover = async () => {
    setLoading(true); setMsg('')
    try {
      const { data } = await api.post('/leads/discover', {
        company_id: companyId, keywords, countries, industries, limit,
      })
      setMsg(`✅ 发现 ${data.count} 条新线索（已自动评分）`)
      load()
    } catch (e) {
      setMsg('❌ ' + (e.response?.data?.detail || '发现失败'))
    } finally { setLoading(false) }
  }

  const sendNext = async (lead) => {
    const step = (lead.follow_up_step || 0) + 1
    if (step > 4) { setMsg('⚠️ 该线索序列已全部发送完毕'); return }
    try {
      const { data } = await api.post(`/leads/${lead.id}/send-sequence`, {
        company_id: companyId, step, language, variant,
      })
      const st = data.result?.status
      setMsg(st === 'simulated'
        ? `📧 第${step}封已生成（演示模式未实际发送，已记录主题「${data.result?.subject}」）`
        : `📧 第${step}封已发送（${st}）`)
      load()
    } catch (e) { setMsg('❌ ' + (e.response?.data?.detail || '发送失败')) }
  }

  const markReply = async (id) => {
    await api.post(`/leads/${id}/mark-reply`, { company_id: companyId }); load()
  }
  const togglePause = async (lead) => {
    const url = lead.status === 'paused' ? `/leads/${lead.id}/resume` : `/leads/${lead.id}/pause`
    await api.post(url, { company_id: companyId }); load()
  }
  const convert = async (id) => {
    if (!confirm('确认将该线索转入 CRM 客户库？')) return
    await api.post(`/leads/${id}/convert`, { company_id: companyId }); load()
  }
  const runSequence = async () => {
    try {
      const { data } = await api.post('/leads/run-sequence', { company_id: companyId })
      setMsg(`🤖 自动跟进执行完成，本次发送 ${data.count} 封`)
      load()
    } catch (e) { setMsg('❌ ' + (e.response?.data?.detail || '执行失败')) }
  }

  const contacted = stats ? (stats.by_status.contacted || 0) + (stats.by_status.replied || 0) : 0

  return (
    <div>
      <PageHeader title="AI 自主销售（全自动获客）">
        <button className="btn btn-sm" style={{ fontSize: 13 }} onClick={() => setShowImport(true)}>📥 导入真实询盘</button>
        <button className="btn btn-primary btn-sm" onClick={runSequence}>🤖 运行自动跟进</button>
        <button className="btn btn-sm" onClick={load}>刷新</button>
      </PageHeader>

      {/* 真实询盘导入弹窗 */}
      {showImport && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)', zIndex: 100,
          display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={() => setShowImport(false)}>
          <div className="card" style={{ width: 640, maxWidth: '92vw', padding: 20 }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
              <b style={{ fontSize: 16 }}>导入真实询盘（AI 解析 + 评分）</b>
              <button className="btn btn-sm" onClick={() => setShowImport(false)}>✕</button>
            </div>
            <textarea rows={8} value={importText}
              onChange={(e) => setImportText(e.target.value)}
              placeholder={'支持两种格式：\n1) JSON 数组：[{"company_name":"XX GmbH","email":"a@x.de","country":"DE","requirement":"We need 1000 glass beakers CIF Hamburg"}]  \n2) 每行一条：公司名|邮箱|国家|需求'} 
              style={{ width: '100%', boxSizing: 'border-box', fontSize: 13, padding: 10,
                border: '1px solid #cbd5e1', borderRadius: 6, fontFamily: 'monospace' }} />
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '10px 0' }}>
              <input type="checkbox" id="af" checked={importAutoFollow}
                onChange={(e) => setImportAutoFollow(e.target.checked)} />
              <label htmlFor="af" style={{ fontSize: 13 }}>导入后自动发送首封开发信（按客户国家语言）</label>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button className="btn btn-sm" onClick={() => setShowImport(false)}>取消</button>
              <button className="btn btn-primary btn-sm" onClick={importLeads} disabled={importing}>
                {importing ? '导入解析中...' : '导入并 AI 解析'}
              </button>
            </div>
          </div>
        </div>
      )}

      {msg && <div className="card" style={{ marginBottom: 16, padding: 12, background: 'var(--bg-subtle)' }}>{msg}</div>}

      {/* 发现配置 */}
      <div className="card" style={{ marginBottom: 16, padding: 16 }}>
        <div style={{ fontWeight: 600, marginBottom: 12 }}>① 客户发现（关键词 / 国家 / 行业筛选）</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12, marginBottom: 12 }}>
          <div className="form-group"><label>搜索关键词</label>
            <input value={keywords} onChange={e => setKeywords(e.target.value)} placeholder="如 laboratory glassware" /></div>
          <div className="form-group"><label>发现数量</label>
            <input type="number" min={1} max={100} value={limit}
              onChange={e => setLimit(Number(e.target.value))} /></div>
          <div className="form-group"><label>邮件语言</label>
            <select value={language} onChange={e => setLanguage(e.target.value)}>
              <option value="en">英文</option><option value="zh">中文</option></select></div>
          <div className="form-group"><label>文案 A/B 变体</label>
            <select value={variant} onChange={e => setVariant(e.target.value)}>
              <option value="A">A（标准）</option><option value="B">B（紧迫感）</option></select></div>
        </div>
        <div style={{ marginBottom: 8, fontSize: 13, color: 'var(--text-muted)' }}>国家 / 地区（可多选，不选=不限）</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 }}>
          {COUNTRIES.map(c => (
            <span key={c} onClick={() => toggleChip(countries, setCountries, c)}
              style={{ cursor: 'pointer', padding: '4px 10px', borderRadius: 16, fontSize: 12,
                border: '1px solid var(--border)', userSelect: 'none',
                background: countries.includes(c) ? 'var(--primary)' : 'transparent',
                color: countries.includes(c) ? '#fff' : 'var(--text)' }}>
              {COUNTRY_LABELS[c] || c}</span>
          ))}
        </div>
        <div style={{ marginBottom: 8, fontSize: 13, color: 'var(--text-muted)' }}>行业（可多选，不选=不限）</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 14 }}>
          {INDUSTRIES.map(c => (
            <span key={c} onClick={() => toggleChip(industries, setIndustries, c)}
              style={{ cursor: 'pointer', padding: '4px 10px', borderRadius: 16, fontSize: 12,
                border: '1px solid var(--border)', userSelect: 'none',
                background: industries.includes(c) ? 'var(--primary)' : 'transparent',
                color: industries.includes(c) ? '#fff' : 'var(--text)' }}>
              {c}</span>
          ))}
        </div>
        <button className="btn btn-primary btn-sm" onClick={discover} disabled={loading}>
          🔍 开始发现客户
        </button>
      </div>

      {/* 统计卡 */}
      {stats && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 14, marginBottom: 16 }}>
          {[
            { k: '总线索', v: stats.total },
            { k: 'A 级客户', v: stats.by_grade.A },
            { k: '已联系', v: contacted },
            { k: '已回复', v: stats.by_status.replied || 0 },
            { k: '已转化 CRM', v: stats.converted },
            { k: '邮件打开', v: stats.opened },
          ].map(s => (
            <div key={s.k} className="card" style={{ textAlign: 'center', padding: 14 }}>
              <div style={{ fontSize: 22, fontWeight: 700 }}>{s.v}</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{s.k}</div>
            </div>
          ))}
        </div>
      )}

      {/* 筛选 */}
      <div className="card" style={{ marginBottom: 16, padding: 12, display: 'flex', gap: 12, alignItems: 'center' }}>
        <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>筛选：</span>
        <select value={gradeFilter} onChange={e => setGradeFilter(e.target.value)}>
          <option value="">全部等级</option><option value="A">A 级</option>
          <option value="B">B 级</option><option value="C">C 级</option></select>
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
          <option value="">全部状态</option>
          {Object.entries(STATUS_MAP).map(([k, v]) => <option key={k} value={k}>{v[1]}</option>)}
        </select>
        <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>共 {leads.length} 条</span>
      </div>

      {/* 线索表 */}
      <div className="card" style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead>
            <tr>
              <th>公司</th><th>国家</th><th>行业</th><th>邮箱</th>
              <th>合规</th><th>评分</th><th>状态</th><th>跟进</th><th>打开</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {leads.map(l => {
              const st = STATUS_MAP[l.status] || ['badge-gray', l.status]
              const step = l.follow_up_step || 0
              return (
                <tr key={l.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{l.company_name}</div>
                    <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{l.website}</div>
                  </td>
                  <td>{countryLabel(l.country)}</td>
                  <td>{l.industry}</td>
                  <td style={{ fontSize: 12 }}>{l.email || <span style={{ color: 'var(--text-muted)' }}>—</span>}</td>
                  <td>
                    {l.compliance_status === 'flagged' ? (
                      <span className="badge alert-error" style={{ fontSize: 11 }}
                        title={(l.compliance_reasons || []).join('\n') || '合规风险，需人工核验'}>
                        ⚠️ 合规风险
                      </span>
                    ) : (
                      <span className="badge" style={{ fontSize: 11, background: 'var(--bg-subtle)' }}>正常</span>
                    )}
                    {l.cert_hint && (
                      <div style={{ fontSize: 10, color: 'var(--text-muted)' }} title="认证要求提示">{l.cert_hint}</div>
                    )}
                  </td>
                  <td>
                    <span className={`badge ${GRADE_CLASS[l.grade]}`}>{l.grade}</span>
                    <span style={{ marginLeft: 6, fontWeight: 700 }}>{l.score}</span>
                    <div><button className="link" style={{ fontSize: 11 }} onClick={() => setDetail(l)}>评分理由</button></div>
                  </td>
                  <td><span className={`badge ${st[0]}`}>{st[1]}</span></td>
                  <td>{step > 0 ? `第 ${step}/4 封` : '未发送'}</td>
                  <td>{l.opened ? `✅ ${l.open_count}` : '—'}</td>
                  <td>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
                      <button className="btn btn-sm btn-primary" onClick={() => sendNext(l)} disabled={step >= 4}>
                        {step >= 4 ? '已发完' : `发送第 ${step + 1} 封`}
                      </button>
                      <div style={{ display: 'flex', gap: 4 }}>
                        <button className="btn btn-sm" onClick={() => markReply(l.id)}>回复</button>
                        <button className="btn btn-sm" onClick={() => togglePause(l)}>
                          {l.status === 'paused' ? '恢复' : '暂停'}</button>
                        <button className="btn btn-sm" onClick={() => convert(l.id)} disabled={l.converted}>
                          {l.converted ? '已转' : '转CRM'}</button>
                      </div>
                    </div>
                  </td>
                </tr>
              )
            })}
            {!leads.length && (
              <tr><td colSpan={9} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 30 }}>
                暂无线索，点击上方「开始发现客户」启动 AI 全自动获客</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 评分理由弹窗 */}
      {detail && (
        <div onClick={() => setDetail(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.4)', display: 'flex',
            alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
          <div onClick={e => e.stopPropagation()} className="card" style={{ maxWidth: 460, padding: 20 }}>
            <h3 style={{ marginBottom: 12 }}>{detail.company_name} · 评分详情</h3>
            <div style={{ marginBottom: 8 }}><b>等级/分值：</b>
              <span className={`badge ${GRADE_CLASS[detail.grade]}`}>{detail.grade}</span> {detail.score}</div>
            <div style={{ marginBottom: 8 }}><b>评分理由：</b>
              <div style={{ marginTop: 4, fontSize: 13, lineHeight: 1.7 }}>{detail.score_reasons}</div></div>
            <div style={{ marginBottom: 8, fontSize: 13 }}><b>富集信息：</b>
              <pre style={{ background: 'var(--bg-subtle)', padding: 10, borderRadius: 6, fontSize: 12 }}>
                {JSON.stringify(detail.enriched, null, 2)}</pre></div>
            <button className="btn btn-sm" onClick={() => setDetail(null)}>关闭</button>
          </div>
        </div>
      )}
    </div>
  )
}
