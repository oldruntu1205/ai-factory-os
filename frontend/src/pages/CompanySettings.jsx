import React, { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import api from '../api'

const LICENSE_STATUS = {
  none:     { label: '未提交', cls: 'badge-gray',   desc: '尚未提交营业执照' },
  pending:  { label: 'AI 审核中', cls: 'badge-yellow', desc: '营业执照正在 AI 审核中' },
  approved: { label: '已认证',   cls: 'badge-green',  desc: '营业执照已通过审核' },
  rejected: { label: '未通过',   cls: 'badge-red',    desc: '营业执照审核未通过，请重新提交' },
}

const TABS = [
  { key: 'basic', label: '基本信息' },
  { key: 'license', label: '企业认证' },
  { key: 'members', label: '成员管理' },
  { key: 'invoice', label: '开票信息' },
  { key: 'security', label: '操作日志' },
]

export default function CompanySettings() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initialTab = (() => {
    const t = searchParams.get('tab')
    if (['basic', 'license', 'security', 'members', 'invoice'].includes(t)) return t
    return 'basic'
  })()
  const [tab, setTab] = useState(initialTab)
  const [company, setCompany] = useState(null)
  const [form, setForm] = useState({ name: '', address: '', mobile: '',
                                     contact_name: '', contact_email: '', industry: '' })
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [industries, setIndustries] = useState([])
  const fileRef = useRef(null)
  const companyId = Number(localStorage.getItem('company_id'))

  // 验证码流程
  const [showSmsModal, setShowSmsModal] = useState(false)
  const [smsCode, setSmsCode] = useState('')
  const [sendingSms, setSendingSms] = useState(false)
  const [verifyingSms, setVerifyingSms] = useState(false)
  const [smsCountdown, setSmsCountdown] = useState(0)
  const [mockCode, setMockCode] = useState('')

  // 安全设置
  const [logs, setLogs] = useState([])

  // 成员管理
  const [members, setMembers] = useState([])
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState('staff')
  const [inviteCode, setInviteCode] = useState('')
  const [memberMsg, setMemberMsg] = useState('')

  // 开票信息
  const [invInfo, setInvInfo] = useState(null)
  const [invMsg, setInvMsg] = useState('')
  const [invSaving, setInvSaving] = useState(false)

  const load = async () => {
    try {
      const [{ data }, { data: ind }] = await Promise.all([
        api.get('/company/settings', { params: { company_id: companyId } }),
        api.get('/company/industries'),
      ])
      setCompany(data)
      setForm({
        name: data.name || '', address: data.address || '',
        mobile: data.mobile || '',
        contact_name: data.contact_name || '', contact_email: data.contact_email || '',
        industry: data.industry || '',
      })
      setIndustries(ind.industries || [])
    } catch (e) { setError(e.response?.data?.detail || '加载失败') }
  }

  const loadLogs = async () => {
    const { data } = await api.get('/company/audit-logs', { params: { company_id: companyId } })
    setLogs(data.logs || [])
  }

  const loadMembers = async () => {
    const { data } = await api.get('/members/list', { params: { company_id: companyId } })
    setMembers(data.members || [])
  }

  const loadInvInfo = async () => {
    try {
      const { data } = await api.get('/billing/invoice-info', { params: { company_id: companyId } })
      setInvInfo(data.info)
    } catch (e) { /* 忽略 */ }
  }

  const saveInvInfo = async () => {
    if (!invInfo || !invInfo.invoice_title || !invInfo.invoice_tax_no) {
      setInvMsg('❌ 请填写发票抬头与纳税人识别号')
      return
    }
    setInvSaving(true)
    try {
      const { data } = await api.put('/billing/invoice-info', invInfo,
        { params: { company_id: companyId } })
      setInvMsg(`✅ ${data.message}`)
      loadInvInfo()
    } catch (e) {
      setInvMsg(`❌ ${e.response?.data?.detail || '保存失败'}`)
    } finally {
      setInvSaving(false)
    }
  }

  useEffect(() => { load() }, [])
  useEffect(() => { if (tab === 'security') loadLogs() }, [tab])
  useEffect(() => { if (tab === 'members') loadMembers() }, [tab])
  useEffect(() => { if (tab === 'invoice') loadInvInfo() }, [tab])

  // 验证码倒计时
  useEffect(() => {
    if (smsCountdown <= 0) return
    const t = setTimeout(() => setSmsCountdown((c) => c - 1), 1000)
    return () => clearTimeout(t)
  }, [smsCountdown])

  const sendSms = async () => {
    if (!form.mobile || form.mobile.length < 7) {
      setError('请填写联系手机号')
      return
    }
    setSendingSms(true)
    setError('')
    setMessage('')
    try {
      const { data } = await api.post('/company/sms/send', {
        company_id: companyId, mobile: form.mobile, purpose: 'verify_mobile',
      })
      setShowSmsModal(true)
      setSmsCountdown(60)
      setMockCode(data.mock_code || '')
      setMessage('✅ 验证码已发送（10 分钟有效）')
    } catch (e) {
      const detail = e.response?.data?.detail || '发送失败'
      setError(typeof detail === 'string' ? detail : '发送失败')
    } finally {
      setSendingSms(false)
    }
  }

  const verifySms = async () => {
    if (!smsCode.trim()) return
    setVerifyingSms(true)
    try {
      await api.post('/company/sms/verify', {
        company_id: companyId, mobile: form.mobile, code: smsCode.trim(),
        purpose: 'verify_mobile',
      })
      setShowSmsModal(false)
      setSmsCode('')
      setMessage('✅ 手机验证成功')
      load()
    } catch (e) {
      const detail = e.response?.data?.detail || '验证失败'
      setError(typeof detail === 'string' ? detail : '验证失败')
    } finally {
      setVerifyingSms(false)
    }
  }

  const save = async () => {
    setSaving(true); setMessage(''); setError('')
    try {
      const { data } = await api.put('/company/settings', { company_id: companyId, ...form })
      setCompany(data.company)
      localStorage.setItem('company', data.company.name)
      setMessage('✅ 企业信息已保存')
    } catch (e) { setError(e.response?.data?.detail || '保存失败') } finally { setSaving(false) }
  }

  const uploadLicense = async (e) => {
    const file = e.target.files[0]
    if (!file) return
    const inputEl = e.target  // 先保存引用，避免异步后访问合成事件
    setUploading(true); setMessage(''); setError('')
    try {
      const fd = new FormData()
      fd.append('company_id', companyId)
      fd.append('file', file)
      await api.post('/company/license/upload', fd)
      setMessage('📤 营业执照已上传，AI 审核约 1 秒...')
      load()
      setTimeout(() => load(), 1500)
    } catch (err) {
      const d = err.response?.data?.detail
      setError(typeof d === 'string' ? d : (Array.isArray(d) ? d.map(x => x.msg).join('; ') : '上传失败'))
    } finally {
      setUploading(false)
      if (inputEl) inputEl.value = ''
    }
  }

  const removeLicense = async () => {
    if (!window.confirm('确认清除营业执照？')) return
    await api.delete('/company/license', { params: { company_id: companyId } })
    load()
  }

  const invite = async () => {
    setMemberMsg('')
    try {
      const { data } = await api.post('/members/invite', {
        company_id: companyId, email: inviteEmail, role: inviteRole,
      })
      setInviteCode(data.code)
      setMemberMsg(`✅ 邀请已创建：注册链接末尾加 ?invite=${data.code}`)
      setInviteEmail('')
      loadMembers()
    } catch (e) { setMemberMsg(`❌ ${e.response?.data?.detail || '邀请失败'}`) }
  }

  const setRole = async (id, role) => {
    await api.put(`/members/${id}/role`, { role, company_id: companyId })
    loadMembers()
  }

  const toggleStatus = async (m) => {
    await api.put(`/members/${m.id}/status`, {
      status: m.status === 'active' ? 'disabled' : 'active', company_id: companyId,
    })
    loadMembers()
  }

  const removeMember = async (m) => {
    if (!window.confirm(`确认移除成员 ${m.name || m.email}？`)) return
    await api.delete(`/members/${m.id}`, { data: { company_id: companyId } })
    loadMembers()
  }

  if (!company) return <div>加载中...</div>
  const ls = LICENSE_STATUS[company.license_status] || LICENSE_STATUS.none

  return (
    <div style={{ maxWidth: 960 }}>
      <h1 style={{ fontSize: 22, marginBottom: 6 }}>⚙️ 企业设置</h1>
      <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 20 }}>
        企业信息 · 认证 · 安全 · 成员
      </p>

      {/* Tab 导航 */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 20, borderBottom: '2px solid #e2e8f0', paddingBottom: 12 }}>
        {TABS.map((t) => (
          <button key={t.key} onClick={() => { setTab(t.key); setSearchParams({ tab: t.key }) }}
            style={{
              padding: '8px 18px', borderRadius: 8, border: 'none', cursor: 'pointer',
              background: tab === t.key ? 'var(--primary)' : 'var(--border)',
              color: tab === t.key ? '#fff' : 'var(--text-muted)', fontSize: 14, fontWeight: 600,
            }}>
            {t.label}
          </button>
        ))}
      </div>

      {message && <div className="alert alert-success">{message}</div>}
      {error && <div className="alert alert-error">{error}</div>}

      {/* ===== 基本信息 ===== */}
      {tab === 'basic' && (
        <div className="card">
          <h3 style={{ marginBottom: 16 }}>企业基本信息</h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div className="form-group"><label>企业名称 *</label>
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={100} /></div>
            <div className="form-group"><label>联系人</label>
              <input value={form.contact_name} onChange={(e) => setForm({ ...form, contact_name: e.target.value })} maxLength={100} /></div>
            <div className="form-group">
              <label>联系手机</label>
              <div style={{ display: 'flex', gap: 8 }}>
                <input
                  value={form.mobile}
                  onChange={(e) => setForm({ ...form, mobile: e.target.value })}
                  placeholder="+86 138-0000-0000"
                  maxLength={50}
                  style={{ flex: 1 }}
                />
                <button className="btn btn-secondary btn-sm"
                  onClick={sendSms}
                  disabled={sendingSms || smsCountdown > 0 || !form.mobile}>
                  {smsCountdown > 0 ? `重新获取(${smsCountdown}s)` :
                   sendingSms ? '发送中...' : '获取验证码'}
                </button>
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
                {company?.mobile_verified ?
                  <span className="badge badge-green">✓ 已验证</span> :
                  <span className="badge badge-yellow">⚠ 未验证（保存前需完成验证）</span>}
              </div>
            </div>
            <div className="form-group"><label>联系邮箱</label>
              <input type="email" value={form.contact_email} onChange={(e) => setForm({ ...form, contact_email: e.target.value })} maxLength={100} /></div>
            <div className="form-group">
              <label>所属行业</label>
              <select
                value={form.industry}
                onChange={(e) => setForm({ ...form, industry: e.target.value })}
                disabled={Boolean(form.industry)}
                style={form.industry ? { background: 'var(--bg-subtle)', color: 'var(--text-muted)', cursor: 'not-allowed' } : undefined}
              >
                <option value="">请选择行业（GB/T 4754-2017 · 制造业）</option>
                {industries.map((i) => (
                  <option key={i.code} value={i.code}>
                    {i.name}
                  </option>
                ))}
              </select>
              {form.industry ? (
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
                  🔒 所属行业已设置，锁定不可修改（如需变更请联系平台管理员）
                </div>
              ) : (
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6 }}>
                  设置后锁定，不可再次修改
                </div>
              )}
            </div>
            <div className="form-group" style={{ gridColumn: '1 / -1' }}><label>企业地址</label>
              <input value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} maxLength={300} /></div>
          </div>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? '保存中...' : '保存基本信息'}
          </button>
        </div>
      )}

      {/* ===== 企业认证 ===== */}
      {tab === 'license' && (
        <div className="card">
          <h3 style={{ marginBottom: 6 }}>企业认证（营业执照）</h3>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16 }}>
            上传营业执照照片，通过平台审核后顶部将展示「✓ 已认证」徽标
          </p>
          <div style={{ padding: 20, background: 'var(--bg-subtle)', borderRadius: 12, marginBottom: 16, border: '1px dashed #cbd5e1' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
              <div style={{
                width: 72, height: 72, borderRadius: 12, background: 'var(--bg-elevated)',
                display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 32,
                border: '1px solid #e2e8f0',
              }}>
                {company.license_status === 'approved' ? '✅'
                  : company.license_status === 'rejected' ? '❌'
                  : company.license_status === 'pending' ? '⏳' : '📄'}
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  <span className={`badge ${ls.cls}`} style={{ marginRight: 8 }}>{ls.label}</span>
                  企业营业执照
                </div>
                <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>{ls.desc}</div>
                {company.license_reject_reason && (
                  <div style={{ fontSize: 12, color: '#b91c1c', marginTop: 4 }}>
                    拒绝原因：{company.license_reject_reason}
                  </div>
                )}
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
            <label className="btn btn-primary" style={{ cursor: 'pointer' }}>
              {uploading ? '上传中...' : company.has_license ? '重新上传' : '上传营业执照'}
              <input ref={fileRef} type="file" style={{ display: 'none' }}
                accept=".jpg,.jpeg,.png,.pdf" onChange={uploadLicense} />
            </label>
            {company.has_license && (
              <button className="btn btn-danger" onClick={removeLicense}>清除已上传</button>
            )}
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>支持 JPG / PNG / PDF，最大 5MB</span>
          </div>
        </div>
      )}

      {/* ===== 操作日志 ===== */}
      {tab === 'security' && (
        <div>
          <div className="card">
            <h3 style={{ marginBottom: 16 }}>操作日志（安全审计）</h3>
            <div style={{ maxHeight: 360, overflowY: 'auto', border: '1px solid #e2e8f0', borderRadius: 8 }}>
            <table className="table">
              <thead><tr><th>时间</th><th>操作</th><th>详情</th><th>操作人</th></tr></thead>
              <tbody>
                {logs.map((l) => (
                  <tr key={l.id}>
                    <td style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
                      {new Date(l.created_at).toLocaleString('zh-CN')}
                    </td>
                    <td>
                      <span className="badge badge-blue">{l.action}</span>
                    </td>
                    <td style={{ fontSize: 13 }}>{l.detail}</td>
                    <td style={{ fontSize: 12 }}>{l.user_email || '—'}</td>
                  </tr>
                ))}
                {logs.length === 0 && (
                  <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无操作日志</td></tr>
                )}
              </tbody>
            </table>
            </div>
          </div>
        </div>
      )}

      {/* ===== 成员管理 ===== */}
      {tab === 'members' && (
        <div>
          <div className="card" style={{ marginBottom: 20 }}>
            <h3 style={{ marginBottom: 16 }}>邀请成员</h3>
            <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr auto', gap: 12, alignItems: 'end' }}>
              <div className="form-group" style={{ marginBottom: 0 }}>
                <label>成员邮箱</label>
                <input type="email" value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  placeholder="colleague@example.com" />
              </div>
              <div className="form-group" style={{ marginBottom: 0 }}>
                <label>角色</label>
                <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value)}>
                  <option value="staff">普通成员</option>
                  <option value="admin">管理员</option>
                </select>
              </div>
              <button className="btn btn-primary" onClick={invite}>生成邀请</button>
            </div>
            {memberMsg && (
              <div style={{ marginTop: 12 }}>
                {memberMsg.startsWith('✅') ?
                  <div className="alert alert-success">{memberMsg}</div> :
                  <div className="alert alert-error">{memberMsg}</div>}
              </div>
            )}
            {inviteCode && (
              <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-muted)' }}>
                邀请码：<code>{inviteCode}</code>（有效期 7 天，新成员在注册页输入邀请码加入）
              </div>
            )}
          </div>

          <div className="card">
            <h3 style={{ marginBottom: 16 }}>企业成员（{members.length}）</h3>
            <table className="table">
              <thead><tr><th>成员</th><th>角色</th><th>状态</th><th>加入时间</th><th>操作</th></tr></thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.id}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{m.name}</div>
                      <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{m.email}</div>
                    </td>
                    <td>
                      <select value={m.role}
                        onChange={(e) => setRole(m.id, e.target.value)}
                        disabled={m.id === Number(localStorage.getItem('user_id'))}
                        style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #cbd5e1', fontSize: 12 }}>
                        <option value="admin">管理员</option>
                        <option value="staff">普通成员</option>
                      </select>
                    </td>
                    <td>
                      <span className={`badge ${m.status === 'active' ? 'badge-green' : 'badge-red'}`}>
                        {m.status === 'active' ? '正常' : '已停用'}
                      </span>
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {new Date(m.created_at).toLocaleDateString('zh-CN')}
                    </td>
                    <td>
                      <button className="btn btn-secondary btn-sm"
                        onClick={() => toggleStatus(m)}
                        disabled={m.id === Number(localStorage.getItem('user_id'))}>
                        {m.status === 'active' ? '停用' : '启用'}
                      </button>
                      <button className="btn btn-danger btn-sm" style={{ marginLeft: 6 }}
                        onClick={() => removeMember(m)}
                        disabled={m.id === Number(localStorage.getItem('user_id'))}>
                        移除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ===== 开票信息 ===== */}
      {tab === 'invoice' && (
        <div>
          <div className="card">
            <h3 style={{ marginBottom: 6 }}>开票信息</h3>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16, lineHeight: 1.7 }}>
              开票类别：<b>服务费</b>（SaaS 订阅服务）。填写本单位的开票资料后，
              可在「套餐与账单」对已支付账单提交开票申请；电子发票开具后发送至企业邮箱。
            </p>
            {invMsg && (
              <div style={{ marginBottom: 12 }}>
                {invMsg.startsWith('✅')
                  ? <div className="alert alert-success">{invMsg}</div>
                  : <div className="alert alert-error">{invMsg}</div>}
              </div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 14 }}>
              <div className="form-group">
                <label>发票抬头（企业全称）*</label>
                <input value={invInfo?.invoice_title || ''}
                  onChange={(e) => setInvInfo({ ...invInfo, invoice_title: e.target.value })}
                  placeholder="与营业执照一致的企业全称" />
              </div>
              <div className="form-group">
                <label>纳税人识别号 *</label>
                <input value={invInfo?.invoice_tax_no || ''}
                  onChange={(e) => setInvInfo({ ...invInfo, invoice_tax_no: e.target.value })}
                  placeholder="统一社会信用代码" />
              </div>
              <div className="form-group">
                <label>开户银行</label>
                <input value={invInfo?.invoice_bank || ''}
                  onChange={(e) => setInvInfo({ ...invInfo, invoice_bank: e.target.value })} />
              </div>
              <div className="form-group">
                <label>银行账号</label>
                <input value={invInfo?.invoice_bank_account || ''}
                  onChange={(e) => setInvInfo({ ...invInfo, invoice_bank_account: e.target.value })} />
              </div>
              <div className="form-group">
                <label>注册地址</label>
                <input value={invInfo?.invoice_address || ''}
                  onChange={(e) => setInvInfo({ ...invInfo, invoice_address: e.target.value })} />
              </div>
              <div className="form-group">
                <label>注册电话</label>
                <input value={invInfo?.invoice_phone || ''}
                  onChange={(e) => setInvInfo({ ...invInfo, invoice_phone: e.target.value })} />
              </div>
              <div className="form-group">
                <label>发票类型</label>
                <select value={invInfo?.invoice_type || 'normal'}
                  onChange={(e) => setInvInfo({ ...invInfo, invoice_type: e.target.value })}>
                  <option value="normal">电子普通发票</option>
                  <option value="special">电子专票（需一般纳税人）</option>
                </select>
              </div>
            </div>
            <button className="btn btn-primary" style={{ marginTop: 16 }}
              onClick={saveInvInfo} disabled={invSaving}>
              {invSaving ? '保存中...' : '保存开票信息'}
            </button>
          </div>
        </div>
      )}

      {/* 短信验证码弹窗 */}
      {showSmsModal && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
        }} onClick={() => setShowSmsModal(false)}>
          <div className="card" style={{ width: 400 }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginBottom: 8 }}>📱 验证手机号</h3>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 16 }}>
              验证码已发送至 <code>{form.mobile}</code>（10 分钟内有效）
            </p>
            {mockCode && (
              <div className="alert alert-info" style={{ fontSize: 12 }}>
                演示验证码：<strong>{mockCode}</strong>（生产环境通过短信接收）
              </div>
            )}
            <div className="form-group">
              <label>6 位验证码</label>
              <input
                value={smsCode}
                onChange={(e) => setSmsCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                placeholder="123456"
                maxLength={6}
                autoFocus
                style={{ letterSpacing: 6, textAlign: 'center', fontSize: 18 }}
              />
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
              <button className="btn btn-secondary" onClick={() => setShowSmsModal(false)}>取消</button>
              <div style={{ display: 'flex', gap: 8 }}>
                {smsCountdown > 0 && (
                  <button className="btn btn-secondary btn-sm" disabled>
                    {smsCountdown}s 后重发
                  </button>
                )}
                <button className="btn btn-primary" onClick={verifySms}
                  disabled={verifyingSms || smsCode.length !== 6}>
                  {verifyingSms ? '验证中...' : '确认验证'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}