import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api'

// 后台全局悬浮在线客服按钮
// 数据来源与官网 Site.jsx 一致（/cms/content 的 social / contact），
// 保证客户后台与官网展示的客服渠道一致，平台方只需在 官网内容 中维护。
export default function FloatingSupport() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [social, setSocial] = useState({})
  const [contact, setContact] = useState({})

  useEffect(() => {
    api.get('/cms/content?lang=zh')
      .then((res) => {
        setSocial(res.data?.social || {})
        setContact(res.data?.contact || {})
      })
      .catch(() => { /* 未配置时隐藏按钮 */ })
  }, [])

  const hasChannels = !!(social.hotline || social.qq || social.wechat_qr ||
    contact.email || contact.phone)

  if (!hasChannels) return null

  const btnStyle = {
    width: 52, height: 52, borderRadius: '50%', border: 'none', cursor: 'pointer',
    background: 'var(--primary)', color: '#fff', fontSize: 22, boxShadow: '0 6px 18px rgba(37,99,235,0.35)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    transition: 'transform 0.2s',
  }

  return (
    <div style={{ position: 'fixed', right: 22, bottom: 22, zIndex: 999, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 10 }}>
      {open && (
        <div className="card" style={{
          width: 300, padding: 16, boxShadow: '0 12px 32px rgba(15,23,42,0.18)',
          borderRadius: 12, overflow: 'hidden',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <div style={{
              width: 38, height: 38, borderRadius: '50%', background: 'var(--primary-soft)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18,
            }}>🎧</div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 14 }}>在线客服</div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>工作日 9:00-18:00 在线</div>
            </div>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {social.hotline && (
              <a className="btn btn-secondary" href={`tel:${social.hotline}`}
                style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'flex-start', textDecoration: 'none' }}>
                📞 客服热线：{social.hotline}
              </a>
            )}
            {social.qq && (
              <a className="btn btn-secondary" href={`tencent://message/?uin=${social.qq}`}
                style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'flex-start', textDecoration: 'none' }}>
                💬 QQ 客服：{social.qq}
              </a>
            )}
            {contact.email && (
              <a className="btn btn-secondary" href={`mailto:${contact.email}`}
                style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'flex-start', textDecoration: 'none' }}>
                ✉️ 邮箱：{contact.email}
              </a>
            )}
            {social.wechat_qr && (
              <div className="btn btn-secondary" style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'default' }}>
                <span>🟢 企业微信</span>
                <img src={social.wechat_qr} alt="企业微信二维码"
                  style={{ width: 72, height: 72, borderRadius: 6, marginLeft: 'auto' }} />
              </div>
            )}
            <button className="btn btn-primary" style={{ width: '100%' }}
              onClick={() => { setOpen(false); navigate('/help') }}>
              📖 使用帮助
            </button>
          </div>
        </div>
      )}
      <button
        onClick={() => setOpen((o) => !o)}
        style={btnStyle}
        onMouseEnter={(e) => { e.currentTarget.style.transform = 'scale(1.08)' }}
        onMouseLeave={(e) => { e.currentTarget.style.transform = 'scale(1)' }}
        title={open ? '关闭客服' : '联系在线客服'}
        aria-label={open ? '关闭客服' : '联系在线客服'}
      >
        {open ? '✕' : '🎧'}
      </button>
    </div>
  )
}
