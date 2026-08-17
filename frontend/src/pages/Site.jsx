import React, { useEffect, useState, useCallback, useRef } from 'react'
import { Link } from 'react-router-dom'
import api from '../api'
import { SiteLanguageProvider, useSiteLang, SITE_LANGS } from '../i18n/site'
import '../site.css'

function LanguageSwitcher() {
  const { lang, setLang } = useSiteLang()
  const active = SITE_LANGS.find((l) => l.code === lang) || SITE_LANGS[0]
  const [open, setOpen] = useState(false)
  const wrapRef = useRef(null)

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const onDown = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false)
    }
    const onKey = (e) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const choose = (code) => {
    setLang(code)
    setOpen(false)
  }

  return (
    <div className={`af-lang ${open ? 'open' : ''}`} ref={wrapRef}>
      <button
        type="button"
        className="af-lang-btn"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={active.name}
      >
        <img className="af-lang-flag" src={active.flagImg} alt={active.name} />
        <span className="af-lang-name">{active.name}</span>
        <span className="af-lang-caret">▾</span>
      </button>
      {open && (
        <ul className="af-lang-menu" role="listbox">
          {SITE_LANGS.map((l) => (
            <li
              key={l.code}
              role="option"
              aria-selected={l.code === lang}
              className={`af-lang-item ${l.code === lang ? 'active' : ''}`}
              onClick={() => choose(l.code)}
            >
              <img className="af-lang-flag" src={l.flagImg} alt={l.name} />
              <span className="af-lang-name">{l.name}</span>
              {l.code === lang && <span className="af-lang-check">✓</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ClientLogo({ c }) {
  if (c.logo) {
    return (
      <div className="af-client">
        <div className="af-client-inner">
          <img src={c.logo} alt={c.name} loading="lazy" />
        </div>
        <div className="c-name">{c.name}</div>
      </div>
    )
  }
  const initials = (c.name || '?').trim().slice(0, 2)
  return (
    <div className="af-client">
      <div className="af-client-inner">
        <div className="logo-ph">{initials}</div>
      </div>
      <div className="c-name">{c.name}</div>
    </div>
  )
}

function HeroCarousel({ hero, scrollTo }) {
  const { t } = useSiteLang()
  const slides = (hero.slides || []).filter((s) => s.image)
  const hasSlides = slides.length > 0
  const [idx, setIdx] = useState(0)

  const next = useCallback(() => {
    if (!hasSlides || slides.length <= 1) return
    setIdx((i) => (i + 1) % slides.length)
  }, [hasSlides, slides.length])

  useEffect(() => {
    if (!hasSlides || slides.length <= 1) return
    const t = setInterval(next, 5000)
    return () => clearInterval(t)
  }, [hasSlides, slides.length, next])

  const slide = hasSlides ? slides[idx] : null
  const title = slide?.title || hero.title || 'AI Factory'
  const subtitle = slide?.subtitle || hero.subtitle || ''
  const ctaText = slide?.cta_text || hero.cta_text || t('hero.cta_free')
  const ctaUrl = slide?.cta_url || hero.cta_url || '/login'

  return (
    <section className="af-hero">
      {hasSlides && (
        <div className="af-hero-slides">
          {slides.map((s, i) => (
            <div
              key={i}
              className={`af-hero-slide ${i === idx ? 'active' : ''}`}
            >
              <img
                src={s.image}
                alt={s.title || `slide-${i + 1}`}
                className="af-hero-slide-img"
                loading={i === 0 ? 'eager' : 'lazy'}
              />
            </div>
          ))}
          <div className="af-hero-overlay" />
        </div>
      )}
      <div className="af-container af-hero-inner">
        <h1>{title}</h1>
        {subtitle && <div className="af-sub">{subtitle}</div>}
        {hero.description && <p className="af-desc">{hero.description}</p>}
        <div className="af-hero-cta">
          <Link to={ctaUrl} className="af-btn af-btn-primary">{ctaText} →</Link>
          <a className="af-btn af-btn-ghost" onClick={() => scrollTo('features')}>{t('hero.learn_more')}</a>
        </div>
        {hasSlides && slides.length > 1 && (
          <div className="af-hero-dots">
            {slides.map((_, i) => (
              <button
                key={i}
                className={i === idx ? 'active' : ''}
                onClick={() => setIdx(i)}
                aria-label={t('hero.slide', { n: i + 1 })}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  )
}

function SiteInner() {
  const { t, lang } = useSiteLang()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [navOpen, setNavOpen] = useState(false)
  const [socialOpen, setSocialOpen] = useState(false)

  useEffect(() => {
    api.get('/cms/content?lang=' + encodeURIComponent(lang || 'zh'))
      .then((res) => setData(res.data))
      .catch((e) => setError(e.response?.data?.detail || t('error')))
  }, [t, lang])

  if (error) {
    return <div className="af-site"><div className="af-loading">{error}</div></div>
  }
  if (!data) {
    return <div className="af-site"><div className="af-loading">{t('loading')}</div></div>
  }

  const hero = data.hero || {}
  const about = data.about || {}
  const features = data.features || []
  const customers = data.customers || []
  const contact = data.contact || {}
  const pricing = data.pricing || {}
  const social = data.social || {}
  const hasSocial = !!(social.hotline || social.qq || social.wechat_qr)

  const scrollTo = (id) => {
    setNavOpen(false)
    const el = document.getElementById(id)
    if (el) el.scrollIntoView({ behavior: 'smooth' })
  }

  const stats = about.stats || []

  return (
    <div className="af-site">
      {/* 顶部导航 */}
      <header className="af-header">
        <div className="af-container af-header-inner">
          <div className="af-logo">
            <span className="af-logo-mark">🏭</span>
            <span>AI Factory</span>
          </div>
          <nav className={`af-nav ${navOpen ? 'open' : ''}`}>
            <a onClick={() => scrollTo('about')}>{t('nav.about')}</a>
            <a onClick={() => scrollTo('features')}>{t('nav.features')}</a>
            <a onClick={() => scrollTo('pricing')}>{t('nav.pricing')}</a>
            <a onClick={() => scrollTo('clients')}>{t('nav.clients')}</a>
            <a onClick={() => scrollTo('contact')}>{t('nav.contact')}</a>
          </nav>
          <div className="af-header-actions">
            <LanguageSwitcher />
            <Link to="/login" className="af-btn af-btn-primary af-btn-sm">{t('nav.login')}</Link>
          </div>
          <button className="af-nav-toggle" onClick={() => setNavOpen(!navOpen)}>☰</button>
        </div>
      </header>

      {/* 首屏 Hero（轮播图） */}
      <HeroCarousel hero={hero} scrollTo={scrollTo} />

      {/* 关于 AI Factory */}
      <section className="af-section" id="about">
        <div className="af-container">
          <div className="af-about-grid">
            <div className="af-about-text">
              <div className="af-section-head" style={{ textAlign: 'left', margin: '0 0 24px' }}>
                <h2 style={{ fontSize: 30 }}>{about.title || t('about.fallback_title')}</h2>
              </div>
              {about.description && <p>{about.description}</p>}
            </div>
            <div className="af-about-card">
              <h3>{t('about.core_caps')}</h3>
              <ul>
                <li><span className="dot">1</span>{t('about.cap1')}</li>
                <li><span className="dot">2</span>{t('about.cap2')}</li>
                <li><span className="dot">3</span>{t('about.cap3')}</li>
                <li><span className="dot">4</span>{t('about.cap4')}</li>
                <li><span className="dot">5</span>{t('about.cap5')}</li>
              </ul>
            </div>
          </div>

          {/* 数据指标 */}
          {stats.length > 0 && (
            <div className="af-stats" style={{ marginTop: 48 }}>
              {stats.map((s, i) => (
                <div className="af-stat" key={i}>
                  <div className="v">{s.value}</div>
                  <div className="l">{s.label}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>

      {/* 功能介绍 */}
      <section className="af-section alt" id="features">
        <div className="af-container">
          <div className="af-section-head">
            <h2>{t('features.title')}</h2>
            <p>{t('features.subtitle')}</p>
            <p className="af-section-note">{t('features.note')}</p>
          </div>
          <div className="af-features">
            {features.map((f, i) => (
              <div className="af-feature" key={i}>
                <div className="af-fnum">{String(i + 1).padStart(2, '0')}</div>
                <div className="f-icon">{f.icon || '🧩'}</div>
                <h3>{f.title}</h3>
                <p>{f.description}</p>
              </div>
            ))}
            {features.length === 0 && (
              <div style={{ gridColumn: '1/-1', textAlign: 'center', color: '#64748b' }}>
                {t('features.empty')}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* 客户 LOGO 展示 */}
      <section className="af-section" id="clients">
        <div className="af-container">
          <div className="af-section-head">
            <h2>{t('clients.title')}</h2>
            <p>{t('clients.subtitle')}</p>
          </div>
          <div className="af-clients">
            {customers.map((c, i) => <ClientLogo key={i} c={c} />)}
            {customers.length === 0 && (
              <div style={{ gridColumn: '1/-1', textAlign: 'center', color: '#64748b' }}>
                {t('clients.empty')}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* 套餐价格 */}
      <section className="af-section alt" id="pricing">
        <div className="af-container">
          <div className="af-section-head">
            <h2>{pricing.title || t('pricing.fallback_title')}</h2>
            {pricing.subtitle && <p>{pricing.subtitle}</p>}
          </div>
          <div className="af-plans">
            {(pricing.plans || []).map((p, i) => (
              <div className={`af-plan ${p.featured ? 'featured' : ''}`} key={i}>
                {p.tag && <div className="af-plan-tag">{p.tag}</div>}
                <h3 className="af-plan-name">{p.name}</h3>
                {p.description && <p className="af-plan-desc">{p.description}</p>}
                <div className="af-plan-price">
                  <span className="pp-num">{p.price}</span>
                  {p.period && <span className="pp-period">{p.period}</span>}
                </div>
                <ul className="af-plan-features">
                  {(p.features || []).map((ft, j) => (
                    <li key={j}><span className="ck">✓</span>{ft}</li>
                  ))}
                </ul>
                <Link to={p.cta_url || '/login'} className={`af-btn ${p.featured ? 'af-btn-primary' : 'af-btn-ghost'} af-plan-cta`}>
                  {p.cta_text || t('pricing.cta')}
                </Link>
              </div>
            ))}
            {(!pricing.plans || pricing.plans.length === 0) && (
              <div style={{ gridColumn: '1/-1', textAlign: 'center', color: '#64748b' }}>
                {t('pricing.empty')}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* 联系 CTA 大块 */}
      <section className="af-cta" id="contact">
        <div className="af-container af-cta-inner">
          <div className="af-cta-text">
            <h2>{t('contact.title')}</h2>
            <p>{t('contact.subtitle')}</p>
            <div className="af-cta-actions">
              <Link to="/login" className="af-btn af-btn-light">{t('contact.free_trial')} →</Link>
              {contact.phone && (
                <a className="af-btn af-btn-ghost-light" href={`tel:${contact.phone}`}>📞 {contact.phone}</a>
              )}
            </div>
          </div>
          <div className="af-cta-card">
            <div className="ci-row">
              <span className="ci-ic">🏢</span>
              <div>
                <div className="ci-label">{t('contact.company')}</div>
                <div className="ci-value">{contact.company_name || 'AI Factory'}</div>
              </div>
            </div>
            {contact.address && (
              <div className="ci-row">
                <span className="ci-ic">📍</span>
                <div>
                  <div className="ci-label">{t('contact.address')}</div>
                  <div className="ci-value">{contact.address}</div>
                </div>
              </div>
            )}
            {contact.email && (
              <div className="ci-row">
                <span className="ci-ic">✉️</span>
                <div>
                  <div className="ci-label">{t('contact.email')}</div>
                  <div className="ci-value">{contact.email}</div>
                </div>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* 页脚 */}
      <footer className="af-footer">
        <div className="af-container af-footer-inner">
          <div>
            <div className="f-brand">🏭 AI Factory</div>
            <div className="f-desc">{t('footer.desc')}</div>
            <div className="f-social">
              {social.wechat_qr && (
                <a className="f-sic" title={t('social.wechat')} onClick={() => setSocialOpen(true)}>💬</a>
              )}
              {social.qq && (
                <a className="f-sic" title={`${t('social.qq')} ${social.qq}`} href={`tencent://message/?uin=${social.qq}`}>🐧</a>
              )}
              {social.hotline && (
                <a className="f-sic" title={`${t('social.hotline')} ${social.hotline}`} href={`tel:${social.hotline}`}>📞</a>
              )}
              <a className="f-sic" title={t('contact.email')} href={`mailto:${contact.email || ''}`}>✉️</a>
            </div>
          </div>
          <div className="f-cols">
            <div className="f-col">
              <h4>{t('footer.contact')}</h4>
              {contact.phone && <a href={`tel:${contact.phone}`}>{contact.phone}</a>}
              {contact.email && <a href={`mailto:${contact.email}`}>{contact.email}</a>}
              {contact.address && <a>{contact.address}</a>}
            </div>
          </div>
        </div>
        <div className="af-container">
          <div className="f-bottom">
            {t('footer.copyright').replace('{year}', new Date().getFullYear())}
          </div>
        </div>
      </footer>

      {/* 右侧悬浮客服按钮 */}
      {hasSocial && (
        <div className="af-social">
          {socialOpen && (
            <div className="af-social-panel">
              <div className="af-sp-title">{t('social.title')}</div>
              {social.hotline && (
                <a className="af-sp-item" href={`tel:${social.hotline}`}>
                  <span className="af-sp-ic">📞</span>
                  <div>
                    <div className="af-sp-label">{t('social.hotline')}</div>
                    <div className="af-sp-value">{social.hotline}</div>
                  </div>
                </a>
              )}
              {social.qq && (
                <div className="af-sp-item">
                  <span className="af-sp-ic">💬</span>
                  <div>
                    <div className="af-sp-label">{t('social.qq')}</div>
                    <div className="af-sp-value">{social.qq}</div>
                  </div>
                </div>
              )}
              {social.wechat_qr && (
                <div className="af-sp-item af-sp-wechat">
                  <span className="af-sp-ic">🟢</span>
                  <div>
                    <div className="af-sp-label">{t('social.wechat')}</div>
                    <img src={social.wechat_qr} alt={t('social.wechat')} className="af-sp-qr" />
                  </div>
                </div>
              )}
            </div>
          )}
          <button
            className="af-social-toggle"
            onClick={() => setSocialOpen((o) => !o)}
            aria-label={socialOpen ? t('social.close') : t('social.open')}
            title={socialOpen ? t('social.close') : t('social.open')}
          >
            {socialOpen ? '✕' : '🎧'}
          </button>
        </div>
      )}
    </div>
  )
}

export default function Site() {
  return (
    <SiteLanguageProvider>
      <SiteInner />
    </SiteLanguageProvider>
  )
}
