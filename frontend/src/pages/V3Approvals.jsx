import React, { useCallback, useEffect, useState } from 'react'
import PageHeader from '../components/PageHeader'
import { getJson, postJson, clearCache } from '../v3'
import {
  plain,
  fieldLabel,
  describeAction,
  describeRefusal,
  actionTitle,
  versionLabel,
} from '../v3Terms'

/**
 * 批准（V3.0 用户侧写操作）。
 *
 * ## 这一页解决什么
 *
 * 在此之前 V3.0 的四个用户侧页面**全是只读**：能看结论、能看能力、能看台账，
 * 但不能做任何事。上游 §6.2 的受控写入链要求「发起 → 建立批准绑定 → **批准**
 * → 持久化命令」，其中**批准**必须由**另一个人**作出（§7 职责分离）。
 * 所以"能批准"是这条链路上真正缺的那一环。
 *
 * ## 三个刻意的设计
 *
 * ### 1. 一个页面、两个角色，靠**身份**区分，不靠开关
 *
 * 「我发起的」与「待我批准」都在这一页，但服务端按**登录身份解析出的 actor**
 * 决定谁能批。页面**不做权限判断**——它只把服务端给的结论呈现出来。
 * 如果页面自己判"我不是审批人所以隐藏按钮"，那只是装饰：
 * 真正的拦截必须在服务端（那一条已经有测试）。
 *
 * ### 2. 「自己批自己」要**说清为什么不行**，而不是把 403 原样抛出
 *
 * 发起人点批准会被服务端拒绝（`DUTIES_NOT_SEPARATED`）。这是**正确行为**，
 * 但对用户来说像个 bug。所以这里把该原因翻译成人话，并给出**下一步该做什么**。
 *
 * ### 3. 批准 ≠ 已执行
 *
 * 批准成立只意味着"可以执行了"。真正是否过账要看**权威回执**（上游 §6），
 * 那是「任务结论」页的事。这里绝不写"已完成"。
 */
