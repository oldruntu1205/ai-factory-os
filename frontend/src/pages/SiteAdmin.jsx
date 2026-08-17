import React, { useEffect, useState } from 'react'
import api from '../api'
import { SITE_LANGS } from '../i18n/site'

const emptyContent = () => ({
  hero: { title: '', subtitle: '', description: '', cta_text: '免费体验', cta_url: '/login', slides: [] },
  about: { title: '关于 AI Factory', description: '', stats: [] },
  features: [],
  customers: [],
  contact: { company_name: '', address: '', phone: '', email: '', map_url: '' },
  pricing: { title: '套餐价格', subtitle: '', plans: [] },
  social: { hotline: '', qq: '', wechat_qr: '' },
})

// 深合并：over 覆盖 base（dict 递归，list/scalar 直接覆盖）
const _mergeContent = (base, over) => {
  if (over == null) return JSON.parse(JSON.stringify(base))
  if (base && typeof base === 'object' && !Array.isArray(base) &&
      over && typeof over === 'object' && !Array.isArray(over)) {
    const out = { ...base }
    for (const k of Object.keys(over)) out[k] = _mergeContent(out[k], over[k])
    return out
  }
  return over
}

// 深 diff：返回 over 相对 base 的差异（只存不同部分），用于保存某语言翻译覆盖
const _diffContent = (base, over) => {
  if (base && typeof base === 'object' && !Array.isArray(base) &&
      over && typeof over === 'object' && !Array.isArray(over)) {
    const out = {}
    for (const k of Object.keys(over)) {
      const d = _diffContent(base[k], over[k])
      if (d !== undefined) out[k] = d
    }
    return Object.keys(out).length ? out : undefined
  }
  if (Array.isArray(base) && Array.isArray(over)) {
    return JSON.stringify(base) !== JSON.stringify(over) ? over : undefined
  }
  return base === over ? undefined : over
}

