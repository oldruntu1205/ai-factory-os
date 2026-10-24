/**
 * 客户语言层（V3.0 用户侧）。
 *
 * ## 为什么需要单独一个文件
 *
 * V3.0 是**内核**：它的字段名（`payload_hash`、`expected_version`、`tenant_id`）
 * 是给工程师与审计看的。客户（车间主任、计划员、财务）登录后看到这些，
 * **不是"专业"，是看不懂**——他们真正要回答的是：
 *
 *   「这是要我批什么？批了会怎样？我能不能不批？」
 *
 * 所以这些字段必须翻译。但**不能翻译掉**：V3.0 的全部价值在于可追溯
 * （上游 §5「审计时能从任务定位到它产生的副作用」）。把原始标识删掉，
 * 就换不回那条追溯链了。
 *
 * 结论：**人话在前，原始标识收在「技术详情」里可展开**。
 * 这也是真实产品的做法——默认易懂，需要时能查到。
 *
 * ## 集中在一处的理由
 *
 * 同一个概念在四个页面出现（如"租户"），各页各译必然译出四种说法。
 * 同理，`**加粗**` 这类 Markdown 记号在 JSX 里会**原样显示**成星号
 * （实测在截图里就能看到），所以清理也放在这里统一做。
 */

/** 把 `**粗体**` 与 `` `代码` `` 转成真实语义，而不是让星号显示给客户看。 */
export function plain(text) {
  if (typeof text !== 'string') return text
  return text.replace(/\*\*/g, '').replace(/`/g, '')
}

/**
 * 内部字段名 → 客户能懂的说法。
 *
 * 原则：**用客户的工作语言**，不用系统语言。
 * "排产计划"是客户说的，"object"是系统说的。
 */
const FIELD_LABELS = {
  object_id: '业务对象编号',
  expected_version: '对象当前版本',
  payload_hash: '内容指纹',
  tool_id: '要执行的操作',
  policy_version: '规则版本',
  valid_until: '批准有效期至',
  task_id: '任务编号',
  command_id: '执行单编号',
  approval_id: '批准单编号',
  release_id: '发布单编号',
  tenant_id: '所属企业',
  factory_id: '所属工厂',
  actor_id: '操作人',
  employee_id: 'AI 员工',
}

export function fieldLabel(key) {
  return FIELD_LABELS[key] ?? key
}

/**
 * 工具（动作）的内部名 → 客户能懂的说法 + 一句"它到底干什么"。
 *
 * `why` 那行很重要：客户批的是**一次具体动作**，不是一串名字。
 * 只给"发布生产排产计划"而不说它会动什么，他仍然没法判断该不该批。
 */
const ACTIONS = {
  'erp.inventory.reserve': {
    title: '预留库存',
    risk: '高',
    why: '按排产需求锁定指定数量的库存，避免被其他订单占用。会改变可用库存，因此需要第二个人确认。',
  },
  'pmc.dispatch.publish': {
    title: '发布生产排产计划',
    risk: '高',
    why: '把新排产计划下发到车间。会覆盖当前生效的排产安排，因此需要第二个人确认。',
  },
  'erp.stock.query': {
    title: '查询库存',
    risk: '低',
    why: '只读取库存数量，不修改任何数据，因此无需批准。',
  },
}

export function describeAction(toolId) {
  return ACTIONS[toolId] ?? { title: toolId ?? '未知操作', risk: '未知', why: null }
}

/** 未登记的工具要**说清没有说明**，而不是编一个。 */
export function actionTitle(toolId) {
  return describeAction(toolId).title
}

/**
 * 结果状态 → 客户能懂的说法。
 *
 * 注意「未过账」刻意**不**翻译成"处理中"或"失败"：
 * 它的准确含义是"还没有源系统回执"，两者都不等于。
 * 这个页面的全部意义就是不许把"没回执"说成别的。
 */
const OUTCOMES = {
  POSTED: { label: '已过账', tone: 'ok' },
  NOT_POSTED: { label: '未过账（还没收到源系统回执）', tone: 'wait' },
  NEEDS_HUMAN: { label: '需要人工处理', tone: 'warn' },
  FAILED: { label: '已失败', tone: 'bad' },
  UNKNOWN: { label: '结果未知（需要查对）', tone: 'warn' },
}

export function describeOutcome(code) {
  return OUTCOMES[code] ?? { label: code ?? '未知', tone: 'wait' }
}

/** 版本号单独显示没有意义，加一个"改过几次"的说法。 */
export function versionLabel(version) {
  if (version === null || version === undefined || version === '') return '—'
  return `第 ${version} 版`
}

/**
 * 内部代码（如 `DUTIES_NOT_SEPARATED`）→ 客户能懂的原因 + 下一步。
 *
 * 集中在这里还有一个好处：**每种拒绝都必须回答"那我该怎么办"**。
 * 只说"被拒绝"的界面，等于把问题丢回给用户。
 */
const REFUSALS = {
  DUTIES_NOT_SEPARATED: {
    title: '你不能批准自己发起的申请',
    because:
      '这条申请是你自己提交的。让提交的人自己点头，批准就失去意义了——' +
      '所以系统要求换一个人来批。',
    next: '请让另一位有批准权限的同事登录后处理这一条。',
  },
  APPROVAL_ALREADY_GRANTED: {
    title: '这一条已经批准过了',
    because: '批准记录一旦成立就不会被覆盖，重复提交不会改变结果。',
    next: '直接按已批准的流程继续；如需改动，请重新发起一次申请。',
  },
  APPROVAL_ALREADY_DENIED: {
    title: '这一条已经被否决',
    because: '否决是不可撤销的：不能事后改判为通过。',
    next: '如需继续，请重新发起一次申请并说明调整内容。',
  },
  APPROVAL_BINDING_CHANGED: {
    title: '申请内容在批准前发生了变化',
    because:
      '批准绑定的是当时那份内容。内容变了就必须重新批准——' +
      '否则会出现"批的是 A、执行的是 B"。',
    next: '请让发起人重新发起，你批准新的那一条。',
  },
}

export function describeRefusal(code) {
  return REFUSALS[code] ?? null
}
