import React, { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import api from '../api'

export default function Register() {
  const [form, setForm] = useState({
    company: '', email: '', password: '', name: '管理员',
    invite: new URLSearchParams(window.location.search).get('invite') || '',
  })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const handleChange = (e) => setForm({ ...form, [e.target.name]: e.target.value })

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      // 有邀请码：走成员接受邀请流程（加入已有企业）
      if (form.invite) {
        const { data } = await api.post('/members/invite/accept', {
          code: form.invite, email: form.email,
          password: form.password, name: form.name,
        })
        localStorage.setItem('token', data.access_token || '')
        localStorage.setItem('company_id', data.company_id)
        localStorage.setItem('company', '我的企业')
        localStorage.setItem('role', data.role || 'staff')
        try {
          const me = await api.get('/auth/me')
          localStorage.setItem('user_id', me.data.user_id)
        } catch { /* 拿不到 user_id 不阻塞注册流程 */ }
        navigate('/dashboard')
        return
      }
      const { data } = await api.post('/auth/register', form)
      localStorage.setItem('token', data.access_token)
      localStorage.setItem('company_id', data.company_id)
      localStorage.setItem('company', form.company)
      localStorage.setItem('role', data.role || 'admin')
      try {
        const me = await api.get('/auth/me')
        localStorage.setItem('user_id', me.data.user_id)
      } catch { /* 同上 */ }
      navigate('/dashboard')
    } catch (err) {
      setError(err.response?.data?.detail || '注册失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%)',
    }}>
      <div className="card" style={{ width: 420 }}>
        <h2 style={{ textAlign: 'center', marginBottom: 6 }}>
          {form.invite ? '加入企业' : '企业注册'}
        </h2>
        <p style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 13, marginBottom: 24 }}>
          {form.invite
            ? '通过邀请码加入已有企业（管理员已分配角色）'
            : '注册即创建企业 + 管理员账号（多租户隔离）'}
        </p>
        {error && <div className="alert alert-error">{error}</div>}
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>管理员姓名</label>
            <input name="name" value={form.name} onChange={handleChange} required />
          </div>
          {!form.invite && (
            <div className="form-group">
              <label>企业名称</label>
              <input name="company" value={form.company} onChange={handleChange} placeholder="如：太阳玻璃仪器" required />
            </div>
          )}
          <div className="form-group">
            <label>邮箱</label>
            <input name="email" type="email" value={form.email} onChange={handleChange} required />
          </div>
          <div className="form-group">
            <label>密码</label>
            <input name="password" type="password" value={form.password} onChange={handleChange} minLength={6} required />
          </div>
          <button className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
            {loading ? '注册中...' : form.invite ? '接受邀请并加入' : '注册并创建企业'}
          </button>
        </form>
        <p style={{ textAlign: 'center', marginTop: 16, fontSize: 13, color: 'var(--text-muted)' }}>
          已有账号？<Link to="/login" style={{ color: 'var(--primary)' }}>去登录</Link>
        </p>
      </div>
    </div>
  )
}
