import React from 'react'

/**
 * PageHeader —— 页面标题栏统一组件
 * 标题 + 右侧操作按钮，paddingRight 预留右上角主题切换器空间
 * 用法：<PageHeader title="AI 员工管理">
 *         <button ...>+ 创建</button>
 *       </PageHeader>
 */
export default function PageHeader({ title, subtitle, children, style }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 20, paddingRight: 64, flexWrap: 'wrap', ...style }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
        <h1 style={{ fontSize: 22, margin: 0 }}>{title}</h1>
        {subtitle && <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{subtitle}</span>}
      </div>
      {children && <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>{children}</div>}
    </div>
  )
}
