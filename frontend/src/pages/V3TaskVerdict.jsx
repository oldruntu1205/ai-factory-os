import React, { useState } from 'react'
import { Link } from 'react-router-dom'
import PageHeader from '../components/PageHeader'
import { useV3Data } from '../useV3Data'
import { clearCache, postJson } from '../v3'
import { plain, describeAction } from '../v3Terms'

/**
 * 任务结论（V3.0）。
 *
 * ## 这一页存在的唯一理由
 *
 * V2.1 的「驾驶舱」答不了这个问题：**我这单到底成功没有、过账没有。**
 * V3.0 把这件事拆成三段事实（任务状态 / 命令状态 / **权威回执**），
 * 并且**只有权威回执能使"已过账"成立**（上游 §6）。
 *
 * 所以本页**不自己判断**结论——结论来自服务端的 `deriveDisplayStatus`，
 * 页面只负责如实呈现，并把三段事实的来源与时间一起摆出来。
 *
 * ## 功能
 *
 * - **我的任务**：列出当前身份最近提交的任务（服务端按租户过滤）；
 * - **查结论**：按 task_id 查一条的完整结论；
 * - **提交任务**：受理即 202，**不代表完成**。
 */
const OUTCOME_STYLE = {
  POSTED: { badge: 'badge-green', hint: '已过账：拿到了权威回执' },
  NOT_POSTED: { badge: 'badge-yellow', hint: '未过账：还没有权威回执' },
  FAILED: { badge: 'badge-red', hint: '失败' },
  NEEDS_HUMAN: { badge: 'badge-purple', hint: '需要人工处理' },
  UNKNOWN: { badge: 'badge-gray', hint: '无法判定：需要查状态' },
  NOT_APPLICABLE: { badge: 'badge-gray', hint: '不适用' },
}

