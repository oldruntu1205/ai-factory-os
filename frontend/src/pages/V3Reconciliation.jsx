import React, { useState } from 'react'
import { Link } from 'react-router-dom'
import PageHeader from '../components/PageHeader'
import { useV3Data } from '../useV3Data'
import { plain } from '../v3Terms'

/**
 * 把内部动作代号翻译成客户能懂的话。
 *
 * 客户不需要知道 `RESEND_NEW_KEY` 这个常量名，但**必须知道它会造成什么后果**——
 * 因为这一页的用途就是"出错时别做错事"。所以标题说后果，
 * 内部代号降级成一行小字，留给排障与审计对齐用。
 */
function prohibitedTitle(action) {
  const titles = {
    RESEND_NEW_KEY: '换一个标记重发',
    DIRECT_COMPENSATION: '直接做冲销补偿',
  }
  return titles[action] ?? action
}

function prohibitedWhy(action) {
  const reasons = {
    RESEND_NEW_KEY: '会让同一笔业务真的执行两次。',
    DIRECT_COMPENSATION: '可能把已经成功的单再冲一次，造成账实不符。',
  }
  return reasons[action] ?? ''
}

/**
 * 待对账（V3.0）：UNKNOWN 收敛记录。
 *
 * ## 这一页是"出问题时"才需要的，所以重点不是好看
 *
 * UNKNOWN 的含义是**结果未知，必须查状态**。它最要紧的两件事：
 *
 * 1. **禁止动作清单放最显眼**——它是**防误操作的清单**。
 *    上游把「换新键重发」「直接补偿」定成恒定禁止项，正是为了让误操作没有借口。
 * 2. **"没有收敛记录"是一种状态，不是错误**。它表示"这单还没进过 UNKNOWN 收敛"。
 *    若把它当错误弹红，业务用户会去查一个本来就不该有记录的订单——
 *    **把正常状态报成异常，比不报更糟。**
 */
export default function V3Reconciliation() {
  const [commandId, setCommandId] = useState('')
  const [queried, setQueried] = useState('')

  const { data, error, errorText, busy, reload } = useV3Data(
    queried ? `/v3-user/reconciliation?command_id=${encodeURIComponent(queried)}` : null,
    { skip: !queried },
  )

  return (
    <>
      <PageHeader
        title="异常处理"
        when="只有在「我的单据」里看到结果未知时才来这里。平时不需要看这一页。"
        whenKind="warn"
        subtitle="出问题时告诉你：能做什么、不能做什么"
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="form-group">
          <label>执行单编号</label>
          <input
            value={commandId}
            onChange={(e) => setCommandId(e.target.value)}
            placeholder="从「任务结论」页复制，如 command-xxxxxxxx"
            onKeyDown={(e) => e.key === 'Enter' && setQueried(commandId.trim())}
          />
        </div>
        <button
          className="btn btn-primary"
          onClick={() => setQueried(commandId.trim())}
          disabled={busy || !commandId.trim()}
        >
          {busy ? '查询中…' : '查询对账记录'}
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

      {/* 「没有记录」= 一种状态，**不是错误** */}
      {data?.noRecord && (
        <div className="alert alert-info">
          <strong>尚未进入对账。</strong>
          这条命令没有收敛记录（后端 <code>{data.code}</code>）。
          <div style={{ marginTop: 4, fontSize: 12, color: 'var(--text-muted)' }}>
            这说明它还<strong>没有</strong>因为超时／失联而进入 UNKNOWN 收敛。
            一旦进入，这里会列出源系统结论与禁止动作。
          </div>
        </div>
      )}

      {data?.error && <div className="alert alert-error">{data.error.message}</div>}

      {data && !data.noRecord && !data.error && (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
              <span
                className={
                  data.sourceOutcome === 'SUCCEEDED'
                    ? 'badge badge-green'
                    : data.sourceOutcome === 'FAILED'
                      ? 'badge badge-red'
                      : 'badge badge-gray'
                }
              >
                源系统结论：{data.sourceOutcome ?? '未知'}
              </span>
              <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
                查证次数 {data.queryAttempts ?? '—'}
              </span>
              {data.queryVerified !== true && (
                <span className="badge badge-yellow">未查证：结论不可当依据</span>
              )}
              <button
                className="btn btn-secondary btn-sm"
                style={{ marginLeft: 'auto' }}
                onClick={() => reload()}
                disabled={busy}
              >
                {busy ? '刷新中…' : '刷新'}
              </button>
            </div>
          </div>

          {/* ① 禁止动作：最显眼 */}
          {(data.prohibited ?? []).length > 0 && (
            <div className="card" style={{ marginBottom: 16, borderLeft: '4px solid #b91c1c' }}>
              <h3 style={{ marginTop: 0, fontSize: 14, color: '#b91c1c' }}>
                ⛔ 这些操作在任何情况下都不能做
              </h3>
              <ul style={{ margin: '8px 0 0 18px' }}>
                {(data.prohibited ?? []).map((action) => (
                  <li key={action} style={{ marginBottom: 4 }}>
                    <strong style={{ fontSize: 13 }}>{prohibitedTitle(action)}</strong>
                    <span style={{ color: 'var(--text-muted)', marginLeft: 8, fontSize: 12 }}>
                      {prohibitedWhy(action)}
                    </span>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      内部代号：<code>{action}</code>
                    </div>
                  </li>
                ))}
              </ul>
              <div className="alert alert-info" style={{ marginTop: 10 }}>
                {plain(
                  '这不是建议，而是系统的**恒定判定**：' +
                    '界面上也不会提供对应的按钮，所以想误操作也做不到。',
                )}
              </div>
            </div>
          )}

          {/* ② 候选动作 */}
          {(data.candidates ?? []).length > 0 && (
            <div className="card" style={{ marginBottom: 16 }}>
              <h3 style={{ marginTop: 0, fontSize: 14 }}>可以做的事</h3>
              <ul style={{ margin: '8px 0 0 18px' }}>
                {(data.candidates ?? []).map((action) => (
                  <li key={typeof action === 'string' ? action : JSON.stringify(action)}>
                    <code>{typeof action === 'string' ? action : JSON.stringify(action)}</code>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ③ 重试约束 */}
          <div className="card">
            <h3 style={{ marginTop: 0, fontSize: 14 }}>如果决定重试，必须满足</h3>
            <table className="table">
              <tbody>
                <tr>
                  <td style={{ width: 220, color: 'var(--text-muted)' }}>
                    {plain('沿用**同一个防重复标记**')}
                  </td>
                  <td>
                    {data.retryKey ? (
                      <>
                        <code>{data.retryKey}</code>
                        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                          沿用它可以保证：即使重试，也不会真的重复执行两次。
                        </div>
                      </>
                    ) : (
                      <span>—（当前不允许重试）</span>
                    )}
                  </td>
                </tr>
                <tr>
                  <td style={{ color: 'var(--text-muted)' }}>必须重新做一次执行前复核</td>
                  <td>
                    {data.retryNeedsFreshReview === true ? (
                      <span className="badge badge-yellow">是（不能沿用上一次的批准结论）</span>
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}
