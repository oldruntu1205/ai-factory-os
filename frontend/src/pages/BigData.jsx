import React, { useEffect, useRef, useState } from 'react'
import api from '../api'
import * as echarts from 'echarts'

/* ============================================================
 * 腾讯地图 Key（合规要求：必须用腾讯/高德/百度/天地图）
 * 默认是占位符 —— 上线前请到 https://lbs.qq.com 申请「JavaScript API」
 * 密钥，把下面这行替换成你的真实 key（前端明文 key 建议配置 Referer 白名单）。
 * 若仍是占位符，看板会显示提示框而非报错。
 * ============================================================ */
const TENCENT_MAP_KEY = 'J56BZ-JVECV-2NVP3-5LWVL-7COMH-N7FE2'
const MAP_KEY_CONFIGURED = !TENCENT_MAP_KEY.startsWith('Please apply')

const C = {
  bg: '#060b1f',
  panel: 'rgba(13,23,51,0.72)',
  border: 'rgba(0,234,255,0.22)',
  text: '#cfe0ff',
  muted: '#7d93c4',
  accent: '#00eaff',
  success: '#00ffa6',
  warn: '#ffb74d',
  danger: '#ff6b6b',
  blue: '#4d8bff',
}

const panelStyle = {
  background: C.panel,
  border: `1px solid ${C.border}`,
  borderRadius: 10,
  padding: 14,
  boxShadow: '0 0 18px rgba(0,234,255,0.06) inset',
}

const titleStyle = {
  fontSize: 15, color: C.text, marginBottom: 10,
  borderLeft: `3px solid ${C.accent}`, paddingLeft: 9, fontWeight: 700,
  letterSpacing: 1,
}

/* ---------------- ECharts 封装 ---------------- */
function EChart({ option, height = 240 }) {
  const ref = useRef(null)
  const chartRef = useRef(null)
  useEffect(() => {
    if (!ref.current) return
    chartRef.current = echarts.init(ref.current)
    const onResize = () => chartRef.current && chartRef.current.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chartRef.current && chartRef.current.dispose()
    }
  }, [])
  useEffect(() => {
    if (chartRef.current) chartRef.current.setOption(option, true)
  }, [option])
  return <div ref={ref} style={{ width: '100%', height }} />
}

/* ---------------- 腾讯中国地图 ---------------- */
function ChinaMap() {
  const ref = useRef(null)
  const [err, setErr] = useState(false)
  useEffect(() => {
    if (!MAP_KEY_CONFIGURED) return
    let destroyed = false
    const init = () => {
      if (destroyed || !ref.current || !window.TMap) return
      try {
        const map = new window.TMap.Map(ref.current, {
          zoom: 4.3, center: new window.TMap.LatLng(35.0, 104.0),
          baseMap: { type: 'vector', features: ['base', 'building3d'] },
        })
        const cities = [
          { n: '北京', lat: 39.9, lng: 116.4, v: 120 },
          { n: '上海', lat: 31.2, lng: 121.5, v: 200 },
          { n: '广州', lat: 23.1, lng: 113.3, v: 160 },
          { n: '成都', lat: 30.6, lng: 104.1, v: 90 },
          { n: '武汉', lat: 30.6, lng: 114.3, v: 110 },
          { n: '沈阳', lat: 41.8, lng: 123.4, v: 70 },
          { n: '西安', lat: 34.3, lng: 108.9, v: 80 },
          { n: '杭州', lat: 30.3, lng: 120.2, v: 130 },
        ]
        const dot = 'data:image/svg+xml;base64,' + btoa(
          '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><circle cx="8" cy="8" r="6" fill="#00eaff" stroke="#fff" stroke-width="1.5"/></svg>'
        )
        new window.TMap.MultiMarker({
          map,
          styles: { dot: new window.TMap.MarkerStyle({ width: 16, height: 16, src: dot }) },
          geometries: cities.map((c, i) => ({
            id: String(i), styleId: 'dot',
            position: new window.TMap.LatLng(c.lat, c.lng),
            properties: { name: c.n, v: c.v },
          })),
        })
      } catch (e) { setErr(true) }
    }
    if (window.TMap) { init() }
    else {
      const s = document.createElement('script')
      s.src = `https://map.qq.com/api/gljs?v=1.exp&key=${encodeURIComponent(TENCENT_MAP_KEY)}`
      s.onload = init
      s.onerror = () => setErr(true)
      document.head.appendChild(s)
    }
    return () => { destroyed = true }
  }, [])
  if (!MAP_KEY_CONFIGURED)
    return <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.muted, fontSize: 13, textAlign: 'center', lineHeight: 1.8 }}>
      请在 <code>BigData.jsx</code> 顶部配置腾讯地图 Key<br />（lbs.qq.com 申请 JavaScript API 密钥后替换 TENCENT_MAP_KEY）
    </div>
  if (err)
    return <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.warn, fontSize: 13 }}>腾讯地图加载失败，请检查 Key 是否有效</div>
  return <div ref={ref} style={{ width: '100%', height: '100%' }} />
}