export default function SiteAdmin() {
  const [base, setBase] = useState(emptyContent())        // 默认语言(zh)内容
  const [translations, setTranslations] = useState({})   // 各语言翻译覆盖 {"en": {...}, ...}
  const [lang, setLang] = useState('zh')                  // 当前编辑的语言
  const [content, setContent] = useState(emptyContent())  // 当前编辑的（已按语言合并）
  const [loaded, setLoaded] = useState(false)
  const [msg, setMsg] = useState('')
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(null) // 正在上传的字段 key

  const load = async () => {
    try {
      const { data } = await api.get('/cms/content/admin')
      const c = { ...emptyContent(), ...(data.content || {}) }
      const tr = data.translations || {}
      setBase(c)
      setTranslations(tr)
      setLang('zh')
      setContent(c)
    } catch (e) {
      setMsg('❌ 加载失败：' + (e.response?.data?.detail || e.message))
    } finally {
      setLoaded(true)
    }
  }

  useEffect(() => { load() }, [])

  const switchLang = (L) => {
    setLang(L)
    setContent(L === 'zh' ? base : _mergeContent(base, translations[L] || {}))
    setMsg('')
  }

  const setPath = (obj, path, value) => {
    const next = { ...obj }
    let cur = next
    for (let i = 0; i < path.length - 1; i++) cur = cur[path[i]]
    cur[path[path.length - 1]] = value
    return next
  }
  const update = (path, value) => {
    setContent((c) => setPath(c, path, value))
    setMsg('')
  }

  const uploadImage = async (file) => {
    const form = new FormData()
    form.append('file', file)
    const { data } = await api.post('/cms/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return data.url
  }

  // 通用上传（hero 背景 / 单个客户 logo）
  const handleUpload = async (file, onUrl) => {
    if (!file) return
    setUploading(true)
    try {
      const url = await uploadImage(file)
      onUrl(url)
      setMsg('✅ 图片已上传')
    } catch (e) {
      setMsg('❌ 上传失败：' + (e.response?.data?.detail || e.message))
    } finally {
      setUploading(false)
    }
  }

  const save = async () => {
    setSaving(true)
    setMsg('')
    try {
      if (lang === 'zh') {
        await api.put('/cms/content', content)
      } else {
        await api.put(`/cms/content/${lang}`, content)
        // 本地同步该语言覆盖，便于来回切换不丢编辑
        setTranslations((tr) => ({ ...tr, [lang]: _diffContent(base, content) || {} }))
      }
      setMsg('✅ 官网内容已保存，前台已实时更新')
    } catch (e) {
      setMsg('❌ 保存失败：' + (e.response?.data?.detail || e.message))
    } finally {
      setSaving(false)
    }
  }

  // ---- 列表操作辅助 ----
  const addFeature = () => update(['features'], [...content.features, { icon: '🧩', title: '', description: '' }])
  const updFeature = (i, key, v) => {
    const fs = content.features.map((f, idx) => idx === i ? { ...f, [key]: v } : f)
    update(['features'], fs)
  }
  const delFeature = (i) => update(['features'], content.features.filter((_, idx) => idx !== i))

  const addCustomer = () => update(['customers'], [...content.customers, { name: '', logo: '' }])
  const updCustomer = (i, key, v) => {
    const cs = content.customers.map((c, idx) => idx === i ? { ...c, [key]: v } : c)
    update(['customers'], cs)
  }
  const delCustomer = (i) => update(['customers'], content.customers.filter((_, idx) => idx !== i))

  const addStat = () => update(['about', 'stats'], [...(content.about.stats || []), { label: '', value: '' }])
  const updStat = (i, key, v) => {
    const ss = (content.about.stats || []).map((s, idx) => idx === i ? { ...s, [key]: v } : s)
    update(['about', 'stats'], ss)
  }
  const delStat = (i) => update(['about', 'stats'], (content.about.stats || []).filter((_, idx) => idx !== i))

  // ---- 套餐价格 ----
  const addPlan = () => update(['pricing', 'plans'], [...(content.pricing.plans || []),
    { name: '', tag: '', price: '', period: '/月', description: '', features: [''], cta_text: '立即咨询', cta_url: '/login', featured: false }])
  const updPlan = (i, key, v) => {
    const ps = (content.pricing.plans || []).map((p, idx) => idx === i ? { ...p, [key]: v } : p)
    update(['pricing', 'plans'], ps)
  }
  const delPlan = (i) => update(['pricing', 'plans'], (content.pricing.plans || []).filter((_, idx) => idx !== i))
  const addPlanFeature = (i) => {
    const ps = (content.pricing.plans || []).map((p, idx) => idx === i ? { ...p, features: [...(p.features || []), ''] } : p)
    update(['pricing', 'plans'], ps)
  }
  const updPlanFeature = (i, j, v) => {
    const ps = (content.pricing.plans || []).map((p, idx) => {
      if (idx !== i) return p
      const fs = (p.features || []).map((f, k) => k === j ? v : f)
      return { ...p, features: fs }
    })
    update(['pricing', 'plans'], ps)
  }
  const delPlanFeature = (i, j) => {
    const ps = (content.pricing.plans || []).map((p, idx) => {
      if (idx !== i) return p
      return { ...p, features: (p.features || []).filter((_, k) => k !== j) }
    })
    update(['pricing', 'plans'], ps)
  }

  if (!loaded) {
    return <div style={{ padding: 40, color: 'var(--text-muted)' }}>加载官网内容中…</div>
  }

  const fieldStyle = {
    width: '100%', padding: '9px 12px', border: '1px solid var(--border-strong)',
    borderRadius: 8, fontSize: 14, background: 'var(--bg-elevated)', color: 'var(--text)',
    outline: 'none',
  }
  const labelStyle = { display: 'block', fontSize: 13, fontWeight: 600, margin: '10px 0 6px', color: 'var(--text-muted)' }
  const sectionCard = (title, children) => (
    <div className="card" style={{ marginBottom: 20 }}>
      <h3 style={{ marginBottom: 14, fontSize: 16 }}>{title}</h3>
      {children}
    </div>
  )

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 4 }}>
          <h1 style={{ fontSize: 22, marginBottom: 0 }}>🌐 官网内容管理</h1>
          <div style={{ display: 'flex', gap: 8 }}>
            <a className="btn btn-secondary btn-sm" href="/site" target="_blank" rel="noreferrer">🔎 预览前台</a>
            <button className="btn btn-primary btn-sm" onClick={save} disabled={saving}>
              {saving ? '保存中…' : '💾 保存全部'}
            </button>
          </div>
        </div>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: 0 }}>
          编辑前台「AI Factory 介绍页」内容，保存后前台实时生效。
        </p>
      </div>

      {/* 多语言翻译切换 Tab */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', margin: '12px 0 4px' }}>
        {SITE_LANGS.map((l) => (
          <button
            key={l.code}
            className={`btn btn-sm ${lang === l.code ? 'btn-primary' : 'btn-secondary'}`}
            onClick={() => switchLang(l.code)}
          >
            {l.name}{l.code === 'zh' ? '（默认）' : ''}
          </button>
        ))}
      </div>
      {lang !== 'zh' && (
        <p style={{
          fontSize: 13, color: '#b45309', margin: '8px 0 0',
          background: '#fffbeb', border: '1px solid #fde68a', padding: '8px 12px', borderRadius: 8,
        }}>
          当前为「{SITE_LANGS.find((l) => l.code === lang)?.name}」翻译模式：仅填写与简体中文不同的字段，
          留空项将自动回退中文默认内容；保存时只记录差异。
        </p>
      )}

      {msg && (
        <div className={msg.startsWith('✅') ? 'alert alert-success' : 'alert alert-error'} style={{ marginBottom: 16 }}>
          {msg}
        </div>
      )}

      {/* 首屏 Hero */}
      {sectionCard('首屏（Hero）', (
        <>
          <label style={labelStyle}>主标题</label>
          <input style={fieldStyle} value={content.hero.title}
            onChange={(e) => update(['hero', 'title'], e.target.value)} />
          <label style={labelStyle}>副标题</label>
          <input style={fieldStyle} value={content.hero.subtitle}
            onChange={(e) => update(['hero', 'subtitle'], e.target.value)} />
          <label style={labelStyle}>简介</label>
          <textarea style={fieldStyle} rows={3} value={content.hero.description}
            onChange={(e) => update(['hero', 'description'], e.target.value)} />
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ flex: 1, minWidth: 160 }}>
              <label style={labelStyle}>按钮文字</label>
              <input style={fieldStyle} value={content.hero.cta_text}
                onChange={(e) => update(['hero', 'cta_text'], e.target.value)} />
            </div>
            <div style={{ flex: 1, minWidth: 160 }}>
              <label style={labelStyle}>按钮链接</label>
              <input style={fieldStyle} value={content.hero.cta_url}
                onChange={(e) => update(['hero', 'cta_url'], e.target.value)} />
            </div>
          </div>
          <label style={labelStyle}>首屏轮播图（可选，建议 2~5 张，单张尺寸 1920×1080）</label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {(content.hero.slides || []).map((s, i) => (
              <div key={i} style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 12 }}>
                <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
                  {s.image ? <img src={s.image} alt={`slide-${i}`} style={{ height: 56, width: 100, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--border)' }} />
                    : <div style={{ height: 56, width: 80, borderRadius: 6, border: '1px dashed var(--border-strong)', display: 'grid', placeItems: 'center', color: 'var(--text-muted)', fontSize: 12 }}>未上传</div>}
                  <input type="file" accept="image/*" disabled={uploading}
                    onChange={(e) => handleUpload(e.target.files[0], (url) => {
                      const slides = [...(content.hero.slides || [])]
                      slides[i] = { ...slides[i], image: url }
                      update(['hero', 'slides'], slides)
                    })} />
                  <button className="btn btn-secondary btn-sm" onClick={() => {
                    const slides = (content.hero.slides || []).filter((_, idx) => idx !== i)
                    update(['hero', 'slides'], slides)
                  }}>移除</button>
                </div>
                <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                  <input style={{ ...fieldStyle, flex: 1, minWidth: 140 }} placeholder="单张标题（可选，为空用顶层标题）" value={s.title || ''}
                    onChange={(e) => {
                      const slides = [...(content.hero.slides || [])]
                      slides[i] = { ...slides[i], title: e.target.value }
                      update(['hero', 'slides'], slides)
                    }} />
                  <input style={{ ...fieldStyle, flex: 1, minWidth: 140 }} placeholder="单张副标题（可选）" value={s.subtitle || ''}
                    onChange={(e) => {
                      const slides = [...(content.hero.slides || [])]
                      slides[i] = { ...slides[i], subtitle: e.target.value }
                      update(['hero', 'slides'], slides)
                    }} />
                </div>
              </div>
            ))}
            <div>
              <button className="btn btn-secondary btn-sm" onClick={() => update(['hero', 'slides'], [...(content.hero.slides || []), { image: '', title: '', subtitle: '', cta_text: '', cta_url: '' }])}>
                + 添加轮播图
              </button>
            </div>
          </div>
        </>
      ))}

      {/* 关于 AI Factory */}
      {sectionCard('关于 AI Factory', (
        <>
          <label style={labelStyle}>标题</label>
          <input style={fieldStyle} value={content.about.title}
            onChange={(e) => update(['about', 'title'], e.target.value)} />
          <label style={labelStyle}>正文（支持换行）</label>
          <textarea style={fieldStyle} rows={6} value={content.about.description}
            onChange={(e) => update(['about', 'description'], e.target.value)} />

          <label style={labelStyle}>数据指标</label>
          {(content.about.stats || []).map((s, i) => (
            <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
              <input style={{ ...fieldStyle, flex: 1 }} placeholder="数值，如 500+" value={s.value}
                onChange={(e) => updStat(i, 'value', e.target.value)} />
              <input style={{ ...fieldStyle, flex: 2 }} placeholder="标签，如 服务制造企业" value={s.label}
                onChange={(e) => updStat(i, 'label', e.target.value)} />
              <button className="btn btn-danger btn-sm" onClick={() => delStat(i)}>删</button>
            </div>
          ))}
          <button className="btn btn-secondary btn-sm" onClick={addStat}>＋ 添加指标</button>
        </>
      ))}

      {/* 功能介绍 */}
      {sectionCard('功能介绍', (
        <>
          {content.features.map((f, i) => (
            <div key={i} style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 14, marginBottom: 12 }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
                <input style={{ width: 64, padding: '9px', border: '1px solid var(--border-strong)', borderRadius: 8, fontSize: 18, textAlign: 'center', background: 'var(--bg-elevated)', color: 'var(--text)' }}
                  value={f.icon} onChange={(e) => updFeature(i, 'icon', e.target.value)} />
                <input style={{ ...fieldStyle, flex: 1 }} placeholder="功能标题" value={f.title}
                  onChange={(e) => updFeature(i, 'title', e.target.value)} />
                <button className="btn btn-danger btn-sm" onClick={() => delFeature(i)}>删</button>
              </div>
              <textarea style={fieldStyle} rows={2} placeholder="功能描述" value={f.description}
                onChange={(e) => updFeature(i, 'description', e.target.value)} />
            </div>
          ))}
          <button className="btn btn-secondary btn-sm" onClick={addFeature}>＋ 添加功能</button>
        </>
      ))}

      {/* 客户 LOGO 展示 */}
      {sectionCard('客户 LOGO 展示', (
        <>
          {content.customers.map((c, i) => (
            <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 12, border: '1px solid var(--border)', borderRadius: 10, padding: 10 }}>
              <div style={{ width: 56, height: 56, borderRadius: 10, border: '1px solid var(--border)', display: 'grid', placeItems: 'center', overflow: 'hidden', background: 'var(--bg-subtle)' }}>
                {c.logo ? <img src={c.logo} alt={c.name} style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }} />
                  : <span style={{ fontSize: 18, color: 'var(--text-muted)' }}>{(c.name || '?').slice(0, 2)}</span>}
              </div>
              <input style={{ ...fieldStyle, flex: 1 }} placeholder="客户名称" value={c.name}
                onChange={(e) => updCustomer(i, 'name', e.target.value)} />
              <input type="file" accept="image/*" disabled={uploading}
                onChange={(e) => handleUpload(e.target.files[0], (url) => updCustomer(i, 'logo', url))} />
              <button className="btn btn-danger btn-sm" onClick={() => delCustomer(i)}>删</button>
            </div>
          ))}
          <button className="btn btn-secondary btn-sm" onClick={addCustomer}>＋ 添加客户</button>
        </>
      ))}

      {/* 套餐价格 */}
      {sectionCard('套餐价格', (
        <>
          <label style={labelStyle}>板块标题</label>
          <input style={fieldStyle} value={content.pricing.title || ''}
            onChange={(e) => update(['pricing', 'title'], e.target.value)} />
          <label style={labelStyle}>板块副标题</label>
          <textarea style={fieldStyle} rows={2} value={content.pricing.subtitle || ''}
            onChange={(e) => update(['pricing', 'subtitle'], e.target.value)} />

          <label style={labelStyle}>价格方案</label>
          {(content.pricing.plans || []).map((p, i) => (
            <div key={i} style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 14, marginBottom: 12 }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10, flexWrap: 'wrap' }}>
                <input style={{ ...fieldStyle, flex: 1, minWidth: 120 }} placeholder="方案名称，如 专业版"
                  value={p.name} onChange={(e) => updPlan(i, 'name', e.target.value)} />
                <input style={{ ...fieldStyle, width: 110 }} placeholder="角标，如 推荐"
                  value={p.tag} onChange={(e) => updPlan(i, 'tag', e.target.value)} />
                <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: 'var(--text-muted)' }}>
                  <input type="checkbox" checked={!!p.featured}
                    onChange={(e) => updPlan(i, 'featured', e.target.checked)} /> 高亮推荐
                </label>
                <button className="btn btn-danger btn-sm" onClick={() => delPlan(i)}>删方案</button>
              </div>
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                <div style={{ flex: 1, minWidth: 120 }}>
                  <label style={labelStyle}>价格（如 299 / 价格面议）</label>
                  <input style={fieldStyle} value={p.price} onChange={(e) => updPlan(i, 'price', e.target.value)} />
                </div>
                <div style={{ flex: 1, minWidth: 120 }}>
                  <label style={labelStyle}>计费周期（如 /月 /年）</label>
                  <input style={fieldStyle} value={p.period} onChange={(e) => updPlan(i, 'period', e.target.value)} />
                </div>
              </div>
              <label style={labelStyle}>方案简介</label>
              <input style={fieldStyle} value={p.description} onChange={(e) => updPlan(i, 'description', e.target.value)} />
              <label style={labelStyle}>功能点（每行一条）</label>
              {(p.features || []).map((ft, j) => (
                <div key={j} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                  <input style={{ ...fieldStyle, flex: 1 }} placeholder="功能点描述" value={ft}
                    onChange={(e) => updPlanFeature(i, j, e.target.value)} />
                  <button className="btn btn-danger btn-sm" onClick={() => delPlanFeature(i, j)}>删</button>
                </div>
              ))}
              <button className="btn btn-secondary btn-sm" onClick={() => addPlanFeature(i)}>＋ 添加功能点</button>
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 10 }}>
                <div style={{ flex: 1, minWidth: 120 }}>
                  <label style={labelStyle}>按钮文字</label>
                  <input style={fieldStyle} value={p.cta_text} onChange={(e) => updPlan(i, 'cta_text', e.target.value)} />
                </div>
                <div style={{ flex: 1, minWidth: 120 }}>
                  <label style={labelStyle}>按钮链接</label>
                  <input style={fieldStyle} value={p.cta_url} onChange={(e) => updPlan(i, 'cta_url', e.target.value)} />
                </div>
              </div>
            </div>
          ))}
          <button className="btn btn-secondary btn-sm" onClick={addPlan}>＋ 添加方案</button>
        </>
      ))}

      {/* 联系我们 */}
      {sectionCard('联系我们', (
        <>
          <label style={labelStyle}>公司名称</label>
          <input style={fieldStyle} value={content.contact.company_name}
            onChange={(e) => update(['contact', 'company_name'], e.target.value)} />
          <label style={labelStyle}>公司地址</label>
          <input style={fieldStyle} value={content.contact.address}
            onChange={(e) => update(['contact', 'address'], e.target.value)} />
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ flex: 1, minWidth: 160 }}>
              <label style={labelStyle}>联系电话</label>
              <input style={fieldStyle} value={content.contact.phone}
                onChange={(e) => update(['contact', 'phone'], e.target.value)} />
            </div>
            <div style={{ flex: 1, minWidth: 160 }}>
              <label style={labelStyle}>电子邮箱</label>
              <input style={fieldStyle} value={content.contact.email}
                onChange={(e) => update(['contact', 'email'], e.target.value)} />
            </div>
          </div>
          <label style={labelStyle}>地图嵌入地址（iframe src，可选）</label>
          <input style={fieldStyle} placeholder="https://map.xxx.com/embed?..." value={content.contact.map_url}
            onChange={(e) => update(['contact', 'map_url'], e.target.value)} />
        </>
      ))}

      {/* 在线客服（悬浮按钮） */}
      {sectionCard('在线客服（右侧悬浮按钮）', (
        <>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: '0 0 12px' }}>
            设置后，前台页面右侧会显示悬浮客服按钮，点击展开显示以下联系方式。三项均为空则不显示按钮。
          </p>
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
            <div style={{ flex: 1, minWidth: 160 }}>
              <label style={labelStyle}>热线电话</label>
              <input style={fieldStyle} value={content.social.hotline}
                onChange={(e) => update(['social', 'hotline'], e.target.value)} />
            </div>
            <div style={{ flex: 1, minWidth: 160 }}>
              <label style={labelStyle}>客服 QQ</label>
              <input style={fieldStyle} value={content.social.qq}
                onChange={(e) => update(['social', 'qq'], e.target.value)} />
            </div>
          </div>
          <label style={labelStyle}>微信二维码（上传图片，点击可放大）</label>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            {content.social.wechat_qr ? (
              <img src={content.social.wechat_qr} alt="微信二维码"
                style={{ height: 72, borderRadius: 8, border: '1px solid var(--border)' }} />
            ) : (
              <div style={{ height: 72, width: 72, borderRadius: 8, border: '1px dashed var(--border-strong)', display: 'grid', placeItems: 'center', color: 'var(--text-muted)', fontSize: 12 }}>未上传</div>
            )}
            <input type="file" accept="image/*" disabled={uploading}
              onChange={(e) => handleUpload(e.target.files[0], (url) => update(['social', 'wechat_qr'], url))} />
            {content.social.wechat_qr && (
              <button className="btn btn-secondary btn-sm" onClick={() => update(['social', 'wechat_qr'], '')}>移除</button>
            )}
          </div>
        </>
      ))}

      <div style={{ margin: '8px 0 40px' }}>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? '保存中…' : '💾 保存全部修改'}
        </button>
      </div>
    </div>
  )
}
