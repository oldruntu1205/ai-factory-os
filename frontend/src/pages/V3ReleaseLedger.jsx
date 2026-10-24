import React, { useState } from 'react'
import { Link } from 'react-router-dom'
import PageHeader from '../components/PageHeader'
import { useV3Data } from '../useV3Data'
import { plain } from '../v3Terms'

/**
 * 边缘发布台账（V3.0）：七态状态机。
 *
 * ## 两条恒定安全声明必须照实显示
 *
 * `ai_new_actions_enabled` 恒 `false`、`control_write_disabled` 恒 `true`。
 * 它们在**任何状态下都不放开**——界面若把它们显示成"已放开"，
 * 就是在**替上游承诺**它没有承诺的事。
 *
 * 所以本页把它们放在显眼位置，并明确写"不随状态推进放开"。
 */
const STATES = ['DRAFT', 'SCHEMA_FROZEN', 'CALIBRATED', 'SHADOW', 'APPROVED', 'ACTIVE', 'REVOKED']

const STATE_LABEL = {
  DRAFT: '草稿',
  SCHEMA_FROZEN: '结构冻结',
  CALIBRATED: '已校准',
  SHADOW: '影子运行',
  APPROVED: '已批准',
  ACTIVE: '生效',
  REVOKED: '已撤销',
}

export default function V3ReleaseLedger() {
  const [releaseId, setReleaseId] = useState('')
  const [queried, setQueried] = useState('')

  const { data, error, errorText, busy, reload } = useV3Data(
    queried ? `/v3-user/release?release_id=${encodeURIComponent(queried)}` : null,
    { skip: !queried },
  )

  const release = data?.release

  return (
    <>
      <PageHeader
        title="设备程序发布"
        when="给车间设备更新程序时用。这是设备/运维的事，日常业务不需要点进来。"
        whenKind="muted"
        subtitle="一次发布走到哪一步、现在能不能叫停"
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="form-group">
          <label>发布单编号</label>
          <input
            value={releaseId}
            onChange={(e) => setReleaseId(e.target.value)}
            placeholder="release-xxxxxxxx"
            onKeyDown={(e) => e.key === 'Enter' && setQueried(releaseId.trim())}
          />
        </div>
        <button
          className="btn btn-primary"
          onClick={() => setQueried(releaseId.trim())}
          disabled={busy || !releaseId.trim()}
        >
          {busy ? '查询中…' : '查询台账'}
        </button>
      </div>

      {error && (
        <div className="alert alert-error">
          {errorText}
          {error.status === 401 && (
            <>
              {' '}
              <Link to="/login">去登录</Link>
            </>
          )}
        </div>
      )}

      {release && (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <span className="badge badge-blue">{release.status}</span>
              <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>
                {STATE_LABEL[release.status] ?? ''}
              </span>
              {data.isTerminal && <span className="badge badge-red">终态（不可再出发）</span>}
            </div>

            {/* 时间轴 */}
            <div style={{ display: 'flex', gap: 4, marginTop: 14, flexWrap: 'wrap' }}>
              {STATES.map((state) => {
                const step = (data.timeline ?? []).find((t) => t.state === state)
                const isCurrent = step?.current
                return (
                  <div
                    key={state}
                    style={{
                      padding: '6px 10px',
                      borderRadius: 6,
                      fontSize: 12,
                      border: `1px solid ${isCurrent ? 'var(--primary)' : 'var(--border)'}`,
                      background: isCurrent ? 'var(--primary-soft)' : 'var(--bg-subtle)',
                      color: isCurrent ? 'var(--primary)' : 'var(--text-muted)',
                      fontWeight: isCurrent ? 600 : 400,
                    }}
                  >
                    {STATE_LABEL[state] ?? state}
                  </div>
                )
              })}
            </div>

            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 12, marginBottom: 0 }}>
              合法下一步：
              {(data.legalNext ?? []).length ? (
                (data.legalNext ?? []).map((s) => (
                  <code key={s} style={{ marginLeft: 6 }}>{s}</code>
                ))
              ) : (
                <span style={{ marginLeft: 6 }}>（无——终态）</span>
              )}
            </p>
          </div>

          {/* 恒定安全声明 */}
          <div className="card">
            <h3 style={{ marginTop: 0, fontSize: 14 }}>两项安全闸门（不随状态变化）</h3>
            <table className="table">
              <thead>
                <tr>
                  <th>闸门</th>
                  <th style={{ width: 120 }}>当前</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>
                    允许 AI 自行发起新动作
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      内部代号：<code>ai_new_actions_enabled</code>
                    </div>
                  </td>
                  <td>
                    <span className="badge badge-gray">
                      {release.ai_new_actions_enabled ? '已放开' : '未放开'}
                    </span>
                  </td>
                </tr>
                <tr>
                  <td>
                    控制面写入已被禁止
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      内部代号：<code>control_write_disabled</code>
                    </div>
                  </td>
                  <td>
                    <span className="badge badge-gray">
                      {release.control_write_disabled ? '已禁止' : '未禁止'}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div className="alert alert-info" style={{ marginTop: 12 }}>
              {plain(
                '这两项**不会**因为发布推进到「生效」就自动放开。' +
                  '要放开得等现场许可与安全互锁，不是这个状态机能决定的事。' +
                  '所以界面不会把它们显示成"已放开"——那等于替系统承诺它没承诺的事。',
              )}
            </div>
          </div>
        </>
      )}
    </>
  )
}
