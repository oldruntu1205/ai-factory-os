import React from 'react'
import { Link } from 'react-router-dom'
import PageHeader from '../components/PageHeader'
import { useV3Data } from '../useV3Data'
import { plain } from '../v3Terms'

/**
 * AI 员工（V3.0）：能力与授权范围。
 *
 * ## 与 V2.1「AI员工」页的区别
 *
 * V2.1 那页管的是"创建/配置 Agent"（业务配置）。
 * 这一页回答 V3.0 的问题：**这个 AI 员工能做什么、以谁的身份、受什么约束。**
 *
 * ## 2026-10-05：从"待冻结"变成真数据
 *
 * 之前登记表里只有**裸员工 id**，这一页只能显示一个 id 加一片"待冻结"。
 * 员工数据到位后（契约 `p3-employee.json`），现在能显示：
 * **执行身份 / 岗位 / 按风险等级的授权限额 / 配置权**。
 *
 * ## 三条刻意的显示选择
 *
 * 1. **权限挂在岗位上，不挂人**——所以岗位是主视觉，员工是它的持有者。
 *    上游三处都写「岗位批准」「岗位 ACL」，无一处写「某人可批多少」。
 * 2. **`FORBIDDEN` 用红色**，与 `AUTO`/`APPROVE` 明确区分：
 *    "不得批准"和"允许批准"看错，代价不对称。
 * 3. **有"不完整"记录时仍然如实标出**，不因为"现在有数据了"就假设一切齐全。
 */

const DECISION_STYLE = {
  AUTO: { badge: 'badge-green', label: '可自动执行' },
  APPROVE: { badge: 'badge-yellow', label: '需批准' },
  FORBIDDEN: { badge: 'badge-red', label: '禁止批准' },
}

export default function V3AIEmployees() {
  const { data, error, errorText } = useV3Data('/v3-user/employees')

  return (
    <>
      <PageHeader
        title="AI 员工权限"
        when="管理员配置用：看系统里有哪几个 AI 员工、它们被允许做什么、金额上限多少。日常业务不需要改这里。"
        whenKind="muted"
        subtitle="权限挂在岗位上，不挂人"
      />

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
      {data?.error && <div className="alert alert-error">{data.error.message}</div>}

      {data && !data.error && (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <h3 style={{ marginTop: 0, fontSize: 14 }}>当前查看身份</h3>
            <table className="table">
              <tbody>
                <tr>
                  <td style={{ width: 180, color: 'var(--text-muted)' }}>所属企业与工厂</td>
                  <td>
                    <code>{data.tenant_id}</code> / <code>{data.factory_id}</code>
                  </td>
                </tr>
                <tr>
                  <td style={{ color: 'var(--text-muted)' }}>员工数量</td>
                  <td>
                    {data.employees.length} 名
                    {data.incomplete_count > 0 && (
                      <span className="badge badge-yellow" style={{ marginLeft: 8 }}>
                        其中 {data.incomplete_count} 名登记不完整
                      </span>
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 8, marginBottom: 0 }}>
              {plain(
                '每个 AI 员工都有一个**执行身份**（它代表谁去操作）。' +
                  '这个身份由系统按登录账号注入，不由页面填写。',
              )}
            </p>
          </div>

          {data.employees.map((employee) => (
            <div className="card" key={employee.employee_id} style={{ marginBottom: 16 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <h3 style={{ margin: 0, fontSize: 15 }}>🤖 {employee.employee_id}</h3>
                {employee.executing_actor_ref ? (
                  <span className="badge badge-blue">
                    执行身份 <code>{employee.executing_actor_ref}</code>
                  </span>
                ) : (
                  <span className="badge badge-gray">执行身份未登记</span>
                )}
                {employee.config_authority?.may_change_config === true && (
                  <span className="badge badge-purple">具备配置修改权</span>
                )}
              </div>

              {employee.positions.length === 0 && (
                <div className="alert alert-error" style={{ marginTop: 12, marginBottom: 0 }}>
                  {plain(
                    '这名员工**没有登记任何岗位**，因此不具备批准资格。' +
                      '（内部判据 `position_authorized` 会判不通过。）',
                  )}
                </div>
              )}

              {employee.positions.map((position) => (
                <div
                  key={position.position_id}
                  style={{
                    marginTop: 14,
                    paddingTop: 12,
                    borderTop: '1px solid var(--border)',
                  }}
                >
                  <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                    <strong style={{ fontSize: 14 }}>
                      {position.title ?? position.position_id}
                    </strong>
                    <code style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      {position.position_id}
                    </code>
                    {position.data_class_ceiling && (
                      <span className="badge badge-gray">
                        可访问至 {position.data_class_ceiling}
                      </span>
                    )}
                  </div>

                  <h4 style={{ fontSize: 13, margin: '12px 0 6px' }}>被委派范围</h4>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {position.delegated_scopes.length ? (
                      position.delegated_scopes.map((scope) => (
                        <span className="badge badge-blue" key={scope}>
                          <code>{scope}</code>
                        </span>
                      ))
                    ) : (
                      <span style={{ color: 'var(--text-muted)' }}>（无）</span>
                    )}
                  </div>

                  <h4 style={{ fontSize: 13, margin: '14px 0 6px' }}>
                    {plain('这类操作**最多能批多少**（按金额与风险分级）')}
                  </h4>
                  <table className="table">
                    <thead>
                      <tr>
                        <th style={{ width: 80 }}>分级</th>
                        <th style={{ width: 140 }}>系统结论</th>
                        <th>单笔上限</th>
                        <th style={{ width: 120 }}>是否需要第二人复核</th>
                      </tr>
                    </thead>
                    <tbody>
                      {position.limits.map((limit) => {
                        const style = DECISION_STYLE[limit.decision] ?? {}
                        return (
                          <tr key={limit.risk_class}>
                            <td>
                              <code>{limit.risk_class}</code>
                            </td>
                            <td>
                              <span className={`badge ${style.badge ?? 'badge-gray'}`}>
                                {style.label ?? limit.decision}
                              </span>
                            </td>
                            <td>{limit.max_amount ? <code>{limit.max_amount}</code> : '—'}</td>
                            <td>{limit.requires_second_approval ? '是' : '—'}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              ))}

              {employee.tenant_delegated_scopes?.length > 0 && (
                <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>
                  {plain('对照——**整个企业**被允许的范围（与上面按岗位的授权相比）：')}
                  {employee.tenant_delegated_scopes.map((s) => (
                    <code key={s} style={{ marginLeft: 6 }}>{s}</code>
                  ))}
                </p>
              )}
            </div>
          ))}

          <div className="alert alert-info">{plain(data.note)}</div>
        </>
      )}
    </>
  )
}
