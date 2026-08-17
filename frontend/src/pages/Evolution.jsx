import React, { useEffect, useState } from 'react'
import api from '../api'

/**
 * 自进化管理（Hermes Evolution）——企业侧 AI 员工进化审批台
 * - 候选审批：GEPA 产出的 pending 候选，人工 approve/reject（Human-in-the-loop 门）
 * - 技能管理：查看已沉淀技能，恢复被隔离技能 / 删除
 * - 执行轨迹：AI 员工运行轨迹（GEPA 学习信号来源）
 */
export default function Evolution() {
  const companyId = Number(localStorage.getItem('company_id'))
  const [tab, setTab] = useState('candidates')
  const [candidates, setCandidates] = useState([])
  const [skills, setSkills] = useState([])
  const [traces, setTraces] = useState([])
  const [status, setStatus] = useState('pending')
  const [loading, setLoading] = useState(false)
  const [msg, setMsg] = useState('')
  const [expanded, setExpanded] = useState(null)
  const [running, setRunning] = useState(false)

  const errText = (e) => (e.response?.data?.detail || e.message || '操作失败')

  const loadCandidates = async () => {
    setLoading(true)
    try {
      const params = { company_id: companyId }
      if (status) params.status = status
      const { data } = await api.get('/hermes/evolution/candidates', { params })
      setCandidates(data.candidates || [])
    } catch (e) { setMsg('加载候选失败: ' + errText(e)) } finally { setLoading(false) }
  }
  const loadSkills = async () => {
    try {
      const { data } = await api.get('/hermes/evolution/skills', {
        params: { company_id: companyId, include_quarantined: true, limit: 200 },
      })
      setSkills(data.skills || [])
    } catch (e) { setMsg('加载技能失败: ' + errText(e)) }
  }
  const loadTraces = async () => {
    try {
      const { data } = await api.get('/hermes/evolution/traces', {
        params: { company_id: companyId, limit: 50 },
      })
      setTraces(data.traces || [])
    } catch (e) { setMsg('加载轨迹失败: ' + errText(e)) }
  }

  useEffect(() => {
    if (tab === 'candidates') loadCandidates()
    else if (tab === 'skills') loadSkills()
    else if (tab === 'traces') loadTraces()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, status])

  const approve = async (id) => {
    if (!window.confirm('确认「通过」该进化候选？将写回运行态（Soul 底座层 / 工具描述 / 技能）。')) return
    try {
      await api.post(`/hermes/evolution/candidates/${id}/approve`, null, { params: { company_id: companyId } })
      setMsg('✅ 已通过并应用')
      loadCandidates()
    } catch (e) { setMsg('❌ ' + errText(e)) }
  }
  const reject = async (id) => {
    if (!window.confirm('确认拒绝该候选？')) return
    try {
      await api.post(`/hermes/evolution/candidates/${id}/reject`, null, { params: { company_id: companyId } })
      setMsg('已拒绝')
      loadCandidates()
    } catch (e) { setMsg('❌ ' + errText(e)) }
  }
  const restoreSkill = async (id) => {
    try {
      await api.post(`/hermes/evolution/skills/${id}/restore`, null, { params: { company_id: companyId } })
      setMsg('✅ 技能已恢复（解除隔离）')
      loadSkills()
    } catch (e) { setMsg('❌ ' + errText(e)) }
  }
  const deleteSkill = async (id, title) => {
    if (!window.confirm(`确认删除技能「${title || id}」？`)) return
    try {
      await api.delete(`/hermes/evolution/skills/${id}`, { params: { company_id: companyId } })
      setMsg('已删除')
      loadSkills()
    } catch (e) { setMsg('❌ ' + errText(e)) }
  }
  const runAuto = async () => {
    if (!window.confirm('触发「全自动进化循环」？将扫描近期轨迹并调用 LLM 生成候选（仅产出 pending，需人工审批）。')) return
    setRunning(true)
    try {
      const { data } = await api.post('/hermes/evolution/auto-run', null, { params: { company_id: companyId } })
      setMsg(`✅ 进化循环完成：${data.candidates_created ?? ''} 个候选（详见响应）`)
      loadCandidates()
    } catch (e) { setMsg('❌ ' + errText(e)) } finally { setRunning(false) }
  }

  const typeLabel = { soul: 'Soul人格', tool: '工具描述', skill: '技能', tool_code: '工具代码' }
  const statusColor = { pending: 'yellow', applied: 'success', rejected: '' }

  return (
    <div>
      <h1 style={{ fontSize: 22, marginBottom: 4 }}>自进化管理</h1>
      <p style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 16 }}>
        GEPA 自进化引擎：AI 员工从运行轨迹中沉淀技能、产出改进候选，<b>必须经人工审批才生效</b>
      </p>

      {msg && <div className={`alert alert-${msg.startsWith('❌') ? 'error' : 'info'}`} style={{ marginBottom: 16 }}>{msg}</div>}

      <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        {[['candidates', '候选审批'], ['skills', '技能'], ['traces', '执行轨迹']].map(([k, l]) => (
          <button key={k} className={`btn ${tab === k ? 'btn-primary' : ''}`} style={{ fontSize: 13 }}
            onClick={() => { setTab(k); setMsg('') }}>
            {l}
          </button>
        ))}
        {tab === 'candidates' && (
          <button className="btn" style={{ fontSize: 13, marginLeft: 'auto' }} disabled={running} onClick={runAuto}>
            {running ? '运行中...' : '⚡ 触发全自动进化'}
          </button>
        )}
      </div>

      {/* ===== 候选审批 ===== */}
      {tab === 'candidates' && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>状态筛选：</span>
            {['', 'pending', 'applied', 'rejected'].map((s) => (
              <button key={s || 'all'} className={`btn btn-sm ${status === s ? 'btn-primary' : ''}`}
                onClick={() => setStatus(s)}>
                {s === '' ? '全部' : s === 'pending' ? '待审批' : s === 'applied' ? '已应用' : '已拒绝'}
              </button>
            ))}
          </div>
          {loading && <div style={{ color: 'var(--text-muted)' }}>加载中...</div>}
          {!loading && candidates.length === 0 && (
            <div className="card" style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)' }}>
              {status === 'pending' ? '暂无待审批候选——可点击右上角「触发全自动进化」生成' : '该状态下暂无候选'}
            </div>
          )}
          {candidates.map((c) => (
            <div key={c.id} className="card" style={{ marginBottom: 12, padding: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <span className="badge" style={{ background: 'rgba(77,139,255,0.15)', color: '#4d8bff' }}>#{c.id}</span>
                <b>{c.agent_name}</b>
                <span className="badge" style={{ background: 'var(--bg-subtle)' }}>{typeLabel[c.target_type] || c.target_type}</span>
                {c.target_key && <span style={{ fontSize: 12, color: 'var(--text-muted)', wordBreak: 'break-all' }}>{c.target_key}</span>}
                <span className={`badge alert-${statusColor[c.status] || 'info'}`} style={{ fontSize: 12 }}>
                  {c.status === 'pending' ? '⏳ 待审批' : c.status === 'applied' ? '✅ 已应用' : '❌ 已拒绝'}
                </span>
                {c.soul_changed && (
                  <span className="badge alert-yellow" style={{ fontSize: 12 }} title="评估时的 Soul 版本与当前不一致">
                    ⚠️ Soul 基线已漂移
                  </span>
                )}
                {c.guardrails_pass === false && (
                  <span className="badge alert-error" style={{ fontSize: 12 }}>护栏未全过</span>
                )}
                <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--text-muted)' }}>
                  基线 {c.baseline_score ?? '-'} → 候选 {c.eval_score ?? '-'}
                  {c.lift != null && <span style={{ color: c.lift >= 0 ? 'green' : 'red', fontWeight: 700 }}> (Δ{c.lift > 0 ? '+' : ''}{c.lift})</span>}
                </span>
              </div>
              {c.rationale && <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: '8px 0' }}>理由：{c.rationale}</p>}
              <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                {c.status === 'pending' && (
                  <>
                    <button className="btn btn-primary btn-sm" onClick={() => approve(c.id)}>✅ 通过并应用</button>
                    <button className="btn btn-sm" onClick={() => reject(c.id)}>❌ 拒绝</button>
                  </>
                )}
                <button className="btn btn-sm" style={{ marginLeft: 'auto' }}
                  onClick={() => setExpanded(expanded === c.id ? null : c.id)}>
                  {expanded === c.id ? '收起内容' : '查看进化内容'}
                </button>
              </div>
              {expanded === c.id && (
                <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, fontSize: 12 }}>
                  <div>
                    <div style={{ fontWeight: 700, marginBottom: 6, color: 'var(--text-muted)' }}>当前内容</div>
                    <pre style={{ background: 'var(--bg-subtle)', padding: 10, borderRadius: 6, whiteSpace: 'pre-wrap', maxHeight: 300, overflowY: 'auto', margin: 0 }}>{c.current_text || '（无）'}</pre>
                  </div>
                  <div>
                    <div style={{ fontWeight: 700, marginBottom: 6, color: '#4d8bff' }}>候选内容</div>
                    <pre style={{ background: 'rgba(77,139,255,0.06)', padding: 10, borderRadius: 6, whiteSpace: 'pre-wrap', maxHeight: 300, overflowY: 'auto', margin: 0 }}>{c.candidate_text || '（无）'}</pre>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ===== 技能管理 ===== */}
      {tab === 'skills' && (
        <div className="card" style={{ padding: 16, overflowX: 'auto' }}>
          {skills.length === 0 && <div style={{ color: 'var(--text-muted)' }}>暂无已沉淀技能（AI 员工重复执行同类任务后自动生成）</div>}
          {skills.length > 0 && (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>
                  <th style={{ padding: '8px 6px' }}>技能</th>
                  <th style={{ padding: '8px 6px' }}>Agent</th>
                  <th style={{ padding: '8px 6px' }}>使用/性能</th>
                  <th style={{ padding: '8px 6px' }}>版本</th>
                  <th style={{ padding: '8px 6px' }}>状态</th>
                  <th style={{ padding: '8px 6px' }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {skills.map((s) => (
                  <tr key={s.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td style={{ padding: '8px 6px' }}>
                      <b>{s.title || s.skill_key}</b>
                      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{s.skill_key} · 触发: {(s.trigger || '—').slice(0, 40)}</div>
                    </td>
                    <td style={{ padding: '8px 6px' }}>{s.agent_name}</td>
                    <td style={{ padding: '8px 6px' }}>{s.usage_count} 次 / {s.perf_score != null ? s.perf_score.toFixed(2) : '-'}</td>
                    <td style={{ padding: '8px 6px' }}>v{s.version}</td>
                    <td style={{ padding: '8px 6px' }}>
                      {s.quarantined ? (
                        <span className="badge alert-error" style={{ fontSize: 11 }}>已隔离{s.quarantine_reason ? `（${String(s.quarantine_reason).slice(0, 20)}）` : ''}</span>
                      ) : s.stale ? (
                        <span className="badge alert-yellow" style={{ fontSize: 11 }}>陈旧</span>
                      ) : (
                        <span className="badge alert-success" style={{ fontSize: 11 }}>正常</span>
                      )}
                    </td>
                    <td style={{ padding: '8px 6px' }}>
                      {s.quarantined && (
                        <button className="btn btn-sm" style={{ marginRight: 6 }} onClick={() => restoreSkill(s.id)}>恢复</button>
                      )}
                      <button className="btn btn-sm" onClick={() => deleteSkill(s.id, s.title)}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* ===== 执行轨迹 ===== */}
      {tab === 'traces' && (
        <div className="card" style={{ padding: 16, overflowX: 'auto' }}>
          {traces.length === 0 && <div style={{ color: 'var(--text-muted)' }}>暂无执行轨迹（AI 员工对话后产生）</div>}
          {traces.length > 0 && (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>
                  <th style={{ padding: '8px 6px' }}>Agent</th>
                  <th style={{ padding: '8px 6px' }}>任务输入</th>
                  <th style={{ padding: '8px 6px' }}>工具数</th>
                  <th style={{ padding: '8px 6px' }}>质量分</th>
                  <th style={{ padding: '8px 6px' }}>时间</th>
                </tr>
              </thead>
              <tbody>
                {traces.map((t) => (
                  <tr key={t.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td style={{ padding: '8px 6px' }}>{t.agent_name}</td>
                    <td style={{ padding: '8px 6px', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.task_input}>{t.task_input || '—'}</td>
                    <td style={{ padding: '8px 6px' }}>{t.tool_count ?? 0}</td>
                    <td style={{ padding: '8px 6px' }}>{t.quality_score ?? '-'}</td>
                    <td style={{ padding: '8px 6px', color: 'var(--text-muted)', fontSize: 12 }}>{t.created_at ? String(t.created_at).slice(0, 19) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
