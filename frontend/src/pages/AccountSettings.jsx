import React, { useEffect, useState } from 'react'
import api from '../api'

export default function AccountSettings() {
  const [mobile, setMobile] = useState('')
  const [currentMobile, setCurrentMobile] = useState('')
  const [mobileVerified, setMobileVerified] = useState(false)
  const [msg, setMsg] = useState('')
  const [error, setError] = useState('')

  // 改手机号验证码
  const [smsModal, setSmsModal] = useState(false)
  const [smsCode, setSmsCode] = useState('')
  const [sending, setSending] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [countdown, setCountdown] = useState(0)
  const [mockCode, setMockCode] = useState('')

  // 改登录密码（当前密码 → 新密码）
  const [pw, setPw] = useState({ old_password: '', new_password: '', confirm: '' })
  const [pwMsg, setPwMsg] = useState('')
  const [pwLoading, setPwLoading] = useState(false)

  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    try {
      const { data } = await api.get('/company/settings', { params: { company_id: companyId } })
      setCurrentMobile(data.mobile || '')
      setMobile('')
      setMobileVerified(Boolean(data.mobile_verified))
    } catch (e) { /* 忽略 */ }
  }

  useEffect(() => { load() }, [])
  useEffect(() => {
    if (countdown <= 0) return
    const t = setTimeout(() => setCountdown((c) => c - 1), 1000)
    return () => clearTimeout(t)
  }, [countdown])

  // ===== 改手机号 =====
  const sendMobileCode = async () => {
    if (!mobile || mobile.length < 7) { setError('请填写新手机号'); return }
    setSending(true); setError(''); setMsg('')
    try {
      const { data } = await api.post('/company/sms/send', {
        company_id: companyId, mobile, purpose: 'verify_mobile',
      })
      setMockCode(data.mock_code || '')
      setSmsModal(true)
      setCountdown(60)
      setMsg('✅ 验证码已发送到新手机号')
    } catch (e) {
      const d = e.response?.data?.detail
      setError(typeof d === 'string' ? d : '发送失败')
    } finally { setSending(false) }
  }

  const confirmMobile = async () => {
    setVerifying(true)
    try {
      const { data } = await api.post('/company/sms/verify', {
        company_id: companyId, mobile, code: smsCode.trim(), purpose: 'verify_mobile',
      })
      setSmsModal(false)
      setSmsCode('')
      setMsg('✅ 手机号验证成功，已更新')
      load()
    } catch (e) {
      const d = e.response?.data?.detail
      setError(typeof d === 'string' ? d : '验证失败')
    } finally { setVerifying(false) }
  }

  // ===== 改登录密码（当前密码 → 新密码）=====
  const changePassword = async () => {
    if (pw.old_password.length < 6) { setPwMsg('❌ 请输入当前密码'); return }
    if (pw.new_password.length < 6) { setPwMsg('❌ 新密码至少 6 位'); return }
    if (pw.new_password !== pw.confirm) { setPwMsg('❌ 两次输入的新密码不一致'); return }
    setPwLoading(true)
    try {
      await api.put('/company/password', pw)
      setPwMsg('✅ 密码修改成功')
      setPw({ old_password: '', new_password: '', confirm: '' })
    } catch (e) {
      const d = e.response?.data?.detail
      setPwMsg(`❌ ${typeof d === 'string' ? d : '修改失败'}`)
    } finally { setPwLoading(false) }
  }

  return (
    <div>
      <h1 style={{ fontSize: 22, marginBottom: 6 }}>👤 账户设置</h1>
      <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 24 }}>
        修改企业联系手机号、账户登录密码（安全操作均需手机验证码）
      </p>

      {msg && <div className="alert alert-success">{msg}</div>}
      {error && <div className="alert alert-error">{error}</div>}

    <div style={{
      display: 'grid',
      gridTemplateColumns: '1fr 1fr',
      gap: 24, alignItems: 'start',
    }}>
      {/* 左：表单 */}
      <div>
        {/* 修改手机号 */}
      <div className="card" style={{ marginBottom: 20 }}>
        <h3 style={{ marginBottom: 16 }}>修改手机号</h3>
        <div className="form-group">
          <input
            value={mobile}
            onChange={(e) => setMobile(e.target.value)}
            placeholder="示例：13800138000"
            maxLength={50}
          />
          {currentMobile && (
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
              当前绑定：<code>{currentMobile}</code>
            </div>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {mobileVerified
            ? <span className="badge badge-green">✓ 已验证</span>
            : <span className="badge badge-yellow">⚠ 未验证</span>}
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            修改手机号后需短信验证码确认
          </span>
        </div>
        <button className="btn btn-primary" onClick={sendMobileCode}
          disabled={sending || countdown > 0 || !mobile}
          style={{ marginTop: 12 }}>
          {countdown > 0 ? `重新获取(${countdown}s)` : sending ? '发送中...' : '📱 获取验证码'}
        </button>
      </div>

      {/* 修改登录密码 */}
      <div className="card">
        <h3 style={{ marginBottom: 16 }}>修改登录密码</h3>
        <div className="alert alert-info" style={{ fontSize: 12, marginBottom: 16 }}>
          输入当前密码后再设置新密码（至少 6 位）。修改密码后已登录的设备不会被强制下线。
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <div className="form-group">
            <label>当前密码</label>
            <input type="password" value={pw.old_password}
              onChange={(e) => setPw({ ...pw, old_password: e.target.value })} />
          </div>
          <div className="form-group">
            <label>新密码（至少 6 位）</label>
            <input type="password" value={pw.new_password}
              onChange={(e) => setPw({ ...pw, new_password: e.target.value })} />
          </div>
          <div className="form-group" style={{ gridColumn: '1 / -1' }}>
            <label>确认新密码</label>
            <input type="password" value={pw.confirm}
              onChange={(e) => setPw({ ...pw, confirm: e.target.value })} />
          </div>
        </div>
        {pwMsg && (
          <div style={{ marginTop: 12 }}>
            {pwMsg.startsWith('✅')
              ? <div className="alert alert-success">{pwMsg}</div>
              : <div className="alert alert-error">{pwMsg}</div>}
          </div>
        )}
        <button className="btn btn-primary" onClick={changePassword}
          disabled={pwLoading} style={{ marginTop: 12 }}>
          {pwLoading ? '修改中...' : '🔐 修改登录密码'}
        </button>
      </div>

      {/* 关闭左栏 */}
      </div>

      {/* 改手机号验证码弹窗 */}
      {smsModal && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }} onClick={() => setSmsModal(false)}>
          <div className="card" style={{ width: 380 }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginBottom: 8 }}>📱 验证新手机号</h3>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 12 }}>
              验证码已发送至 <code>{mobile}</code>
            </p>
            {mockCode && (
              <div className="alert alert-info" style={{ fontSize: 12 }}>
                演示验证码：<strong>{mockCode}</strong>
              </div>
            )}
            <input value={smsCode}
              onChange={(e) => setSmsCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              placeholder="123456" maxLength={6} autoFocus
              style={{ letterSpacing: 6, textAlign: 'center', fontSize: 18, marginBottom: 16 }} />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button className="btn btn-secondary" onClick={() => setSmsModal(false)}>取消</button>
              <button className="btn btn-primary" onClick={confirmMobile}
                disabled={verifying || smsCode.length !== 6}>
                {verifying ? '验证中...' : '确认验证'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 右：使用帮助侧栏 */}
      <aside>
        <div className="card">
          <h3 style={{ fontSize: 15, color: 'var(--primary)', marginBottom: 12 }}>
            📖 制造业 AI 员工智能平台使用帮助
          </h3>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.9 }}>
            <div style={{ marginBottom: 10 }}>
              <strong style={{ color: 'var(--primary)' }}>1. 业务菜单（核心功能）</strong><br />
              · <b>驾驶舱</b>：企业 KPI（销售/订单/生产/质量）+ AI 建议<br />
              · <b>AI 员工</b>：管理 Lisa/Amy 等 AI 员工，可设置"性格设定"<br />
              · <b>AI 对话</b>：和 AI 员工实时对话（须先企业认证）<br />
              · <b>知识库</b>：上传 PDF/Word/Excel，AI 员工回答时自动引用<br />
              · <b>CRM 客户</b>：管理客户线索、跟进状态<br />
              · <b>报价单</b>：AI 一键生成专业报价 + PDF
            </div>
            <div style={{ marginBottom: 10 }}>
              <strong style={{ color: 'var(--primary)' }}>2. 企业菜单（管理）</strong><br />
              · <b>Agent 商城</b>：浏览/一键安装行业 AI 包<br />
              · <b>套餐与账单</b>：升级套餐、查看用量、支付账单<br />
              · <b>企业设置</b>：基本信息、营业执照、操作日志、成员管理<br />
              · <b>账户设置</b>：修改手机号、修改登录密码（← 你在这里）
            </div>
            <div style={{ marginBottom: 10 }}>
              <strong style={{ color: 'var(--primary)' }}>3. 安全操作（当前页）</strong><br />
              · <b>修改手机号</b>：需短信验证码（演示版 123456）<br />
              · <b>修改登录密码</b>：输入当前密码 + 新密码 6 位以上<br />
              · <b>安全提示</b>：敏感操作均记录到"操作日志"
            </div>
            <div>
              <strong style={{ color: 'var(--primary)' }}>4. 遇到问题怎么办？</strong><br />
              · 联系您的<strong>企业管理员</strong>（邀请您加入的同事）<br />
              · 工作日内 <strong>30 分钟内</strong>在线响应<br />
              · 紧急情况请直接拨打服务热线
            </div>
          </div>
          <div style={{
            marginTop: 12, padding: 8, background: 'var(--bg-subtle)', borderRadius: 6,
            fontSize: 11, color: 'var(--text-muted)', textAlign: 'center',
          }}>
            遇到问题？联系企业管理员或平台超管
          </div>
        </div>
      </aside>
    </div>
    </div>
  )
}