/* ---------------- 主组件 ---------------- */
export default function BigData() {
  const [data, setData] = useState(null)
  const [report, setReport] = useState('')
  const [clock, setClock] = useState('')
  const [vh, setVh] = useState(typeof window !== 'undefined' ? window.innerHeight : 1080)
  const companyId = Number(localStorage.getItem('company_id'))
  const companyName = localStorage.getItem('company') || '我的企业'

  useEffect(() => {
    const t = setInterval(() => {
      const d = new Date()
      const p = (n) => String(n).padStart(2, '0')
      setClock(`${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`)
    }, 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const onResize = () => setVh(window.innerHeight)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const fitScreen = vh >= 800

  useEffect(() => {
    api.get('/dashboard/bigdata', { params: { company_id: companyId } })
      .then((r) => setData(r.data))
      .catch(() => {})
    // CEO 经营日报（LLM 生成，懒加载，避免阻塞首屏）
    api.get('/dashboard/daily-report', { params: { company_id: companyId } })
      .then((r) => setReport(r.data.report || ''))
      .catch(() => {})
  }, [companyId])

  // 生产订单按产线聚合
  const lineAgg = (() => {
    if (!data) return []
    const m = {}
    data.production_orders.forEach((o) => {
      if (!m[o.line]) m[o.line] = { line: o.line, count: 0, progress: 0 }
      m[o.line].count += 1
      m[o.line].progress += o.progress
    })
    return Object.values(m).map((x) => ({
      line: x.line, count: x.count, avg: Math.round(x.progress / x.count),
    }))
  })()

  const emailPie = data ? Object.entries(data.email_status).map(([k, v]) => ({
    name: k, value: v,
  })) : []

  const gaugeOption = {
    series: [{
      type: 'gauge',
      // 全屏用 3/4 圆弧；普通窗口高度紧，改用半圆，避免被压扁成椭圆
      radius: fitScreen ? '88%' : '85%',
      center: fitScreen ? ['50%', '55%'] : ['50%', '70%'],
      startAngle: fitScreen ? 225 : 180,
      endAngle: fitScreen ? -45 : 0,
      axisLine: { lineStyle: { width: 12, color: [[0.6, C.danger], [0.85, C.warn], [1, C.success]] } },
      pointer: { itemStyle: { color: C.accent } },
      progress: { show: false },
      axisTick: { show: false }, splitLine: { length: 10, lineStyle: { color: C.muted } },
      axisLabel: fitScreen
        ? { color: C.muted, fontSize: 10, distance: 14 }
        : { color: C.muted, fontSize: 10, distance: 12, interval: 20 },
      detail: { valueAnimation: true, fontSize: fitScreen ? 24 : 20, color: C.text, formatter: '{value}%', offsetCenter: fitScreen ? [0, '36%'] : [0, '0%'] },
      data: [{ value: data ? data.quality_yield : 0 }],
      title: { show: false },
    }],
  }

  const barOption = {
    grid: { left: 44, right: 52, top: 44, bottom: 28, containLabel: false },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: lineAgg.map((x) => x.line), axisLine: { lineStyle: { color: C.muted } }, axisLabel: { color: C.muted, fontSize: 11 } },
    yAxis: [
      { type: 'value', name: '订单数', nameTextStyle: { color: C.muted }, axisLabel: { color: C.muted }, splitLine: { lineStyle: { color: 'rgba(125,147,196,0.15)' } } },
      { type: 'value', name: '平均进度%', max: 100, nameTextStyle: { color: C.muted }, axisLabel: { color: C.muted }, splitLine: { show: false } },
    ],
    series: [
      { name: '订单数', type: 'bar', data: lineAgg.map((x) => x.count), itemStyle: { color: C.blue, borderRadius: [4, 4, 0, 0] }, barWidth: 18 },
      { name: '平均进度', type: 'line', yAxisIndex: 1, data: lineAgg.map((x) => x.avg), itemStyle: { color: C.accent }, smooth: true },
    ],
  }

  const pieOption = {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: C.muted, fontSize: 11 } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
      label: { color: C.text, fontSize: 11 },
      data: emailPie.length ? emailPie : [{ name: '暂无', value: 1, itemStyle: { color: C.muted } }],
      color: [C.success, C.accent, C.danger, C.warn, C.blue],
    }],
  }

  const kpi = data ? [
    { label: '销售额', value: `¥${(data.revenue).toLocaleString()}`, color: C.accent },
    { label: '报价单数', value: data.quotation_count, color: C.blue },
    { label: '生产订单', value: data.production_order_count, color: C.success },
    { label: '今日新增客户', value: data.new_customers_today, color: C.warn },
    { label: '质检合格率', value: `${data.quality_yield}%`, color: data.quality_yield >= 95 ? C.success : C.danger },
  ] : []

  const bs = data ? data.business_systems : {}

  return (
    <div style={{ background: C.bg, height: fitScreen ? '100vh' : 'auto', minHeight: '100vh', boxSizing: 'border-box', overflow: fitScreen ? 'hidden' : 'auto', color: C.text, padding: 16, fontFamily: 'system-ui, "Segoe UI", sans-serif', display: 'flex', flexDirection: 'column' }}>
      {/* 顶部标题 */}
      <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14, borderBottom: `1px solid ${C.border}`, paddingBottom: 12, flexShrink: 0 }}>
        <h1 style={{ fontSize: 26, margin: 0, color: '#fff', textShadow: `0 0 14px ${C.accent}`, letterSpacing: 2 }}>
          AiFactory大数据看板
        </h1>
        <div style={{ fontSize: 14, color: C.muted }}>{clock} ｜ {companyName}</div>
      </header>

      {!data ? <div style={{ padding: 40, textAlign: 'center', color: C.muted }}>加载中...</div> : (
        <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
          {/* KPI 条 */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 14, marginBottom: 14, flexShrink: 0 }}>
            {kpi.map((k) => (
              <div key={k.label} style={{ ...panelStyle, textAlign: 'center' }}>
                <div style={{ fontSize: 26, fontWeight: 800, color: k.color }}>{k.value}</div>
                <div style={{ fontSize: 13, color: C.muted, marginTop: 4 }}>{k.label}</div>
              </div>
            ))}
          </div>

          {/* 三列布局：填满剩余高度，grid row 1 对齐业务系统数据 / 邮件日志 */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr 1fr', gridTemplateRows: 'auto 1fr', gap: 14, height: '100%', minHeight: 0 }}>

            {/* 左列：AI 经营建议（跨两行，撑满列高，超出滚动） */}
            <div style={{ gridRow: '1 / 3', display: 'flex', flexDirection: 'column', gap: 14, height: '100%', minHeight: 0 }}>
              <div style={{ ...panelStyle, flex: 1, display: 'flex', flexDirection: 'column', overflowY: 'auto' }}>
                <div style={titleStyle}>AI 经营建议 · CEO 经营大脑每日汇报</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
                  {data.ai_suggestions.map((s, i) => (
                    <div key={i} style={{
                      fontSize: 12, padding: '6px 10px', borderRadius: 6,
                      background: s.level === 'danger' ? 'rgba(255,107,107,0.15)' : s.level === 'warning' ? 'rgba(255,183,77,0.15)' : s.level === 'success' ? 'rgba(0,255,166,0.12)' : 'rgba(125,147,196,0.15)',
                      color: s.level === 'danger' ? C.danger : s.level === 'warning' ? C.warn : s.level === 'success' ? C.success : C.text,
                    }}>{s.text}</div>
                  ))}
                </div>
                {report ? (
                  <pre style={{ background: 'rgba(0,0,0,0.25)', padding: 12, borderRadius: 8, fontSize: 12, whiteSpace: 'pre-wrap', lineHeight: 1.7, color: C.text, margin: 0 }}>
                    {report}
                  </pre>
                ) : (
                  <div style={{ fontSize: 12, color: C.muted }}>（CEO 经营日报生成中或未认证）</div>
                )}
              </div>
            </div>

            {/* 中列第 1 行：业务系统数据 */}
            <div style={panelStyle}>
              <div style={titleStyle}>业务系统数据</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                {[
                  { sys: 'CRM', m: bs.crm?.metrics, items: [['客户', 'customers'], ['报价', 'quotations'], ['线索', 'leads']] },
                  { sys: 'MES', m: bs.mes?.metrics, items: [['工单', 'work_orders'], ['均进度', 'avg_progress'], ['延期', 'delay_count']] },
                  { sys: 'QMS', m: bs.qms?.metrics, items: [['检验', 'inspections'], ['良率', 'yield_rate'], ['不良', 'avg_defect']] },
                  { sys: 'ERP', m: bs.erp?.metrics, items: [['库存值', 'inventory_value'], ['PO', 'open_po'], ['应收', 'ar_balance']] },
                ].map((b) => (
                    <div key={b.sys} style={{ background: 'rgba(0,234,255,0.06)', padding: 6, borderRadius: 8, border: `1px solid ${C.border}` }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: C.accent, marginBottom: 6 }}>{b.sys}</div>
                    {b.items.map(([lab, key]) => (
                      <div key={key} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: C.muted, marginBottom: 2 }}>
                        <span>{lab}</span>
                        <span style={{ color: C.text }}>{b.m ? (typeof b.m[key] === 'number' ? b.m[key].toLocaleString() : b.m[key]) : '-'}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>

            {/* 右列第 1 行：邮件发送日志（与业务系统数据在同一行，底部自然对齐） */}
            <div style={panelStyle}>
              <div style={titleStyle}>邮件发送日志</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 10, height: 'calc(100% - 30px)' }}>
                <div style={{ overflowY: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <tbody>
                      {data.email_logs.length === 0 && (
                        <tr><td style={{ color: C.muted, padding: 8 }}>暂无邮件记录</td></tr>
                      )}
                      {data.email_logs.slice(0, 8).map((l) => (
                        <tr key={l.id} style={{ borderTop: '1px solid rgba(125,147,196,0.12)' }}>
                          <td style={{ padding: '4px 4px', color: C.text, maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{l.recipient}</td>
                          <td style={{ color: C.muted }}>{l.status}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <EChart option={pieOption} height='100%' />
              </div>
            </div>

            {/* 中列第 2 行：质检/生产订单图表（撑满）+ AI 销售中心（贴底） */}
            <div style={{ gridColumn: 2, display: 'flex', flexDirection: 'column', gap: 14, height: '100%', minHeight: 0 }}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, flex: 1, minHeight: 220 }}>
                <div style={{ ...panelStyle, height: '100%', minHeight: 180, display: 'flex', flexDirection: 'column' }}>
                  <div style={titleStyle}>质检合格率</div>
                  <div style={{ flex: 1, minHeight: 0 }}>
                    <EChart option={gaugeOption} height='100%' />
                  </div>
                </div>
                <div style={{ ...panelStyle, height: '100%', minHeight: 180, display: 'flex', flexDirection: 'column' }}>
                  <div style={titleStyle}>生产订单 · 产线</div>
                  <div style={{ flex: 1, minHeight: 0 }}>
                    <EChart option={barOption} height='100%' />
                  </div>
                </div>
              </div>
              <div style={panelStyle}>
                <div style={titleStyle}>AI 销售中心</div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                  {[
                    { label: '发现客户', value: data.sales.discovered_today },
                    { label: 'A 级客户', value: data.sales.grade_a },
                    { label: '发送邮件', value: data.sales.emails_sent },
                    { label: '回复', value: data.sales.replies },
                    { label: '商机', value: data.sales.opportunities },
                    { label: '预计金额', value: `$${(data.sales.estimated_amount).toLocaleString()}` },
                  ].map((s) => (
                    <div key={s.label} style={{ background: 'rgba(77,139,255,0.12)', padding: '6px 10px', borderRadius: 8 }}>
                      <div style={{ fontSize: 14, fontWeight: 700, color: C.accent }}>{s.value}</div>
                      <div style={{ fontSize: 10, color: C.muted }}>{s.label}</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* 右列第 2 行：生产排程（撑满剩余空间） */}
            <div style={{ gridColumn: 3, ...panelStyle, height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
              <div style={titleStyle}>生产排程</div>
              <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                  <thead>
                    <tr style={{ color: C.muted, textAlign: 'left' }}>
                      <th style={{ padding: '4px 6px' }}>工单</th><th>产品</th><th>产线</th><th>进度</th><th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.production_orders.length === 0 && (
                      <tr><td colSpan={5} style={{ textAlign: 'center', color: C.muted, padding: 12 }}>暂无生产订单</td></tr>
                    )}
                    {data.production_orders.slice(0, 30).map((o) => (
                      <tr key={o.order_no} style={{ borderTop: '1px solid rgba(125,147,196,0.12)' }}>
                        <td style={{ padding: '5px 6px' }}>{o.order_no}</td>
                        <td>{o.product}</td>
                        <td>{o.line}</td>
                        <td>{o.progress}%</td>
                        <td>{o.delay ? <span style={{ color: C.danger }}>延期</span> : <span style={{ color: C.success }}>正常</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

          </div>
        </div>
      )}
    </div>
  )
}