export default function V3TaskVerdict() {
  const [taskId, setTaskId] = useState('')
  const [inputRef, setInputRef] = useState('pmc.weekly-schedule')
  const [queried, setQueried] = useState('')
  const [created, setCreated] = useState(null)
  const [formError, setFormError] = useState(null)
  const [busy, setBusy] = useState(false)

  const list = useV3Data('/v3-user/tasks')
  const { data, error, errorText, busy: loading, reload } = useV3Data(
    queried ? `/v3-user/task?task_id=${encodeURIComponent(queried)}` : null,
    { skip: !queried },
  )

  async function submit() {
    setBusy(true)
    setFormError(null)
    setCreated(null)
    try {
      const body = await postJson('/v3-user/task-create', { input_ref: inputRef })
      clearCache()
      setCreated(body)
      setTaskId(body.task_id)
      setQueried(body.task_id)
      list.reload('/v3-user/tasks')
    } catch (e) {
      setFormError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const display = data?.display

  return (
    <>
      <PageHeader
        title="我的单据"
        when="想知道某张单子办成没有、账上记了没有时来这里。"
        subtitle="结论以源系统回执为准，不凭猜测"
      />

      {/* ── 我的任务 ───────────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h3 style={{ margin: 0, fontSize: 14 }}>我的任务</h3>
          <button
            className="btn btn-secondary btn-sm"
            style={{ marginLeft: 'auto' }}
            onClick={() => list.reload('/v3-user/tasks')}
            disabled={list.busy}
          >
            {list.busy ? '刷新中…' : '刷新'}
          </button>
        </div>

        {list.error && <div className="alert alert-error" style={{ marginTop: 10 }}>{list.errorText}</div>}

        {list.data && (
          <>
            {list.data.tasks?.length ? (
              <table className="table" style={{ marginTop: 10 }}>
                <thead>
                  <tr>
                    <th>任务编号</th>
                    <th>要办的事</th>
                    <th>当前状态</th>
                    <th>执行单</th>
                    <th>最后更新</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {list.data.tasks.map((task) => (
                    <tr key={task.task_id}>
                      <td>
                        <code style={{ fontSize: 12 }}>{task.task_id}</code>
                      </td>
                      <td>
                        {/* 这一列原先是 `input_ref`——而**任务记录里根本没有这个字段**
                            （实测确认），所以它一直显示"—"。改成从任务名下的命令里
                            读出"这是什么操作 + 哪张单"。 */}
                        {task.business ? (
                          <div>
                            <div style={{ fontSize: 13 }}>
                              {describeAction(task.business.command_type).title}
                            </div>
                            <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                              {task.business.object_id ? (
                                <>编号 <code style={{ fontSize: 11 }}>{task.business.object_id}</code></>
                              ) : (
                                '—'
                              )}
                            </div>
                          </div>
                        ) : (
                          <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                            仅受理，还没有具体动作
                          </span>
                        )}
                      </td>
                      <td>
                        <span className="badge badge-blue">{task.status}</span>
                      </td>
                      <td style={{ fontSize: 12 }}>
                        {task.command_count === null ? (
                          // null ≠ 0：读不到命令账本与"确实没有命令"是两件事。
                          // 显示成 0 会让人以为已执行的单没有副作用。
                          <span style={{ color: 'var(--text-muted)' }} title="执行单账本暂不可读">
                            —
                          </span>
                        ) : task.command_count === 0 ? (
                          <span style={{ color: 'var(--text-muted)' }}>0</span>
                        ) : (
                          <span className="badge badge-purple">{task.command_count}</span>
                        )}
                      </td>
                      <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                        {task.updated_at ?? '—'}
                      </td>
                      <td>
                        <button
                          className="btn btn-secondary btn-sm"
                          onClick={() => {
                            setTaskId(task.task_id)
                            setQueried(task.task_id)
                          }}
                        >
                          看结论
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p style={{ color: 'var(--text-muted)', marginTop: 10, marginBottom: 0 }}>
                还没有任务。用下面的「提交任务」建一条。
              </p>
            )}
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10, marginBottom: 0 }}>
              {list.data.note}
            </p>
          </>
        )}
      </div>

      {/* ── 按编号查 + 新建 ────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="form-group">
          <label>任务编号</label>
          <input
            value={taskId}
            onChange={(e) => setTaskId(e.target.value)}
            placeholder="从上面的列表里复制，如 task-xxxxxxxx"
            onKeyDown={(e) => e.key === 'Enter' && setQueried(taskId.trim())}
          />
        </div>
        <button
          className="btn btn-primary"
          onClick={() => setQueried(taskId.trim())}
          disabled={loading || !taskId.trim()}
        >
          {loading ? '查询中…' : '查询结论'}
        </button>

        <hr style={{ margin: '14px 0', border: 0, borderTop: '1px solid var(--border)' }} />

        <div className="form-group">
          <label>{plain('新建一条任务（系统受理即返回，**不代表已做完**）')}</label>
          <input value={inputRef} onChange={(e) => setInputRef(e.target.value)} placeholder="内容标识，如 pmc.weekly-schedule" />
        </div>
        <button className="btn btn-secondary" onClick={submit} disabled={busy}>
          {busy ? '提交中…' : '提交任务'}
        </button>
      </div>

      {formError && <div className="alert alert-error">{formError}</div>}

      {created && (
        <div className="alert alert-info">
          {plain('任务已受理（**这不表示已完成**）。')}编号 <code>{created.task_id}</code>
        </div>
      )}

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

      {display && (
        <div className="card">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span className={`badge ${(OUTCOME_STYLE[display.outcome] ?? {}).badge ?? 'badge-gray'}`}>
              {display.label}
            </span>
            <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>
              {(OUTCOME_STYLE[display.outcome] ?? {}).hint ?? ''}
            </span>
            {display.authoritative ? (
              <span className="badge badge-green">结论已收到源系统确认</span>
            ) : (
              <span className="badge badge-gray">结论尚未收到源系统确认</span>
            )}
            <button
              className="btn btn-secondary btn-sm"
              style={{ marginLeft: 'auto' }}
              onClick={() => reload()}
              disabled={loading}
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>

          {display.reason && <p style={{ marginTop: 10, marginBottom: 0 }}>{display.reason}</p>}

          <h3 style={{ fontSize: 14, margin: '16px 0 6px' }}>判定依据（三个方面分别核对）</h3>
          <table className="table">
            <thead>
              <tr>
                <th>核对项</th>
                <th>结果</th>
                <th>由谁给出</th>
                <th>最后更新</th>
              </tr>
            </thead>
            <tbody>
              {(display.sources ?? []).map((s) => (
                <tr key={s.name}>
                  <td>
                    {{
                      task: '任务受理',
                      command: '执行单',
                      receipt: '源系统确认凭证',
                    }[s.name] ?? s.name}
                  </td>
                  <td>
                    {s.status ? (
                      <code>{plain(s.status)}</code>
                    ) : (
                      <span style={{ color: 'var(--text-muted)' }}>—</span>
                    )}
                  </td>
                  <td>{s.source ?? '—'}</td>
                  <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>{s.updatedAt ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="alert alert-info" style={{ marginTop: 12 }}>
            <strong>为什么「未过账」不等于出错：</strong>
            {plain(
              '「已过账」必须拿到**源系统的凭证**才算数。没拿到凭证时，' +
                '这里如实显示「未过账」——这比显示一个好看的"成功"要安全，' +
                '因为最危险的情况正是**界面显示成功、而 ERP 里其实没记账**。',
            )}
          </div>

          {data.commandId ? (
            // 执行单编号收进折叠区：客户不认这串 ID，审计与排障时才需要。
            <details style={{ marginTop: 10 }}>
              <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-muted)' }}>
                技术详情（审计与排障用）
                {data.commandCount > 1 && (
                  <span style={{ marginLeft: 6 }}>（本任务共 {data.commandCount} 张执行单）</span>
                )}
              </summary>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 2, paddingLeft: 14 }}>
                <div>
                  执行单编号：<code>{data.commandId}</code>
                </div>
                {data.commandCount > 1 && <div>此处只显示最早一张。</div>}
              </div>
            </details>
          ) : (
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>
              {plain(
                '这条任务还没有对应的执行单，所以「未过账」的意思是：**还没有提交执行**，' +
                  '而不是执行失败。',
              )}
            </p>
          )}
        </div>
      )}
    </>
  )
}
