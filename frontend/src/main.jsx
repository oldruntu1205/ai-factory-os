import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'

// 全局错误边界：任何页面渲染异常都不会白屏，而是显示可恢复的错误提示
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error) {
    return { error }
  }
  componentDidCatch(error, info) {
    console.error('页面渲染错误:', error, info)
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{
          minHeight: '100vh', display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
          background: 'var(--bg, #f1f5f9)', color: 'var(--text, #0f172a)',
          fontFamily: 'system-ui, sans-serif', padding: 24,
        }}>
          <div style={{ fontSize: 48, marginBottom: 12 }}>😵</div>
          <h2 style={{ fontSize: 18, marginBottom: 8 }}>页面出现了一点小问题</h2>
          <p style={{ fontSize: 13, color: '#94a3b8', marginBottom: 16, maxWidth: 480, textAlign: 'center' }}>
            {String(this.state.error?.message || this.state.error).slice(0, 200)}
          </p>
          <button onClick={() => { this.setState({ error: null }); window.location.reload() }}
            style={{
              padding: '10px 24px', borderRadius: 8, border: 'none',
              background: 'var(--primary, #2563eb)', color: '#fff', fontSize: 14,
              cursor: 'pointer',
            }}>
            刷新重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>
)
