import React, { useMemo, useState } from 'react'
import PageHeader from '../components/PageHeader'

// 帮助中心（wiki 模式）：按主题分组 FAQ，支持关键词搜索
// 内容当前为内置静态条目，后续可扩展为后端可编辑（如挂到知识库 / CMS）。
const HELP_GROUPS = [
  {
    key: 'start', icon: '🚀', title: '快速上手',
    items: [
      { q: '平台是什么？能帮我做什么？', a: '这是一套面向制造业（出口型）企业的 AI 员工平台：AI 销售可自主找客户、发多语种开发信、跟进询盘；AI 员工可处理报价、知识库问答、图纸等；还有报价单、渠道中心、CRM、大数据看板等模块。' },
      { q: '第一次使用应该先做什么？', a: '① 在「企业设置」完成企业认证（上传营业执照，审核通过后解锁全部功能）；② 在「模型设置/账户设置」确认 AI 模型已配置；③ 从「AI员工」开始创建第一个 AI 员工，或直接使用「AI对话」体验。' },
      { q: 'AI 员工和 AI 对话有什么区别？', a: '「AI对话」是即时问答工具；「AI员工」是可持续执行任务的角色（如销售、客服），带工作台、日志、自进化，可长期运行并沉淀经验。' },
    ],
  },
  {
    key: 'verify', icon: '🔐', title: '认证与账户',
    items: [
      { q: '为什么提示我"未认证"？', a: '企业需上传营业执照并通过平台审核后才算认证完成。未认证期间只能访问驾驶舱、企业设置、账户设置。请到「企业设置 → 认证」上传资料，平台审核通过后自动解锁。' },
      { q: '如何添加企业成员？', a: '管理员可在「账户设置/企业设置」中添加成员，分配管理员或员工角色。员工默认看不到部分管理功能。' },
      { q: '忘记密码怎么办？', a: '在登录页点击"忘记密码"，通过注册邮箱接收重置链接（如平台未配置邮件服务，请联系客服人工重置）。' },
    ],
  },
  {
    key: 'agents', icon: '🤖', title: 'AI员工',
    items: [
      { q: '如何创建一个 AI 员工？', a: '进入「AI员工」页面，点击新建：选择角色模板（销售/客服/采购等）、填写职责描述、绑定知识库和渠道，保存后即可开始对话或任务。' },
      { q: 'AI 员工的"自进化"是什么？', a: 'AI 员工会把执行过的任务、用户反馈、成功经验沉淀到自己的经验库中，后续同类任务会优先参考，越用越贴合你的业务。可到「自进化」页面查看学习记录。' },
      { q: '为什么 AI 员工不回复某些问题？', a: '可能是职责边界设置（越权问题会被拒绝）、知识库未命中，或模型服务异常。可在员工设置中放宽职责，或补充知识库内容。' },
    ],
  },
  {
    key: 'knowledge', icon: '📚', title: '知识库',
    items: [
      { q: '如何上传资料到知识库？', a: '进入「知识库」→ 新建/选择分类 → 上传文件（PDF/Word/Excel/文本等）或粘贴网页链接。系统会自动解析并建立索引，供 AI 员工检索。' },
      { q: '知识库多久生效？', a: '上传后通常几分钟内完成解析与索引。若 AI 未命中新内容，可稍等片刻或手动触发重新索引。' },
      { q: '支持哪些语言的内容？', a: '支持中、英、德、法、西、日等多语种内容解析，适合存放外贸资料、产品手册、技术文档等。' },
    ],
  },
  {
    key: 'sales', icon: '🎯', title: 'AI自主销售与渠道',
    items: [
      { q: 'AI自主销售怎么用？', a: '在「AI自主销售」页面设置目标国家/行业、关键词后点击"发现客户"，AI 会生成潜在客户列表；可一键发送开发信并自动跟进（邮件语言可选英文/中文，支持 A/B 文案）。' },
      { q: '如何接入 WhatsApp / 企业微信 / 邮件？', a: '在「渠道中心」选择对应渠道完成绑定（扫码/授权）。绑定后 AI 员工即可通过该渠道与客户沟通，往来记录会同步到 CRM。' },
      { q: '客户跟进状态代表什么？', a: '已发现 → 已联系 → 已回复 → 已转化商机 → 已成交。系统按设定节奏自动跟进（如未回复会换文案重发），可在报价单/CRM 中查看全链路。' },
    ],
  },
  {
    key: 'quotation', icon: '📄', title: '报价与交易',
    items: [
      { q: '如何生成出口报价单？', a: '在「报价单」页面新建：选择产品、数量、贸易术语（FOB/CIF 等）、目的国，系统会结合汇率与运费生成报价；支持导出为 PDF/Excel 并与客户分享。' },
      { q: '汇率和运费怎么算的？', a: '系统通过公开汇率接口获取实时汇率，并按目的国/重量体积估算运费与关税（估算值仅供参考，实际以货代/海关为准）。' },
      { q: '可以发多语言报价单吗？', a: '可以。选择报价单语言后系统自动翻译成目标语言，方便发给海外客户。' },
    ],
  },
  {
    key: 'billing', icon: '💳', title: '计费与发票',
    items: [
      { q: '套餐和账单在哪查看？', a: '「套餐与账单」页面可查看当前套餐、用量、账单明细，以及续费/升级入口。' },
      { q: '如何获取发票？', a: '平台开票类别为"服务费"。请先在「企业设置 → 开票信息」维护开票资料（企业抬头、税号等），再到「套餐与账单」对已支付账单点击「申请开票」；财务开具后可在申请记录中下载电子发票。' },
      { q: '用量超了会怎样？', a: '超出套餐部分按对应计费规则计入下期账单。可在套餐页面升级以避免超量费用。' },
    ],
  },
  {
    key: 'tech', icon: '⚙️', title: '系统与集成',
    items: [
      { q: '如何对接我公司的 ERP / MES / CRM？', a: '在「企业 → 对接业务系统」中选择对应系统，按向导填写接口地址与凭据完成对接；对接后「业务系统数据」页面可同步查看与推送数据。' },
      { q: '数据安全如何保障？', a: '平台数据隔离存储，密码加密保存，敏感配置（API Key 等）加密处理；聊天与业务数据仅你的企业可见。' },
      { q: '页面显示异常或功能报错怎么办？', a: '先尝试刷新页面或重新登录；若问题持续，请通过右下角"在线客服"或邮件联系平台支持，并附上报错截图。' },
    ],
  },
]

