import React, { useEffect, useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import api from '../api'

const TABS = [
  { key: 'crm', label: 'CRM 客户', icon: '📇', desc: '客户、商机、报价与跟进全景' },
  { key: 'erp', label: 'ERP 资源', icon: '🧾', desc: '财务、采购、库存与主数据' },
  { key: 'mes', label: 'MES 生产', icon: '🏭', desc: '生产工单、产线负载与产量实绩' },
  { key: 'qms', label: 'QMS 质量', icon: '✅', desc: '检验记录、不良率与合规整改' },
]

const INTEG_STATUS = {
  unconfigured: { text: '未对接', cls: 'badge-gray' },
  configured: { text: '已配置', cls: 'badge-blue' },
  ok: { text: '已连通', cls: 'badge-green' },
  error: { text: '异常', cls: 'badge-red' },
}

const SOURCE_META = {
  platform: { text: '平台采集数据', cls: 'badge-green', tip: '来自平台已沉淀的真实业务记录' },
  sample: { text: '示例数据', cls: 'badge-gold', tip: '暂无真实来源，以下为演示示例（已标注）' },
  external: { text: '外部系统实时', cls: 'badge-purple', tip: '已从客户对接的外部系统实时拉取' },
}

const fmtMoney = (n) => '¥' + Number(n || 0).toLocaleString()

export default function BusinessSystems() {
  const [searchParams, setSearchParams] = useSearchParams()
  const initial = searchParams.get('tab')
  const [active, setActive] = useState(TABS.some((t) => t.key === initial) ? initial : 'crm')
  const [data, setData] = useState(null)
  const [loaded, setLoaded] = useState(false)
  const [msg, setMsg] = useState('')
  const companyId = Number(localStorage.getItem('company_id'))

  const load = useCallback(async (type) => {
    setLoaded(false)
    setMsg('')
    try {
      const { data } = await api.get(`/systems/${type}`, { params: { company_id: companyId } })
      setData(data)
    } catch (e) {
      setMsg('❌ 加载失败：' + (e.response?.data?.detail || e.message))
      setData(null)
    } finally {
      setLoaded(true)
    }
  }, [companyId])

  useEffect(() => { load(active) }, [active, load])

  const switchTab = (key) => {
    setActive(key)
    setSearchParams({ tab: key })
  }

  const ist = INTEG_STATUS[data?.integration_status] || INTEG_STATUS.unconfigured
  const src = SOURCE_META[data?.source] || SOURCE_META.sample

  return (
    <div>
      <div style={{ marginBottom: 14 }}>
        <h1 style={{ fontSize: 22, marginBottom: 4 }}>🗂️ 业务系统数据中心</h1>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: 0 }}>
          客户对接自有 CRM / ERP / MES / QMS 后，在此统一查看各系统的业务明细。
          MES 标签即「生产详情页」，QMS 标签即「质量详情页」。
        </p>
      </div>

      {/* 标签页 */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 18 }}>
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => switchTab(t.key)}
            className={active === t.key ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'}
            style={{ fontWeight: active === t.key ? 700 : 500 }}
          >
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      {msg && <div className="alert alert-error" style={{ marginBottom: 16 }}>{msg}</div>}

      {!loaded ? (
        <div className="card" style={{ padding: 30, color: 'var(--text-muted)' }}>加载中…</div>
      ) : !data ? (
        <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>暂无数据</div>
      ) : (
        <div>
          {/* 状态条 */}
          <div className="card" style={{ marginBottom: 18, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <span className={`badge ${ist.cls}`}>{ist.text}外部系统</span>
            <span className={`badge ${src.cls}`} title={src.tip}>{src.text}</span>
            {data.external_connected && (
              <span className="badge badge-purple">已对接客户外部系统</span>
            )}
            <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
              {TABS.find((t) => t.key === active)?.desc}
            </span>
          </div>

          {data.note && (
            <div className="alert alert-info" style={{ marginBottom: 16 }}>ℹ️ {data.note}</div>
          )}

          {/* 指标卡 */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 14, marginBottom: 18 }}>
            {Object.entries(data.metrics || {}).map(([k, v]) => (
              <div key={k} className="card" style={{ textAlign: 'center' }}>
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>{METRIC_LABELS[active]?.[k] || k}</div>
                <div style={{ fontSize: 22, fontWeight: 800 }}>
                  {METRIC_FORMAT[active]?.[k] === 'money' ? fmtMoney(v) : v}
                  {METRIC_SUFFIX[active]?.[k] ? <span style={{ fontSize: 13, color: 'var(--text-muted)' }}> {METRIC_SUFFIX[active][k]}</span> : null}
                </div>
              </div>
            ))}
          </div>

          {/* 明细表 */}
          <div className="card" style={{ overflowX: 'auto' }}>
            {active === 'crm' && <CustomersTable rows={data.recent_customers || []} />}
            {active === 'mes' && <WorkOrdersTable rows={data.work_orders || []} />}
            {active === 'qms' && <QualityTable rows={data.reports || []} />}
            {active === 'erp' && <ErpTable rows={data.recent_docs || []} />}
          </div>
        </div>
      )}
    </div>
  )
}

