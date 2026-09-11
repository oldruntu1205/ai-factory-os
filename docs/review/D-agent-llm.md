# D · Agent 执行链路与 LLM 网关 代码审查报告

- 审查方式：只读静态分析（未启动服务、未跑测试、未修改任何项目文件）
- 审查范围：LLM 网关（ai/llm.py、api/platform.py LLM 配置）、结构化解析（services/llm_parse.py）、LangGraph（agents/state|router|workflow.py）、全部角色 Agent（agents/*.py）、Agent 对话与管理 API（api/agent.py、api/agent_chat.py）、支撑层 Hermes Runtime/RBAC/ToolCenter/AgentRegistry/Soul（hermes/）、消费端点 api/quotation.py、utils/i18n.py、models/config/main 相关定义
- 阅读文件清单：
  - backend/ai/llm.py；backend/api/platform.py（LLM 配置读写）；backend/services/llm_parse.py；backend/utils/i18n.py
  - backend/agents/{state,router,workflow,customer,sales,quotation,service,production,quality,process,ceo,drawing_chat}.py
  - backend/api/{agent,agent_chat,quotation,marketplace,hermes}.py
  - backend/hermes/{runtime,tool/center,tool/tools,permissions/rbac,registry/agent_registry,registry/prompt_agent,registry/soul}.py
  - backend/models.py（PlatformLLMConfig/AIAgent/Conversation/AgentManifestRecord 等）、backend/config.py、backend/main.py（_init_hermes/中间件）、backend/knowledge/parser.py、backend/auth/deps.py

## 发现统计：P0 x 0 / P1 y 4 / P2 z 13

未发现可直接由普通用户无前置条件触发的 P0（跨租户越权、任意工具调用、密钥外泄的直接路径均被 require_company / RBAC 权限点 / fail-closed 未注册工具拒绝等挡住）。主要问题集中在三类：① RBAC/工具白名单的 **fail-open 边界语义**（空清单 = 放行）；② LLM 结构化输出的**契约不一致与静默失败**（写库路径无类型容错、异常全吞）；③ **演示兜底默认开启**与明文密钥落库。

---

## P1 中等

### P1-1 工具白名单为空时跳过校验（fail-open），未声明工具的自定义 Agent 实际可调用全部工具
- 【backend/hermes/permissions/rbac.py:160】【backend/hermes/tool/center.py:78,90-91】【backend/hermes/registry/prompt_agent.py:94-115】
- 问题描述：`assert_tool_allowed` 中 manifest 校验为 `if manifest and manifest.tools and tool_name not in manifest.tools: raise`——当 manifest 存在但 `tools` 为空列表（自定义 Agent 注册允许 `tools=[]`，见 hermes.py:52 无非空校验）时，**白名单校验被整体跳过**，仅剩租户权限点校验；而 `RBACSystem._default_permissions`（rbac.py:55-59）默认把全部 8 个权限点（含 `email:send`、`quotation:write`、`crm:write`）授予所有租户。同时 `prompt_agent_handler` 执行路径中，**工具名与参数完全来自 LLM 输出**（`call.get("tool")`），唯一拦截点就是该校验。组合后果：注册一个 `tools=[]` 的"纯对话"自定义 Agent，其对话消息经提示注入即可诱导 LLM 输出任意工具调用（如 `email.send` 发送到任意收件人、`crm.create_customer` 写库、`quotation.create` 生成报价），且工具执行全部成功——与注册语义（"未配置任何工具"）直接矛盾。另外 `center.execute` 在 `tool.permissions` 为空时跳过整个权限校验（center.py:78），在构造 permission_checker 的异常路径上 `permission_checker=None` 不校验（center.py:90-91），均为同族 fail-open。
- 影响：白名单语义翻转（空 = 全开）；提示注入从"影响文本输出"升级为"可调用真实副作用工具（外发邮件/写 CRM/生成报价）"。
- 修复建议：`assert_tool_allowed` 对 `manifest.tools == []`（或 None）应 fail-closed 拒绝一切工具调用（"未声明 = 不可用"），除非显式约定空=放行并在注册 API 强制 `tools` 非空；`center.execute` 去掉 `and tool.permissions` 前置条件，未声明权限的工具按拒绝处理。

### P1-2 AI 员工对话内报价/销售：LLM JSON 字段裸转无边界 + 异常全吞，产生脏报价或静默失败
- 【backend/agents/service.py:170-171,210-211,103-104,151-152,118-119】
- 问题描述：`_quotation_with_record` 用自有 `_extract_json` 解析后直接 `qty = int(parsed.get("quantity", 1) or 1)`、`price = float(parsed.get("unit_price", 0) or 0)`，再 `db.add + commit` 落库生成 `Quotation`。① LLM 常见输出（`"1,000"`、`"一千"`、`10.5`、负数）会使 `int()/float()` 抛 ValueError → 被外层 `except Exception: return content`（service.py:210）**静默吞掉**：用户看不到报价单、也无任何错误提示或日志；② 数量/单价为负数时不抛异常（`int("-5")` 合法）→ **生成负总价报价单落库**；③ 对照 api/quotation.py:51-108 已正确使用 `parse_json_output + safe_int/safe_float`（≥0、>0 校验），此处是第二套无容错实现，行为不一致；④ RBAC 检查的 `except Exception: pass`（service.py:103-104、151-152）把"权限系统非 PermissionError 异常"也吞掉并放行写库（fail-open），`_sales_with_lead` 顶层 `except Exception: return content`（118-119）同样吞掉 CRM 写库失败。
- 影响：报价/CRM 写库路径对 LLM 输出不可信假设缺失——脏业务数据（负金额报价单）、写库失败静默（用户误以为成功）、权限检查异常时放行写入。
- 修复建议：统一复用 `services/llm_parse.parse_json_output/safe_int/safe_float`，并对 quantity>0、unit_price>0、delivery_days>0 显式校验，非法即返回明确错误文案（含日志）；RBAC 异常改为 fail-closed（PermissionError 与其它异常一律拒绝并记录）；写库异常不再全吞，至少 logger.exception。

### P1-3 MockLLM 演示兜底默认开启：无 Key 生产部署会落库伪造报价并生成 PDF
- 【backend/config.py:71】【backend/ai/llm.py:134-139,255-267】【backend/agents/service.py:141-211】【backend/api/quotation.py:30-115】
- 问题描述：`LLM_FALLBACK` 默认值为 `"1"`（config.py:71），其注释自认"生产建议 LLM_FALLBACK=0，防止输出伪造报价/排产数据"，默认值与注释相反。未配置 `LLM_API_KEY` 的生产实例（或 DB 中平台 LLM 配置被清空）会静默降级 MockLLM，而 `MockLLM._demo_answer` 的报价分支（llm.py:255-267）返回**写死的伪造报价 JSON**（500ml 烧杯 1000 只、单价 1.20 美元）；`_quotation_with_record` 与 `api/quotation.py /quotation/create` 都会照常 `_extract_json`/`parse_json_output` 解析成功、通过 RBAC、**落库生成 Quotation 记录并调用 utils.trade 外贸估算/生成 PDF**。客服/生产/质量等角色同样返回编造数据。
- 影响：无 Key 部署时把演示假数据当成真实 AI 产出写进业务库（报价单可外发 PDF），数据污染与误导；与"演示兜底只保证链路可演示"的定位不符。
- 修复建议：默认值改为 `0`（fail-closed，无 Key 时启动即明确报错）；Mock 模式仅允许在显式 `DEMO_MODE=1` 下开启，且所有写库入口在 `is_mock()` 时拒绝落库并返回明确提示。

### P1-4 自定义 Agent 名为全局单例但注册不校验归属：跨租户同名覆盖，可被植入恶意 system_prompt
- 【backend/api/hermes.py:84-172】【backend/hermes/registry/agent_registry.py:67-91,95-99,107-113】【backend/models.py:723（name unique）】
- 问题描述：`AgentManifestRecord.name` 全局唯一、`AgentRegistry` 以 name 为全局 key，但 `register_agent` 只拦截内置名（hermes.py:97-98），**不检查该自定义名是否已被其它企业注册**。企业 B 注册同名 Agent 时：`registry.register` 覆盖内存 manifest（agent_registry.py:97），`persist` 以 name upsert 覆盖 DB 行并把 `company_id` 改成 B（agent_registry.py:86）——企业 A 的 manifest（system_prompt/tools/权限声明）被整体替换为 B 的内容。之后 A 企业经 `/hermes/execute`（agent_name 由调用方任意指定，仅 `assert_agent_allowed` 校验本企业白名单，而 A 注册时已把该名 allow 进自己白名单）执行该 Agent 时，运行的将是 B 填写的 system_prompt 与工具集；B 可在 prompt 中植入"把对话内容经 email.send 发送到指定邮箱"类指令，A 的对话与工具查询结果成为跨租户数据外泄载体。
- 影响：跨租户 manifest 篡改/数据外泄（需要两家企业注册同名自定义 Agent 的前置，属隔离设计缺陷而非即时漏洞）。
- 修复建议：注册时若 `name` 已存在且 `company_id != 0` 且非本企业，返回 409（或强制 agent 名带企业前缀/命名空间）；`persist` upsert 前同样校验归属；`/hermes/execute` 校验 `manifest.company_id`（非 0 时）与调用企业一致。

---

## P2 轻微

1. 【backend/ai/llm.py:117-161,41-46】`_get_llm` 懒加载无锁：并发首请求可能重复构建 LLM 实例并竞态写全局 `_use_mock`；`set_config_override` 热切换与在途请求存在窗口期混用新旧配置。建议模块级 `threading.Lock` 保护构建与切换。
2. 【backend/ai/llm.py:233-235】【backend/agents/drawing_chat.py:130】图片读取失败时把 `str(e)`（含本地绝对路径）拼进发给第三方 LLM 的文本 content；`_vision_answer` 把服务器本地 `image_paths` 绝对路径直接拼入 prompt 文本发送给 OpenAI——服务器目录结构泄露给 LLM 提供商。建议只传文件名、错误信息脱敏。
3. 【backend/models.py:714】【backend/api/platform.py:154-167】平台 LLM 第三方 API Key 明文存 DB JSON 列（`PlatformLLMConfig.config`，含各 gateway key），仅 GET 时脱敏展示。库泄露即密钥泄露，建议加密列或 KMS 托管。
4. 【backend/models.py:186-203】【backend/api/agent_chat.py:69-78,180-189】`Conversation` 全量落库且无任何保留/清理/删除机制（web/drawing_chat 每轮存用户原文+回答全文，含文件名），DB 无限增长。建议会话保留期清理任务或容量上限。
5. 【backend/api/agent_chat.py:31-36】【backend/api/hermes.py:57-62】`/agent/chat`、`/hermes/execute` 的 message 均无长度上限——租户员工可发 MB 级文本直灌 prompt（token 成本/上下文超限/计费滥用）。建议限制字符数（如 8k）并裁剪。
6. 【backend/api/agent_chat.py:122,110-115】`drawing-chat` files 为无数量上限的列表，每个可 20MB：极端请求可一次占满磁盘临时文件并触发多个解析器，属受认证限制的 DoS 面。建议限制文件数（如 ≤5）。
7. 【backend/agents/service.py:103-104,151-152】RBAC `assert_tool_permission` 的 `except PermissionError` 分支正确，但相邻 `except Exception: pass` 把其它异常也放行写库（fail-open）；同时 agent 执行全程未调用 `assert_agent_allowed`（Agent 级白名单只在 `/hermes/execute` 的 `start_agent` 触发）。建议权限检查失败一律拒绝并记录。
8. 【backend/hermes/tool/center.py:78,90-91】`if permission_checker is None and tool.permissions` 与构造异常→None 的兜底使 ToolCenter 存在多层 fail-open；当前内置工具均声明了 permissions 故未爆雷，属防御纵深缺口（将来新增无权限工具即静默绕过 RBAC）。
9. 【backend/services/llm_parse.py:15-20】docstring 声称"字段类型强制转换（str/int/float）"但 `parse_json_output` 只做 `json.loads`+合并，类型转换实际在调用点各自用 safe_*（且 agents/service.py 忘了用）——文档与实现不符，易误导。解析器本身对"漏最外层括号/截断"统一回退 defaults，容错策略 OK。
10. 【backend/agents/workflow.py:24-46】【backend/agents/quality.py:68-71】【backend/agents/process.py:101-115】LangGraph 图仅注册 customer/sales/quotation 三节点，`run_quality`/`run_process` 为死代码；同一角色在 LangGraph（文本版、不写库）与 `execute_specialized`（JSON 版、写库+RBAC）存在两套行为，入口不同结果不同，易误导维护（架构问题，见下节）。
11. 【backend/agents/process.py:25-26】`tools` 参数声明但函数体从不使用（docstring 声称"tools 注入后多步调用埋点供 GEPA 学习"），与 production/quality 的 `_gather_*` 不一致——死参数。
12. 【backend/agents/drawing_chat.py:72-77】`MAX_TOTAL_TEXT` 截断逻辑按整段计入 total 而非按剩余配额截断：第三个及以上文件可能整体被丢，提示文案"已截断（仅取前 8000 字）"与实际丢弃 0 字不符（成本控制近似，语义误导）。
13. 【backend/ai/llm.py:109-114】`_resolve_route` 对不在 `GATEWAY_ROUTES` 的 override gateway 回退全局 `(LLM_BASE_URL, LLM_MODEL)`（与 90-92 行注释的"修复"不一致），当前因 key 提取限制实际落到 mock，属静默降级无告警；另 line 155 `logger.info` 打印 base_url，若配置为含凭据的 URL 会进日志。

---

## 架构与可维护性观察

- **双执行模型并存**：`agents/service.execute_agent`（LangGraph 自动路由，3 节点）+ `execute_specialized`（角色直调，全角色矩阵）各维护一套 prompt 与产出契约（前者自由文本、后者 JSON 写库），LLM 解析容错只在后者部分实现。建议收敛为单一执行入口：LangGraph 图扩展全部角色节点，或直调统一走 `llm_parse` 契约层。
- **执行轨迹埋点只覆盖 `/hermes/execute`**：`TraceRecorder` 仅在 `HermesRuntime.execute` 中 `set_recorder`（runtime.py:94-97），而主要用户路径 `/agent/chat` → `execute_specialized` 不经过 runtime.execute，其内部 `tools.execute` 的 `get_recorder()` 为 None（center.py:99,103），故**最高频的对话路径完全不上自进化轨迹**，与"runtime.execute 已埋点"的注释不符；GEPA 只能学到低频 API 路径的样本。
- **RBAC 的设计取向是"默认放行 + revoke 拦截"**（`_default_permissions` 全授予、`allow_agent` 首次并入内置白名单、`_ensure_loaded` 无记录回退默认），安全边界实际依赖"租户主动 revoke"。多租户 SaaS 建议默认集收敛为最小必要（至少去掉 `email:send`、`crm:write` 的默认授予）。
- **错误传播策略不统一**：写库 Agent 路径普遍 `except Exception` 静默返回原始 LLM 文本（无日志、无错误标记）；Tool 层 `FunctionTool.execute` 也把异常转成 `{"ok": False}` 结构（tools.py:41-46），上层 quality/production 只检查 `ok` 真值、失败信息常被丢弃。运营侧无法区分"Agent 没生成"与"生成失败"。
- 值得肯定的点：跨租户数据访问普遍双校验（`require_company` + 查询带 `company_id` filter，且多数函数内部二次校验如 service.py:106-108/157-159）；Soul 双层文件存储 + `_safe_name` 路径消毒、平台内置 Agent 名防覆盖、发票/LLM 配置 GET 脱敏、drawing-chat 附件"不进 RAG、用完即删"的约定均实现到位；`rbac.assert_tool_allowed` 对**未注册工具 fail-closed**、`RBACSystem` DB 持久化都正确。
- 会话模型为**无状态单轮**（session_id 仅分组，历史不注入 prompt，多轮语义靠 RAG/新消息），因此不存在"会话历史无限增长撑爆 prompt"的问题——增长只发生在 DB（见 P2-4）。同会话并发消息无共享状态损坏风险，仅剩全局 LLM 单例构建竞态（P2-1）与 LangGraph 图并发 invoke（langgraph 编译图无状态，安全）。

---

## 结论

整体判断：该模块的工程安全意识在代码层普遍在线——租户隔离、权限点、路径消毒、附件生命周期、脱敏展示等关键位置均有显式加固且多数 fail-closed 方向正确，未发现可无前置条件直击的 P0。真正的风险集中在**三个"默认值/边界语义"与实现意图相悖之处**：① RBAC 与工具白名单的多处 fail-open（空 tools 清单 = 全工具放行，配合 LLM 输出的工具名直通执行，把提示注入放大为真实副作用调用）；② AI 员工对话内写库路径对 LLM 结构化输出缺乏 api/quotation.py 已有的类型/边界容错，且异常全吞导致脏数据与静默失败并存；③ LLM_FALLBACK 默认开启使无 Key 生产部署把 Mock 假报价当真数据落库。此外，明文密钥落库、执行轨迹漏掉主对话路径、双执行模型分裂属于值得纳入后续迭代清单的架构负债。建议按 P1-1 → P1-2 → P1-3 → P1-4 的顺序修复，均为局部改动、可独立回归。