function HelpItem({ item, open, onToggle }) {
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 8, marginBottom: 8, overflow: 'hidden' }}>
      <button
        onClick={onToggle}
        style={{
          width: '100%', padding: '12px 16px', background: 'transparent', border: 'none',
          cursor: 'pointer', textAlign: 'left', fontSize: 14, fontWeight: 600, color: 'var(--text)',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10,
        }}
      >
        <span>{item.q}</span>
        <span style={{ color: 'var(--text-muted)', fontSize: 12, transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.2s' }}>▶</span>
      </button>
      {open && (
        <div style={{ padding: '0 16px 14px', fontSize: 13.5, lineHeight: 1.8, color: 'var(--text-muted)' }}>
          {item.a}
        </div>
      )}
    </div>
  )
}

export default function Help() {
  const [query, setQuery] = useState('')
  const [opened, setOpened] = useState(new Set())

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return HELP_GROUPS
    return HELP_GROUPS
      .map((g) => ({
        ...g,
        items: g.items.filter(
          (it) => it.q.toLowerCase().includes(q) || it.a.toLowerCase().includes(q)
        ),
      }))
      .filter((g) => g.items.length > 0)
  }, [query])

  const toggle = (key) => {
    setOpened((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div style={{ maxWidth: 860, margin: '0 auto' }}>
      <PageHeader title="使用帮助" subtitle="常见问题与使用指南（wiki 模式）" />

      <div className="card" style={{ padding: 16, marginBottom: 20 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索问题，例如：认证、发票、报价、WhatsApp…"
            style={{ flex: 1, padding: '10px 14px', borderRadius: 8, border: '1px solid var(--border-strong)', background: 'var(--bg-subtle)', color: 'var(--text)', fontSize: 14 }}
          />
          {query && (
            <button className="btn btn-secondary" onClick={() => setQuery('')}>清空</button>
          )}
        </div>
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--text-muted)' }}>
          共 {groups.reduce((n, g) => n + g.items.length, 0)} 条内容 ·
          未找到答案？点击右下角 🎧 在线客服，或发邮件至平台客服邮箱。
        </div>
      </div>

      {groups.map((g) => (
        <div key={g.key} style={{ marginBottom: 24 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <span style={{ fontSize: 18 }}>{g.icon}</span>
            <h3 style={{ fontSize: 16, margin: 0 }}>{g.title}</h3>
          </div>
          {g.items.map((it, i) => {
            const key = `${g.key}-${i}`
            return (
              <HelpItem key={key} item={it} open={opened.has(key)} onToggle={() => toggle(key)} />
            )
          })}
        </div>
      ))}

      {groups.length === 0 && (
        <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
          没有找到相关条目，试试其他关键词，或联系在线客服。
        </div>
      )}
    </div>
  )
}
