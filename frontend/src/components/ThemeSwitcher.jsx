import React, { useEffect, useState } from 'react'

const PRESETS = [
  { key: 'default',   label: 'Default',  light: true,  color: 'blue' },
  { key: 'oak',       label: 'Oak',      light: true,  color: 'blue' },
  { key: 'lavender',  label: 'Lavender', light: true,  color: 'purple' },
  { key: 'dark',      label: 'Dark',     light: false, color: 'blue' },
]

const COLORS = [
  { key: 'blue',   hex: 'var(--primary)' },
  { key: 'green',  hex: '#22c55e' },
  { key: 'purple', hex: '#a855f7' },
  { key: 'orange', hex: '#f97316' },
  { key: 'cyan',   hex: '#06b6d4' },
  { key: 'pink',   hex: '#ec4899' },
  { key: 'red',    hex: '#ef4444' },
  { key: 'gray',   hex: 'var(--text-muted)' },
]

export default function ThemeSwitcher() {
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState('light')      // light / dark
  const [color, setColor] = useState('blue')
  const [preset, setPreset] = useState('default')

  // 初始化：从 localStorage 读取
  useEffect(() => {
    const m = localStorage.getItem('theme_mode') || 'light'
    const c = localStorage.getItem('theme_color') || 'blue'
    setMode(m); setColor(c)
    applyTheme(m, c)
  }, [])

  // 同步到 <html> 标签
  const applyTheme = (m, c) => {
    const html = document.documentElement
    html.setAttribute('data-theme', m)
    html.setAttribute('data-color', c)
  }

  // 选择预设：同步设置 mode + color
  const pickPreset = (p) => {
    setPreset(p.key)
    setMode(p.light ? 'light' : 'dark')
    setColor(p.color)
    applyTheme(p.light ? 'light' : 'dark', p.color)
    localStorage.setItem('theme_mode', p.light ? 'light' : 'dark')
    localStorage.setItem('theme_color', p.color)
  }

  // 切换明暗（保留主色）
  const toggleMode = () => {
    const m = mode === 'light' ? 'dark' : 'light'
    setMode(m); setPreset(m === 'dark' ? 'dark' : 'default')
    applyTheme(m, color)
    localStorage.setItem('theme_mode', m)
  }

  // 切换主色（保留 mode）
  const pickColor = (k) => {
    setColor(k)
    applyTheme(mode, k)
    localStorage.setItem('theme_color', k)
  }

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      if (!e.target.closest('[data-theme-switcher]')) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  return (
    <div data-theme-switcher style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="主题设置"
        style={{
          background: 'transparent', border: '1px solid var(--border-strong)',
          borderRadius: 8, padding: '6px 10px', fontSize: 16, cursor: 'pointer',
          color: 'var(--sidebar-text)', display: 'flex', alignItems: 'center', gap: 4,
        }}
      >
        🎨 <span style={{ fontSize: 11, marginLeft: 2 }}>{mode === 'dark' ? '🌙' : '☀️'}</span>
      </button>

      {open && (
        <div
          data-theme-switcher
          style={{
            position: 'absolute', right: 0, top: '110%', zIndex: 1000,
            width: 300, padding: 16, background: 'var(--bg-elevated)',
            border: '1px solid var(--border)', borderRadius: 12,
            boxShadow: 'var(--shadow-pop)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
            <h4 style={{ fontSize: 14, color: 'var(--text)' }}>🎨 全局调色板</h4>
            <button
              onClick={toggleMode}
              style={{
                marginLeft: 'auto', padding: '4px 10px', borderRadius: 999,
                border: '1px solid var(--border-strong)',
                background: 'var(--bg-subtle)', color: 'var(--text)',
                fontSize: 11, cursor: 'pointer',
              }}
            >
              {mode === 'dark' ? '☀️ 切到日间' : '🌙 切到夜间'}
            </button>
          </div>

          {/* 主题预设 */}
          <div style={{
            display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14,
          }}>
            {PRESETS.map((p) => (
              <div
                key={p.key}
                onClick={() => pickPreset(p)}
                style={{
                  border: preset === p.key
                    ? '2px solid var(--primary)'
                    : '1px solid var(--border-strong)',
                  borderRadius: 8, padding: 8, cursor: 'pointer',
                  background: 'var(--bg-subtle)',
                }}
              >
                <div style={{
                  height: 28, borderRadius: 4, marginBottom: 6,
                  background: p.light
                    ? (p.color === 'purple'
                        ? 'linear-gradient(90deg, #f3e8ff, #a855f7, #1e293b, #475569)'
                        : 'linear-gradient(90deg, #dbeafe, #2563eb, #1e293b, #475569)')
                    : 'linear-gradient(90deg, #3b82f6, #1e3a8a, #0f172a, #020617)',
                }} />
                <div style={{ textAlign: 'center', fontSize: 12, color: 'var(--text)' }}>{p.label}</div>
              </div>
            ))}
          </div>

          {/* 自定义主色 */}
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8 }}>
            主题色
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {COLORS.map((c) => (
              <div
                key={c.key}
                onClick={() => pickColor(c.key)}
                title={c.key}
                style={{
                  width: 22, height: 22, borderRadius: '50%', background: c.hex,
                  cursor: 'pointer', border: color === c.key ? '2px solid var(--text)' : '2px solid transparent',
                  boxShadow: '0 0 0 1px var(--border)',
                }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
