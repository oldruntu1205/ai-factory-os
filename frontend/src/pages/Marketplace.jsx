import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api'
import PageHeader from '../components/PageHeader'

export default function Marketplace() {
  const [packages, setPackages] = useState([])
  const [industries, setIndustries] = useState(['all'])
  const [activeIndustry, setActiveIndustry] = useState('all')
  const [installed, setInstalled] = useState([])
  const [seats, setSeats] = useState(null)
  const [detail, setDetail] = useState(null)
  const [installing, setInstalling] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')
  const [confirmInstall, setConfirmInstall] = useState(null)
  const navigate = useNavigate()
  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const [pkgs, inds, inst] = await Promise.all([
      api.get('/marketplace/packages', { params: { industry: activeIndustry } }),
      api.get('/marketplace/industries'),
      api.get('/marketplace/installed', { params: { company_id: companyId } }),
    ])
    setPackages(pkgs.data.packages)
    setSeats(pkgs.data.company_seats || null)  // {max, used, remaining}
    setIndustries(['all', ...inds.data.industries])  // [{code, name}, ...]
    setInstalled(inst.data.installed)
  }

  useEffect(() => { load() }, [activeIndustry])

  const install = async (pkg) => {
    setConfirmInstall(pkg)
  }
  const doInstall = async () => {
    if (!confirmInstall) return
    const pkg = confirmInstall
    setConfirmInstall(null)
    setInstalling(true)
    try {
      const { data } = await api.post('/marketplace/install', {
        company_id: companyId, package_id: pkg.id,
      })
      setMsg(`✅ 安装成功！创建了 ${data.agents.length} 个 AI 员工`)
      setDetail(null)
      load()
    } catch (e) {
      setError(e.response?.data?.detail || '安装失败')
    } finally {
      setInstalling(false)
    }
  }

  const uninstall = async (pkg) => {
    if (!window.confirm(`确认卸载「${pkg.title}」？包内 AI 员工将被停用`)) return
    await api.post('/marketplace/uninstall', {
      company_id: companyId, package_id: pkg.id,
    })
    load()
  }

  return (
    <div>
      <PageHeader title="Agent 行业商城" subtitle={`已安装 ${installed.length} 个技能包`} />
      <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 20 }}>
        一键为您的企业部署行业AI员工
      </p>

      {/* 行业筛选 */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 20, flexWrap: 'wrap' }}>
        {industries.map((ind) => {
          // ind 可能是字符串（旧）或 {code, name}（新）
          const code = typeof ind === 'string' ? ind : ind.code
          const name = typeof ind === 'string' ? ind : ind.name
          const isAll = code === 'all'
          const isGeneral = code === 'general'
          const label = isAll ? '🏷️ 全部' : isGeneral ? `🏷️ ${name}` : `🏭 ${name}`
          return (
            <button key={code}
              onClick={() => setActiveIndustry(code)}
              style={{
                padding: '8px 16px', borderRadius: 999, border: '1px solid var(--border-strong)',
                background: activeIndustry === code ? 'var(--primary)' : 'var(--bg-elevated)',
                color: activeIndustry === code ? '#fff' : 'var(--text)',
                fontSize: 13, cursor: 'pointer',
              }}>
              {label}
            </button>
          )
        })}
      </div>

      {/* 包卡片 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 }}>
        {packages.map((p) => {
          const remaining = seats ? seats.remaining : 0
          const noSeat = !p.installed && p.agent_count > remaining
          return (
          <div key={p.id} className="card" style={{ display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 }}>
              <div style={{
                width: 52, height: 52, borderRadius: 12, background: '#eff6ff',
                display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 26,
              }}>
                {p.icon}
              </div>
              <div>
                <div style={{ fontWeight: 700, fontSize: 15 }}>{p.title}</div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                  {p.agent_count} 个 AI 员工 · v{p.version}
                </div>
              </div>
              <span className="badge badge-blue" style={{ marginLeft: 'auto' }}>
                ¥{p.price.toLocaleString()}/月
              </span>
            </div>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', flex: 1, lineHeight: 1.6 }}>
              {p.description}
            </p>
            {!p.installed && seats && (
              <div style={{
                fontSize: 12, marginBottom: 8,
                color: noSeat ? '#b91c1c' : 'var(--text-muted)',
              }}>
                需占用 {p.agent_count} 个 AI 员工席位 · 当前剩余 {remaining}
                {noSeat && '（席位不足）'}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn btn-secondary btn-sm" style={{ flex: 1 }}
                onClick={() => setDetail(p)}>
                📋 详情
              </button>
              {p.installed ? (
                <button className="btn btn-danger btn-sm" style={{ flex: 1 }}
                  onClick={() => uninstall(p)}>
                  卸载
                </button>
              ) : noSeat ? (
                <button className="btn btn-primary btn-sm" style={{ flex: 1 }}
                  onClick={() => navigate('/billing')}>
                  升级底座套餐
                </button>
              ) : (
                <button className="btn btn-success btn-sm" style={{ flex: 1 }}
                  onClick={() => install(p)} disabled={installing}>
                  一键安装
                </button>
              )}
            </div>
          </div>
          )
        })}
        {packages.length === 0 && (
          <div className="card" style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无行业包</div>
        )}
      </div>

      {/* 详情弹窗 */}
      {detail && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.5)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }} onClick={() => setDetail(null)}>
          <div className="card" style={{ width: 520, maxHeight: '80vh', overflowY: 'auto' }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
              <div style={{ fontSize: 40 }}>{detail.icon}</div>
              <div>
                <h3 style={{ fontSize: 18 }}>{detail.title}</h3>
                <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>
                  {detail.name} · v{detail.version} · ¥{detail.price.toLocaleString()}/月
                </div>
              </div>
              <button className="btn btn-secondary btn-sm" style={{ marginLeft: 'auto' }}
                onClick={() => setDetail(null)}>✕</button>
            </div>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.7, marginBottom: 16 }}>
              {detail.description}
            </p>

            <h4 style={{ marginBottom: 8, fontSize: 14 }}>包含的 AI 员工</h4>
            {detail.agents.map((a, i) => (
              <div key={i} style={{
                padding: 10, background: 'var(--bg-subtle)', borderRadius: 8, marginBottom: 8,
              }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{a.name} · {a.label}</div>
                <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{a.description}</div>
              </div>
            ))}

            {/* tools 工具列表隐藏（保留后端 API 字段，不显示英文 API 标识） */}

            {detail.installed ? (
              <button className="btn btn-danger" style={{ width: '100%', marginTop: 16 }}
                onClick={() => { uninstall(detail); setDetail(null) }}>
                卸载此包
              </button>
            ) : (() => {
              const rem = seats ? seats.remaining : 0
              const noSeat = detail.agent_count > rem
              return noSeat ? (
                <button className="btn btn-primary" style={{ width: '100%', marginTop: 16 }}
                  onClick={() => { setDetail(null); navigate('/billing') }}>
                  升级底座套餐以安装（需 {detail.agent_count} 席位，剩余 {rem}）
                </button>
              ) : (
                <button className="btn btn-success" style={{ width: '100%', marginTop: 16 }}
                  onClick={() => install(detail)} disabled={installing}>
                  {installing ? '安装中...' : `🚀 安装到本企业（占用 ${detail.agent_count} 席位）`}
                </button>
              )
            })()}
          </div>
        </div>
      )}

      {/* 提示条 */}
      {msg && <div className="alert alert-success">{msg}</div>}
      {error && <div className="alert alert-error">{error}</div>}

      {/* 安装确认 modal（统一风格） */}
      {confirmInstall && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }} onClick={() => setConfirmInstall(null)}>
          <div className="card" style={{ width: 440, textAlign: 'center' }}
            onClick={(e) => e.stopPropagation()}>
            <div style={{ fontSize: 56, marginBottom: 8 }}>📦</div>
            <h2 style={{ fontSize: 18, marginBottom: 10 }}>确认安装技能包</h2>
            <p style={{ color: 'var(--text-muted)', fontSize: 14, lineHeight: 1.7, marginBottom: 8 }}>
              确认将 <b>「{confirmInstall.title}」</b> 安装到本企业？
            </p>
            <p style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 20 }}>
              安装后将创建 {confirmInstall.agent_count} 个 AI 员工并授权 {confirmInstall.tools?.length || 0} 项工具
            </p>
            <div style={{ display: 'flex', gap: 10 }}>
              <button className="btn btn-secondary" style={{ flex: 1 }}
                onClick={() => setConfirmInstall(null)} disabled={installing}>
                取消
              </button>
              <button className="btn btn-primary" style={{ flex: 1 }}
                onClick={doInstall} disabled={installing}>
                {installing ? '安装中...' : '🚀 确认安装'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