export default function V3Approvals() {
  const [pending, setPending] = useState(null)
  const [listError, setListError] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [executingId, setExecutingId] = useState(null)
  const [results, setResults] = useState({})
  const [execResults, setExecResults] = useState({})
  // 默认选**能真正走完执行**的那个工具：`erp.inventory.reserve` 三项能力位齐备。
  // `pmc.dispatch.publish` 缺补偿能力，批准后会被执行前复核拒（界面上会说明）。
  const [form, setForm] = useState({ object_id: '', tool_id: 'erp.inventory.reserve', expected_version: '1' })
  const [creating, setCreating] = useState(false)
  const [createResult, setCreateResult] = useState(null)

  const load = useCallback(async () => {
    setListError(null)
    try {
      const body = await getJson('/v3-user/approvals', { force: true })
      setPending(body)
    } catch (e) {
      setListError(e.message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function decide(item, approved) {
    setBusyId(item.approval_id)
    try {
      // 批准必须**逐项绑定**到具体对象与 payload 哈希（上游 §6.2）。
      // 这些值从服务端给的待批准记录里带过来——不让人凭印象填写。
      const body = await postJson('/v3-user/approval-decision', {
        approval_id: item.approval_id,
        approved,
        object_id: item.object_id,
        expected_version: item.expected_version,
        tool_id: item.tool_id,
        tool_version: item.tool_version,
        command_type: item.command_type,
        action_scope: item.action_scope,
        payload_hash: item.payload_hash,
        payload_ref: item.payload_ref,
        normalization_ref: item.normalization_ref,
        policy_version: item.policy_version,
        valid_until: item.valid_until,
      })
      setResults((prev) => ({ ...prev, [item.approval_id]: body }))
      clearCache()
      await load()
    } catch (e) {
      // ⚠️ **必须把 `code` 一起留下**（2026-10-06 修的 bug）。
      //
      // `postJson` 在非 2xx 时只抛出 `Error(message)`，`code` 挂在 error 对象上。
      // 第一版这里只存了 `{ ok:false, error: e.message }` ⇒ 下游 `describeRefusal`
      // 拿不到 `DUTIES_NOT_SEPARATED` ⇒ **翻译（含"下一步该怎么办"）从来没跑过**，
      // 用户看到的是服务端原文。实测：截图里显示的就是原始终点文本。
      setResults((prev) => ({
        ...prev,
        [item.approval_id]: {
          ok: false,
          error: e.message,
          code: e.code ?? null,
          status: e.status ?? null,
          message: e.message,
        },
      }))
    } finally {
      setBusyId(null)
    }
  }

  /**
   * 批准之后**推进执行**。
   *
   * 批准只是"可以执行了"，执行是另一个动作——所以是两个按钮，不是自动串联。
   * 这样做还有一个实际好处：复核不通过时（如缺补偿能力），用户能看到
   * **是哪一项没满足**，而不是批准后一片安静。
   */
  async function runExecution(approvalId) {
    setExecutingId(approvalId)
    try {
      const body = await postJson('/v3-user/execute', { approval_id: approvalId })
      setExecResults((prev) => ({ ...prev, [approvalId]: body }))
      clearCache()
    } catch (e) {
      setExecResults((prev) => ({ ...prev, [approvalId]: { ok: false, error: e.message } }))
    } finally {
      setExecutingId(null)
    }
  }

  async function createWrite() {
    setCreating(true)
    setCreateResult(null)
    try {
      const body = await postJson('/v3-user/write-request', {
        object_id: form.object_id,
        tool_id: form.tool_id,
        expected_version: Number(form.expected_version) || 0,
      })
      setCreateResult(body)
      clearCache()
      await load()
    } catch (e) {
      setCreateResult({ ok: false, error: e.message })
    } finally {
      setCreating(false)
    }
  }

  const identity = pending?.identity
  const myActor = identity?.actor_id ?? null

  /**
   * 这条是不是**我自己发起的**。
   *
   * 判据是服务端给的 `requester_actor`（受委托执行人）与当前登录身份比较——
   * **不在前端猜**：前端不知道"谁是谁"，猜错就会把该别人批的单显示成自己能批。
   */
  function isMine(item) {
    return Boolean(myActor && item.requester_actor && item.requester_actor === myActor)
  }

  return (
    <>
      <PageHeader
        title="待我批准"
        when="有人提交了需要你点头的申请时来这里。它不会自己跑，必须有人批准。"
        subtitle="批准之后才执行——而且不能批自己发起的"
      />

      {/* 当前身份：说清「我是谁」，因为整页的行为都由它决定 */}
      {identity && (
        <div className="card" style={{ marginBottom: 16, padding: '10px 14px' }}>
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            当前身份：
            <code style={{ marginLeft: 6 }}>{identity.actor_id}</code>
            <span style={{ marginLeft: 10 }}>{identity.user_email}</span>
            <span style={{ marginLeft: 10 }}>租户 {identity.tenant_id}</span>
          </span>
        </div>
      )}

      {/* ── 待批准的申请 ───────────────────────────────────────── */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h3 style={{ margin: 0, fontSize: 14 }}>等我处理的申请</h3>
          {pending && <span className="badge badge-blue">{pending.total ?? 0}</span>}
          <button
            className="btn btn-secondary btn-sm"
            style={{ marginLeft: 'auto' }}
            onClick={load}
          >
            刷新
          </button>
        </div>

        {listError && <div className="alert alert-error" style={{ marginTop: 10 }}>{listError}</div>}

        {pending && pending.approvals?.length === 0 && (
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 10 }}>
            {pending.note}
          </p>
        )}

        {pending?.approvals?.length > 0 && (
          <table className="table" style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th style={{ width: '32%' }}>要做的操作</th>
                <th>针对哪张单</th>
                <th>批准有效期至</th>
                <th style={{ width: 150 }} />
              </tr>
            </thead>
            <tbody>
              {pending.approvals.map((item) => {
                const result = results[item.approval_id]
                const action = describeAction(item.tool_id)
                const isSelfRequested = isMine(item)
                return (
                  <React.Fragment key={item.approval_id}>
                    <tr>
                      <td>
                        <div style={{ fontWeight: 600 }}>{action.title}</div>
                        {action.why && (
                          <div
                            style={{
                              fontSize: 12,
                              color: 'var(--text-muted)',
                              marginTop: 4,
                              lineHeight: 1.6,
                            }}
                          >
                            {action.why}
                          </div>
                        )}
                      </td>
                      <td>
                        {/* 业务对象：**先给客户认识的那部分**（这是什么操作、哪张单），
                            不认识的系统编号收进折叠区。 */}
                        <BusinessObject business={item.business} fallback={item} />
                      </td>
                      <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                        {item.valid_until ?? '—'}
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        {isSelfRequested ? (
                          // **发起人不该看到"批准"按钮**：系统要求审批人不是发起人，
                          // 他点下去必然被拒。与其让人撞一次墙，不如一开始就说清
                          // "这条要别人批"——按钮摆在那里本身就是误导。
                          <span
                            style={{ fontSize: 12, color: 'var(--text-muted)' }}
                            title="职责分离：审批人不得是发起人"
                          >
                            你发起的，需他人审批
                          </span>
                        ) : (
                          <>
                            <button
                              className="btn btn-primary btn-sm"
                              onClick={() => decide(item, true)}
                              disabled={busyId === item.approval_id}
                            >
                              批准
                            </button>{' '}
                            <button
                              className="btn btn-secondary btn-sm"
                              onClick={() => decide(item, false)}
                              disabled={busyId === item.approval_id}
                            >
                              否决
                            </button>
                          </>
                        )}
                        {/* 批准之后才能推进执行——所以这个按钮**只在批准成功后出现**，
                            而不是一开始就摆在那里让人误以为可以跳过批准。 */}
                        {(results[item.approval_id]?.ok || execResults[item.approval_id]) && (
                          <>
                            {' '}
                            <button
                              className="btn btn-primary btn-sm"
                              onClick={() => runExecution(item.approval_id)}
                              disabled={executingId === item.approval_id}
                            >
                              {executingId === item.approval_id ? '执行中…' : '继续执行'}
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                    {isSelfRequested && (
                      <tr>
                        <td colSpan={4} style={{ paddingTop: 0 }}>
                          <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.8 }}>
                            {plain(
                              '这条是你自己发起的申请。按**职责分离**（上游 §7），' +
                                '发起人不能批准自己的申请——请让另一位有批准权限的同事处理。',
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                    <tr>
                      <td colSpan={4} style={{ paddingTop: 0 }}>
                        {/* 技术详情：**默认收起**。审计与排障时需要，
                            但客户批东西时不需要看到哈希。 */}
                        <details>
                          <summary
                            style={{
                              cursor: 'pointer',
                              fontSize: 12,
                              color: 'var(--text-muted)',
                              padding: '4px 0',
                            }}
                          >
                            技术详情（审计与排障用）
                          </summary>
                          <div
                            style={{
                              fontSize: 12,
                              color: 'var(--text-muted)',
                              lineHeight: 2,
                              paddingLeft: 14,
                            }}
                          >
                            <div>
                              {fieldLabel('approval_id')}：<code>{item.approval_id}</code>
                            </div>
                            <div>
                              {fieldLabel('tool_id')}：<code>{item.tool_id ?? '—'}</code>
                            </div>
                            <div>
                              {fieldLabel('object_id')}：<code>{item.object_id ?? '—'}</code>
                            </div>
                            <div>
                              {fieldLabel('expected_version')}：{item.expected_version ?? '—'}
                            </div>
                            <div>
                              {fieldLabel('policy_version')}：<code>{item.policy_version ?? '—'}</code>
                            </div>
                            <div>
                              {fieldLabel('payload_hash')}：
                              <code>{item.payload_hash ?? '—'}</code>
                            </div>
                            {item.normalization_ref && (
                              <div>
                                规范化引用：<code>{item.normalization_ref}</code>
                              </div>
                            )}
                            <div>
                              跟踪号：<code>{item.trace_id ?? '—'}</code>
                            </div>
                          </div>
                        </details>
                      </td>
                    </tr>
                    {result && (
                      <tr>
                        <td colSpan={4} style={{ background: 'var(--bg-subtle, transparent)' }}>
                          <DecisionNote result={result} />
                        </td>
                      </tr>
                    )}
                    {execResults[item.approval_id] && (
                      <tr>
                        <td colSpan={4} style={{ background: 'var(--bg-subtle, transparent)' }}>
                          <ExecutionNote result={execResults[item.approval_id]} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* ── 发起一次需要批准的写入 ──────────────────────────────── */}
      <div className="card">
        <h3 style={{ margin: 0, fontSize: 14 }}>发起一次需要批准的申请</h3>
        <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6, lineHeight: 1.7 }}>
          {plain(
            '提交后**不会马上执行**，需要另一位同事批准。' +
              '系统会自动记录这次申请的内容指纹与规则版本，确保批准的就是将要执行的那一份。',
          )}
        </p>

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 12, alignItems: 'flex-end' }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>业务对象编号</span>
            <input
              className="input"
              style={{ width: 220 }}
              value={form.object_id}
              onChange={(e) => setForm({ ...form, object_id: e.target.value })}
              placeholder="如 仓库3-原料A-2026W41"
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              你自己填的业务对象号（订单号 / 物料号 / 排产周次都行），批准后靠它核对。
            </span>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>要执行的操作</span>
            <select
              className="input"
              style={{ width: 280 }}
              value={form.tool_id}
              onChange={(e) => setForm({ ...form, tool_id: e.target.value })}
            >
              <option value="erp.inventory.reserve">预留库存（需批准，可完整执行）</option>
              <option value="pmc.dispatch.publish">发布生产排产计划（需批准，但暂不具备执行条件）</option>
              <option value="erp.stock.query">查询库存（只读，无需批准）</option>
            </select>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>对象第几版</span>
            <input
              className="input"
              style={{ width: 90 }}
              value={form.expected_version}
              onChange={(e) => setForm({ ...form, expected_version: e.target.value })}
            />
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
              防止批准期间对象被别人改过。
            </span>
          </label>
          <button
            className="btn btn-primary"
            onClick={createWrite}
            disabled={creating || !form.object_id}
          >
            {creating ? '发起中…' : '提交申请'}
          </button>
        </div>

        {createResult && (
          <div style={{ marginTop: 12 }}>
            {createResult.ok ? (
              <div className="alert" style={{ background: 'var(--bg-subtle, transparent)' }}>
                <div style={{ fontSize: 13 }}>
                  <strong>申请已提交，等另一位同事批准</strong>
                  <span style={{ color: 'var(--text-muted)' }}>（现在还没有执行）</span>
                </div>
                <div style={{ fontSize: 12, marginTop: 8, lineHeight: 1.9 }}>
                  <div>
                    👉 下一步：请另一位有批准权限的同事登录后来处理这一条。
                  </div>
                  <div>
                    要执行的操作：<strong>{actionTitle(createResult.tool_id)}</strong>
                    {createResult.risk_class && (
                      <span style={{ color: 'var(--text-muted)' }}>
                        （风险等级 {createResult.risk_class}）
                      </span>
                    )}
                  </div>
                  <div>
                    {plain(
                      createResult.approval_required
                        ? '系统判定：**必须批准后才能执行**。'
                        : '系统判定：这一次**无需批准**，可直接进入执行。',
                    )}
                  </div>
                  {/* 准入结论：**提早就说清"这次能不能真的执行"**。
                      让人白批一张票比不让他批更糟，所以缺什么当场讲。 */}
                  {createResult.admission?.known && (
                    <div style={{ marginTop: 4 }}>
                      {createResult.admission.unattended_high_risk_allowed ? (
                        <span style={{ color: 'var(--success, #2e7d32)' }}>
                          ✓ 已具备执行条件（幂等、状态查询、补偿能力齐备）。
                        </span>
                      ) : (
                        <span style={{ color: 'var(--danger, #c00)' }}>
                          ⚠ 暂不具备执行条件：缺 {createResult.admission.missing_capabilities
                            ?.map(capabilityLabel)
                            .join('、')}
                          。即使批准，执行前复核也会拦下。
                        </span>
                      )}
                    </div>
                  )}
                  {createResult.admission && !createResult.admission.known && (
                    <div style={{ marginTop: 4, color: 'var(--text-muted)' }}>
                      {createResult.admission.note}
                    </div>
                  )}
                </div>

                <details style={{ marginTop: 8 }}>
                  <summary
                    style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-muted)' }}
                  >
                    技术详情（审计与排障用）
                  </summary>
                  <div
                    style={{
                      fontSize: 12,
                      color: 'var(--text-muted)',
                      lineHeight: 2,
                      paddingLeft: 14,
                    }}
                  >
                    <div>
                      {fieldLabel('approval_id')}：<code>{createResult.approval_id}</code>
                    </div>
                    <div>
                      {fieldLabel('task_id')}：<code>{createResult.task_id}</code>
                      {createResult.task_created && '（系统自动建立）'}
                    </div>
                    <div>
                      {fieldLabel('tool_id')}：<code>{createResult.tool_id}</code>
                    </div>
                    <div>
                      {fieldLabel('payload_hash')}：
                      <code>{createResult.payload_hash ?? '—'}</code>
                    </div>
                    <div>
                      {fieldLabel('policy_version')}：<code>{createResult.policy_version ?? '—'}</code>
                    </div>
                    <div>
                      允许执行：{String(createResult.allowed)}　需批准：
                      {String(createResult.approval_required)}
                    </div>
                  </div>
                </details>
              </div>
            ) : (
              <div className="alert alert-error">
                {createResult.message || createResult.error || '提交失败'}
                {createResult.code && (
                  <div style={{ fontSize: 11, marginTop: 4, opacity: 0.7 }}>
                    错误代码：{createResult.code}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}

/**
 * 能力位 → 客户能懂的说法。
 *
 * 客户不需要知道 `compensation` 这个字段名，但**必须知道缺的是"出事后能不能补救"**
 * ——那才是他判断"这单能不能放开跑"的依据。
 */
function capabilityLabel(bit) {
  return (
    {
      idempotency_support: '防重复执行的能力',
      status_query: '查询执行结果的能力',
      compensation: '出问题后的补救（补偿）能力',
    }[bit] ?? bit
  )
}

/**
 * 业务对象单元格：**先给客户认得的，再把认不得的收起来**。
 *
 * ## 这里的诚实边界（取证得出的，不是我保守）
 *
 * 我查过实际数据：命令里只有 `object_id = "schedule-ok-d45259"` 这类**技术编号**，
 * **没有「41 周排产计划」这种客户认得的名字**。那个名字应当由 ERP/PMC 提供，
 * 而上游把「真实外部字段映射」列为**待冻结的外部依赖**。
 *
 * 所以这里**不编名字**，而是：
 * 1. 主行放**客户能懂的两件事**：这是什么操作（如"预留库存"）+ 针对哪张单（编号）；
 * 2. 业务名位置**显式留出并标"待接入"**——比编一个好看的名字安全得多，
 *    因为编造会让客户以为系统认识他的单子，而它并不认识；
 * 3. 技术编号与版本收进折叠区，审计与排障时再展开。
 */
function BusinessObject({ business, fallback }) {
  const commandType = business?.command_type ?? fallback?.tool_id
  const action = describeAction(commandType)
  const objectId = business?.object_id ?? fallback?.object_id
  const version = business?.expected_version ?? fallback?.expected_version

  return (
    <div>
      <div style={{ fontSize: 13 }}>{action.title}</div>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
        {objectId ? (
          <>
            {/* 说清这串编号**是客户自己填的业务对象号**，不是系统 ID——
                否则客户会以为"这是系统给的，跟我没关系"，从而放弃核对。 */}
            业务对象编号 <code style={{ fontSize: 12 }}>{objectId}</code>
          </>
        ) : (
          '（未知编号）'
        )}
        {version != null && <>　{versionLabel(version)}</>}
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
        {business?.name ? (
          <>业务名称：{business.name}</>
        ) : (
          // 说清"为什么这里没有名字"，而不是留白让人以为是 bug
          <span title={business?.name_unavailable_reason ?? '业务名称尚未接入'}>
            业务名称：待接入（需 ERP/PMC 提供）
          </span>
        )}
      </div>
    </div>
  )
}

/**
 * 把执行结果翻译成人话。
 *
 * 三种结局，各自的**下一步完全不同**：
 * - 派发成功 → 还在等源系统回执（**不是**已过账）；
 * - 复核不通过 → 是哪一项没过（多半是缺能力位），以及怎么办；
 * - 其他错误 → 如实转达。
 */
function ExecutionNote({ result }) {
  if (result.error) {
    return <div style={{ fontSize: 12, color: 'var(--danger, #c00)' }}>执行请求失败：{result.error}</div>
  }

  if (result.ok && result.already_done) {
    return (
      <div style={{ fontSize: 12, lineHeight: 1.9 }}>
        <strong>这条已经执行完成</strong>
        <div>
          过账状态：<strong>{result.posting_status === 'POSTED' ? '已过账' : '未过账'}</strong>
          {result.authority_receipt_ref && (
            <span style={{ color: 'var(--text-muted)' }}>（回执 {result.authority_receipt_ref}）</span>
          )}
        </div>
      </div>
    )
  }

  if (result.ok) {
    return (
      <div style={{ fontSize: 12, lineHeight: 1.9 }}>
        <div>
          <strong>已派发执行</strong>
          {result.status_after && (
            <span style={{ color: 'var(--text-muted)' }}>　当前状态 {result.status_after}</span>
          )}
        </div>
        <div style={{ color: 'var(--text-muted)' }}>
          这不是已过账——**要等源系统的回执确认**。到「任务结论」页看最终结论。
        </div>
        {result.command_id && (
          <details style={{ marginTop: 4 }}>
            <summary style={{ cursor: 'pointer', color: 'var(--text-muted)' }}>
              技术详情（审计与排障用）
            </summary>
            <div style={{ color: 'var(--text-muted)', lineHeight: 2, paddingLeft: 14 }}>
              <div>
                执行单：<code>{result.command_id}</code>
              </div>
              <div>派发结果：{String(result.dispatched)}</div>
            </div>
          </details>
        )}
      </div>
    )
  }

  if (result.code === 'PRE_EXECUTION_REVIEW_NOT_ALLOWED') {
    return (
      <div style={{ fontSize: 12, lineHeight: 1.9 }}>
        <div style={{ color: 'var(--danger, #c00)' }}>
          <strong>没有执行：执行前的最后一次核对没通过。</strong>
        </div>
        <div>
          {result.reason_ref === 'reason-capability-not-admitted'
            ? '这个操作目前不具备安全执行的条件——缺能力位，万一出错没法可靠地补救。'
            : `未通过的检查项：${(result.failed_checks ?? []).join('、') || result.reason_ref || '（未知）'}`}
        </div>
        <div style={{ color: 'var(--text-muted)' }}>
          <strong>下一步：</strong>
          换一个已具备执行条件的操作，或让管理员先补齐对应能力后再试。
        </div>
        {result.failed_checks?.includes('capabilities_admitted') && (
          <div style={{ color: 'var(--text-muted)' }}>
            缺的能力位见发起时给出的提示。
          </div>
        )}
      </div>
    )
  }

  return (
    <div style={{ fontSize: 12, lineHeight: 1.9 }}>
      <div style={{ color: 'var(--danger, #c00)' }}>
        <strong>{plain(result.message || '执行未成功')}</strong>
      </div>
      {result.code && (
        <div style={{ color: 'var(--text-muted)' }}>
          错误代码：<code>{result.code}</code>
          {result.current_status && `　当前状态 ${result.current_status}`}
        </div>
      )}
    </div>
  )
}

/**
 * 把批准决定的**服务端结论**翻译成人话。
 *
 * ## 为什么必须翻译
 *
 * 客户点"批准"后可能拿到 `DUTIES_NOT_SEPARATED` 这样的代码。那是**正确行为**
 * （系统在阻止"自己批自己"），但直接显示代码或 403，在客户看来就是**坏了**。
 *
 * 所以每种拒绝都要回答**三件事**：
 * ① 发生了什么（一句人话）；② 为什么这样设计（不是故障）；③ **我该怎么办**。
 *
 * 第三条最容易漏。只告诉用户"被拒绝"的界面，等于把问题丢回给他。
 * 判定逻辑集中在 `v3Terms.js`，此处只负责呈现。
 */
function DecisionNote({ result }) {
  // 优先走**拒绝码翻译**：它比服务端原文多了"为什么这样设计"和"我该怎么办"。
  // 只有在没有对应翻译时才退回原文。
  const refusal = result.ok ? null : describeRefusal(result.code)
  if (refusal) {
    return (
      <div style={{ fontSize: 12, lineHeight: 1.9 }}>
        <div style={{ color: 'var(--danger, #c00)' }}>
          <strong>{refusal.title}</strong>
        </div>
        <div>{refusal.because}</div>
        <div style={{ color: 'var(--text-muted)' }}>
          <strong>下一步：</strong>
          {refusal.next}
        </div>
        {result.code === 'DUTIES_NOT_SEPARATED' && (
          <div style={{ color: 'var(--text-muted)' }}>
            演示环境里可用的另一位审批人：<code>approver@demo.com</code>
          </div>
        )}
        <details style={{ marginTop: 4 }}>
          <summary style={{ cursor: 'pointer', color: 'var(--text-muted)' }}>
            技术详情（审计与排障用）
          </summary>
          <div style={{ color: 'var(--text-muted)', lineHeight: 2, paddingLeft: 14 }}>
            <div>
              错误代码：<code>{result.code}</code>
            </div>
            <div>{plain(result.message ?? result.error ?? '')}</div>
          </div>
        </details>
      </div>
    )
  }

  if (result.error) {
    return <div style={{ fontSize: 12, color: 'var(--danger, #c00)' }}>提交失败：{result.error}</div>
  }

  if (result.ok) {
    return (
      <div style={{ fontSize: 12, lineHeight: 1.8 }}>
        <div>
          <strong>{result.approved ? '已批准' : '已否决'}</strong>
          {result.approved && (
            <span style={{ color: 'var(--text-muted)' }}>
              　下一步：系统会按这份批准继续执行
            </span>
          )}
        </div>
        <details style={{ marginTop: 4 }}>
          <summary style={{ cursor: 'pointer', color: 'var(--text-muted)' }}>
            技术详情（审计与排障用）
          </summary>
          <div style={{ color: 'var(--text-muted)', lineHeight: 2, paddingLeft: 14 }}>
            <div>
              批准单编号：<code>{result.approval_id ?? '—'}</code>
            </div>
            <div>
              决定编号：<code>{result.decision_ref ?? '—'}</code>
            </div>
            <div>
              审批人：<code>{result.decided_by ?? '—'}</code>
              {result.record_version != null && `　记录版本 ${result.record_version}`}
            </div>
          </div>
        </details>
      </div>
    )
  }

  return (
    <div style={{ fontSize: 12, lineHeight: 1.9 }}>
      <div style={{ color: 'var(--danger, #c00)' }}>
        <strong>{plain(result.message || result.error || '操作未成功')}</strong>
      </div>
      {result.code && (
        <div style={{ color: 'var(--text-muted)' }}>
          错误代码：<code>{result.code}</code>
        </div>
      )}
    </div>
  )
}