// ===== 指标文案/格式 =====
const METRIC_LABELS = {
  crm: { customers: '客户数', quotations: '报价单数', leads: '线索数', won_orders: '赢单数', pending_quotations: '待处理报价' },
  erp: { inventory_value: '库存金额', open_po: '未结采购单', ar_balance: '应收账款', ap_balance: '应付账款' },
  mes: { work_orders: '生产工单数', avg_progress: '平均进度', delay_count: '延期工单', today_output: '今日产出(良品)' },
  qms: { inspections: '检验批次数', avg_defect: '平均不良率', yield_rate: '良率', open_ncs: '开放不符合项' },
}
const METRIC_FORMAT = {
  erp: { inventory_value: 'money', ar_balance: 'money', ap_balance: 'money' },
}
const METRIC_SUFFIX = {
  mes: { avg_progress: '%' },
  qms: { avg_defect: '%', yield_rate: '%' },
}

// ===== 明细表 =====
function CustomersTable({ rows }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
      <thead>
        <tr style={{ borderBottom: '1px solid var(--border)' }}>
          <Th>客户名称</Th><Th>所属公司</Th><Th>邮箱</Th><Th>电话/WhatsApp</Th><Th>创建时间</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? <Empty /> : rows.map((r) => (
          <tr key={r.id} style={{ borderBottom: '1px solid var(--border)' }}>
            <Td><strong>{r.name}</strong></Td>
            <Td>{r.company || '—'}</Td>
            <Td>{r.email || '—'}</Td>
            <Td>{r.phone || r.whatsapp || '—'}</Td>
            <Td style={{ color: 'var(--text-muted)' }}>{r.created_at ? r.created_at.slice(0, 10) : '—'}</Td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function WorkOrdersTable({ rows }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
      <thead>
        <tr style={{ borderBottom: '1px solid var(--border)' }}>
          <Th>工单号</Th><Th>产品</Th><Th>数量</Th><Th>进度</Th><Th>产线</Th><Th>负载</Th><Th>状态</Th><Th>交期(天)</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? <Empty /> : rows.map((r) => (
          <tr key={r.order_no} style={{ borderBottom: '1px solid var(--border)' }}>
            <Td><strong>{r.order_no}</strong></Td>
            <Td>{r.product}</Td>
            <Td>{r.quantity}</Td>
            <Td>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{ flex: 1, height: 6, background: 'var(--bg-subtle)', borderRadius: 4, minWidth: 70 }}>
                  <div style={{ width: `${r.progress}%`, height: '100%', background: r.progress >= 100 ? '#16a34a' : '#2563eb', borderRadius: 4 }} />
                </div>
                <span style={{ fontSize: 12, width: 36 }}>{r.progress}%</span>
              </div>
            </Td>
            <Td>{r.line}</Td>
            <Td>{r.load_rate}%</Td>
            <Td>{r.delay ? <span className="badge badge-red">延期</span> : <span className="badge badge-green">正常</span>}</Td>
            <Td style={{ color: 'var(--text-muted)' }}>{r.deadline_days}</Td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function QualityTable({ rows }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
      <thead>
        <tr style={{ borderBottom: '1px solid var(--border)' }}>
          <Th>检验日期</Th><Th>产线</Th><Th>不良率</Th><Th>缺陷类型</Th><Th>抽检数</Th><Th>不良数</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? <Empty /> : rows.map((r, i) => (
          <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
            <Td style={{ color: 'var(--text-muted)' }}>{r.report_date ? r.report_date.slice(0, 10) : '—'}</Td>
            <Td>{r.line}</Td>
            <Td>
              <span className={r.defect_rate >= 5 ? 'badge badge-red' : r.defect_rate >= 2 ? 'badge badge-gold' : 'badge badge-green'}>
                {r.defect_rate}%
              </span>
            </Td>
            <Td>{r.defect_type === 'none' ? '—' : r.defect_type}</Td>
            <Td>{r.total_count}</Td>
            <Td>{r.defect_count}</Td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ErpTable({ rows }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 14 }}>
      <thead>
        <tr style={{ borderBottom: '1px solid var(--border)' }}>
          <Th>单据号</Th><Th>类型</Th><Th>往来方</Th><Th>金额</Th><Th>状态</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? <Empty /> : rows.map((r, i) => (
          <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
            <Td><strong>{r.doc_no}</strong></Td>
            <Td>{r.type}</Td>
            <Td>{r.supplier || r.customer || '—'}</Td>
            <Td>{fmtMoney(r.amount)}</Td>
            <Td><span className="badge badge-blue">{r.status}</span></Td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

const Th = ({ children }) => (
  <th style={{ textAlign: 'left', padding: '12px 10px', color: 'var(--text-muted)', fontWeight: 600 }}>{children}</th>
)
const Td = (props) => (
  <td style={{ padding: '12px 10px', ...(props.style || {}) }}>{props.children}</td>
)
const Empty = () => (
  <tr><td colSpan={8} style={{ padding: 30, textAlign: 'center', color: 'var(--text-muted)' }}>暂无明细数据</td></tr>
)
