# B · Hermes Agent 内核 代码审查报告

> 审查性质：只读静态审查（未启动服务、未跑测试、未写任何项目文件）
> 审查日期：以本次会话为准（系列审查：A = 认证与租户隔离 / E = 计费支付 / B = Hermes Agent Runtime 内核）
> 相关报告：A 报告（`A-auth-tenant-isolation.md`）确认 hermes/evolution 等 API 层普遍使用 `require_company`；本报告聚焦 A 未覆盖的层面——**Agent 注册表 / Tool Router / RBAC / 自进化引擎的内部隔离与执行安全**。

## 范围 / 阅读文件清单

模块：Hermes Agent Runtime 内核（Agent 注册/编排/Tool 调用/RBAC/记忆/队列/自我进化）

- `backend/hermes/runtime.py`（生命周期编排/任务派发/会话）
- `backend/hermes/registry/agent_registry.py`、`models.py`、`prompt_agent.py`、`soul.py`
- `backend/hermes/tool/center.py`、`tools.py`
- `backend/hermes/permissions/rbac.py`
- `backend/hermes/memory/memory_system.py`
- `backend/hermes/queue/task_queue.py`、`rabbitmq.py`
- `backend/hermes/evolution/`（service/gepa/extractor/evaluator/curator/scheduler/store/trace）
- `backend/api/hermes.py`、`backend/api/evolution.py`
- 交叉验证：`backend/main.py`（装配/内置 Agent/调度器）、`backend/models.py`（AgentManifestRecord/HermesRbacSetting/AgentTrace/AgentSkill/ToolOverride/EvolutionCandidate/AgentMemory）、`backend/auth/deps.py`、`backend/agents/service.py` 及子模块调用点、`backend/scripts/hermes_worker.py`、`backend/config.py`

## 发现统计：P0 0 / P1 5 / P2 10

- **未发现 P0**：没有"LLM 生成代码被执行/热替换"（tool_code 进化只产 `.patch.md` 提案文件且 AST 静态校验后**明确不执行**，`gepa.py:301-349` + `service.py:89-109`）；没有可直达的路径穿越（`soul.py` 用 `_safe_name` 清洗 agent 名，API 层 company_id 均为 int 强类型，tool_code 的 target_key 有白名单 `gepa.py:226-228`）；没有无认证任意代码执行 sink（hermes 目录内无 eval/exec/subprocess/os.system）。
- **最接近 P0 的一条（P1-1）**：自定义 Agent 注册表是**全局命名空间**，可被任一企业管理员同名覆盖/注销他企 Agent、且 manifest 对他企可读可执行——在多租户 SaaS 威胁模型下具备"跨租户提示词接管 → 借受害企业员工触发导出其数据"的完整链路，详见下文"升级条件"。

---

## P1 中等偏高

### P1-1｜自定义 Agent 注册表无租户命名空间：同名覆盖他企 Agent（跨租户接管/数据外泄链）

【文件:行号】
- `backend/api/hermes.py:84-172`（`register_agent`：仅拒绝内置名 `:96-98` 与 tools 白名单 `:100-104`，不校验 `name` 是否已被其他企业注册）
- `backend/hermes/registry/agent_registry.py:67-91`（`persist`：按 `name` 单键 upsert，`row.company_id` 被直接改写 `:86`，若他企已有同名记录则**整行覆盖**）
- `backend/models.py:723`（`agent_manifests.name` 全局 `unique=True`，无 `(company_id, name)` 复合唯一——产品层即全局名空间）
- `backend/hermes/registry/agent_registry.py:107-142`、`217-230`（`get_agent`/`invoke_agent` 只按 name 解析，从不校验 manifest.company_id 与执行方 company_id）
- `backend/hermes/runtime.py:41-78`（`start_agent`/`execute` 不校验 agent 归属）

【问题描述】A 企业注册自定义 Agent `X`（含专属 system_prompt 与工具白名单）后，B 企业管理员可注册同名 `X`：`persist` 按 name upsert 直接覆盖 DB 整行并把 `company_id` 改写为 B；B 的 `system_prompt`/`tools` 成为该名字的全局唯一定义。受害企业 A 的员工此后每次 `POST /hermes/execute` 触发 `X`（A 的 RBAC 白名单含 X、`rbac.py:144-151` 无配置时默认放行），实际执行的已是 B 的提示词与工具白名单——B 可声明 `["crm.get_customer","email.send"]` 并编写"读取客户数据并邮件发送到指定地址"的提示词，A 员工触发后即完成**跨租户数据外泄**；同时 A 的 Agent 定义被静默破坏（完整性/可用性）。

【影响】
1. 跨租户完整性/可用性：任何企业管理员可让他企 Agent 失效或行为被替换（注册覆盖 / 注销删除，见 P1-2）；
2. 跨租户机密性：受害企业员工正常使用被接管 Agent 时，其 CRM/知识数据可被攻击者提示词指挥外发（多步但链条完整）；
3. 无任何隔离或告警：`register` 成功返回 200，A 侧无感知。

【升级条件】若平台面向公网多租户开放（企业自助注册 + 客户管理员即攻击者），且存在同名撞名/恶意抢注，此条应视为 **P0** 处理。

【修复建议】
- DB 改为 `UniqueConstraint(company_id, name)`，`persist`/`register` 前校验既有行属主（非本企业 → 拒绝或要求唯一化命名）；
- `get_agent`/`invoke_agent`/`start_agent` 增加 company 维度解析（`company_id=0` 平台级共享除外），执行时校验 `manifest.company_id ∈ {0, 执行方 company_id}`；
- `execute` 前显式校验 agent 归属，防"借用他企 agent 名"。

### P1-2｜Agent 列表/详情/注销接口无属主校验：跨租户读取他企 system_prompt、任意注销他企 Agent

【文件:行号】
- `backend/api/hermes.py:201-215`（`GET /agent/list`、`GET /agent/{name}`：任意登录用户，无 `require_company`、无 admin 门槛、无 manifest 属主过滤）
- `backend/hermes/registry/agent_registry.py:144-184`（`list_agents` 合并 DB **全部企业**的自定义 Agent 记录 `:152-171`）；`:115-142`（`_load_one_from_db` 按 name 全局查库）
- `backend/hermes/registry/models.py:32-44`（`to_dict()` 含 `tools` 与 `system_prompt`，不含 company_id）
- `backend/api/hermes.py:175-198`（`DELETE /agent/{name}`：仅拦内置名，按 name 删 DB 记录，**无企业属主校验**）

【问题描述】注册表读取/注销面完全没有租户过滤：
- 任意企业**普通员工**（非管理员）调用 `GET /hermes/agent/list` 可拿到全平台所有自定义 Agent 的完整定义（含他企 `system_prompt`、工具白名单、描述）；
- 任意企业管理员可用 `DELETE /hermes/agent/{name}` 注销**任意企业**的自定义 Agent（DB 记录按 name 直删），构成跨租户 DoS/破坏。

【影响】① 他企商业提示词（常含公司业务规则/策略）泄露给所有租户员工；② 恶意抢注前可先枚举全平台 agent 名（配合 P1-1 的接管链）；③ 跨租户注销破坏他企 AI 员工可用性。

【修复建议】list/get 仅返回 `company_id ∈ {0(平台级), 当前企业}` 的记录；detail 对非属主企业不下发 `system_prompt`；delete/persist 均校验 `record.company_id == current["company_id"]`（或平台超管）。

### P1-3｜RBAC 默认全开 + 空工具白名单反转：未配置工具的 Agent 反而获得全部工具；权限链无员工/部门粒度

【文件:行号】
- `backend/hermes/permissions/rbac.py:55-59`（`_default_permissions` 默认把**全部 8 个权限点**（含 `email:send`、`quotation:write`、`crm:write`）授权给每个新租户）
- `backend/hermes/permissions/rbac.py:144-151`（`assert_agent_allowed`：租户无 `HermesRbacSetting` 记录 → `allowed` 为空 → **默认放行任意 Agent 名**）
- `backend/hermes/permissions/rbac.py:128-135`（`allow_agent` 首次授权自动并入全部内置 Agent 名——白名单几乎永不收窄）
- `backend/hermes/registry/prompt_agent.py:76-82`（`if not allowed or t["name"] in allowed`：`manifest.tools` 为空 → **工具清单 = 全部内置工具**）
- `backend/api/hermes.py:46-54, 100-104`（注册允许 `tools=[]`，校验只挡"不在白名单内"的工具，不要求至少声明一个）
- `backend/main.py:497`（仅 `init_tenant(1)`，其余租户永远走"默认放行"分支）

【问题描述】对外宣称的权限模型是"企业→部门→员工→Agent→Tool 权限链、Agent 只能调用 manifest 声明的 tools、Tool 权限点需租户授权"，实际落地的语义是：
- **默认即全开**：新租户没有 RBAC 记录时（绝大多数租户，`main.py:497` 只初始化了租户 1），任何已注册 Agent 名、任何内置工具权限点全部放行；
- **白名单反向放大**：管理员为自定义 Agent 声明 0 个工具（想"先不配"）时，`prompt_agent` 会把**所有**内置工具（含 `email.send` 等副作用工具）暴露给该 Agent 的 LLM，工具权限点又默认全授权 → "不配=全给"；
- 无员工/部门粒度：任何员工（`POST /hermes/execute` 无 admin 门槛，`api/hermes.py:281-298`）都能触发租户内全部 Agent 与白名单内工具，部门/员工级授权从未实现。

【影响】提示注入面被显著放大：不可信对话内容（客服场景=客户原文）可指挥 LLM 调用未预期的副作用工具（发任意邮件/建报价/写客户）——`prompt_agent.py:100-115` 对 LLM 输出的 `params` 无任何约束。属"最小权限"系统性缺失，而非单点 bug。

【修复建议】① 空 `tools` 的 Agent 运行时白名单按 fail-closed 处理（0 工具），或注册时强制至少 1 个；② RBAC 缺省策略收窄（至少默认剔除 `email:send` 等敏感权限点，改为显式开通）；③ 补充"员工→可触发 Agent 集合"授权层（表/配置），execute 前校验。

### P1-4｜`POST /hermes/evolution/scheduler/trigger`：任一企业管理员可触发**全平台**周期自进化（跨租户写 + 平台级 LLM 费用 DoS）

【文件:行号】
- `backend/api/evolution.py:284-289`（`trigger_scheduler`：仅 `_require_admin(current)`，**无 `require_company`**）
- `backend/hermes/evolution/scheduler.py:21-45`（`_active_company_ids()` 遍历所有 `companies` 行）与 `:69-108`（`run_once()` 对每个活跃企业执行 `service.auto_evolve_cycle`）

【问题描述】该路由不校验调用者所属企业，而 `run_once` 是无差别遍历全平台所有活跃企业的守护线程主逻辑。任一企业管理员调用一次即可让平台对其**每一个**活跃企业执行完整自进化：对每企业、每个有轨迹的 agent、每个 skill 依次调 LLM 生成候选（`service.py:148-204`）。

【影响】① **跨租户写副作用**：在**其他企业**的 `evolution_candidates` 表批量写入 pending 候选（他企数据被无授权污染）；② **平台级经济 DoS**：单次触发产生全平台规模 LLM 调用（×agent×skill×n_variants），可脚本化重复触发烧平台 LLM 费用、拖慢主服务（LLM 全局信号量 `ai/llm.py:23` 被占满）。

【修复建议】trigger 限定平台超管（`role == "platform_admin"`）或加 `company_id` 参数并 `require_company` 限定本企业；`run_once` 保持仅供平台调度器后台线程使用。另建议给 run 类端点加触发频控与 n_variants 上限。

### P1-5｜ToolCenter 权限校验 fail-open：RBAC 异常/未初始化或工具无 permissions 时静默跳过全部检查

【文件:行号】
- `backend/hermes/tool/center.py:78-95`
- 具体：`:78` 权限检查器仅在 `permission_checker is None and tool.permissions`（非空）时才构建；`:79-91` `try` 内 `get_runtime()` 抛错或 DB 访问异常 → `except Exception: permission_checker = None`（注释自认"runtime 未初始化时不拦截"）；`:93-95` `tool.permissions` 为空则连循环都不执行 → 无 manifest 白名单、无租户权限点任何校验。

【问题描述】`ToolCenter.execute` 是 prompt 驱动 Agent 直连工具的唯一闸门（`prompt_agent.py:108-110` 不经过 `runtime.call_tool` 的 `assert_tool_allowed` 前置，直接依赖 center 内部校验）。而该闸门的生效条件是"全局 runtime 已初始化 + DB 正常 + 该工具声明了 permissions"三者的叠加：
- 任一条件不满足（启动早期、后台任务、DB 瞬时抖动、未来注册了不声明 permissions 的新工具）→ **工具照常执行且无任何权限校验**；
- manifest.tools 白名单（对自定义 Agent 最关键的一道闸）实际上只在内层 `assert_tool_allowed` 里执行，而它只在上述条件全满足时才被调用。

【影响】安全机制从"默认拒绝、显式放行"退化为"条件满足才检查"，存在系统性静默失效面；对"工具无 permissions 即无条件放行"的隐性约定，任何后续工具注册都可能无意间绕过 RBAC。

【修复建议】fail-closed 化：权限检查异常时**拒绝执行并记 error 日志**（启动期若需容忍，用显式 `allow_unchecked=True` 参数而非吞异常）；`agent_name` 有值时在 center 无条件执行 manifest 白名单校验（与 `tool.permissions` 解耦）；无 permissions 的工具默认拒绝或要求显式标记。

---

## P2 轻微

1. **轨迹库持久化工具调用原始 params，可能沉淀密钥**：`hermes/evolution/trace.py:52-61` `record_tool` 把完整 `params` 写入 `agent_traces.steps_json`；而 `erp.get_stock`/`mes.get_production` 允许 params 携带 `token`（`tool/tools.py:159-160, 271`）。若真实 ERP/MES 接入后 token 经 params 传入，密钥会随 trace 落库且无保留期清理。建议对 params 做与 `result_excerpt` 同类的摘录/脱敏，明确密钥字段不入库。
2. **进程内状态跨 worker 不一致（当前单 worker 可运行，多 worker 部署即失效）**：`runtime._running`（`runtime.py:37`）、`AgentRegistry._agents`（`agent_registry.py:22-23`，有 DB 兜底）、`RBACSystem` 内存态（`rbac.py:51-53`）、`TaskQueue._local_queue`（`task_queue.py:34-35`）均为进程内单例；`main.py:661` 单 worker 启动无碍，但扩 `--workers>1` 后会话复用校验（`runtime.py:56-70`）、RBAC 授权即时性、内存队列消费都会互相不可见（注册中心靠 DB 兜底 `agent_registry.py:107-142`，会话/权限无兜底）。建议在部署文档明确单 worker 约束或迁移共享存储。
3. **工具错误信息回显内部实现细节**：`FunctionTool.execute` 统一 `{"ok": False, "error": str(e)}`（`tool/tools.py:41-46`），异常文本（DB 表结构、连接 URL、内部堆栈片段）经 `prompt_agent.py:115`/`trace.py` 回灌给 LLM 并可能进最终答复/轨迹。建议对外只暴露"内部错误"类目，明细进日志。
4. **GEPA 候选内容受"用户可控失败样本"影响，人工门预览失真**：`gepa.py:102-118` 把 trace 的 task_input（用户可控文本）喂给 LLM 生成候选 Soul/技能；护栏仅做关键词锚点 ≥30% 与体积校验（`evaluator.py:106-132`）；审批列表只展示截断 800/1200 字符（`service.py:52-53`）而 apply 写全量。提示注入有经"管理员审批"持久化到 Soul/技能（进而注入所有后续 prompt）的间接路径，纵深不足。建议审批页展示完整 diff + 触发样本告警。
5. **tool_code 候选"passed/score=1.0"措辞有误导**：`gepa.py:301-349` AST 静态校验通过即返回 `passed=True, score=1.0`（`_test_tool_code` 明示"未执行"），审批 UI/报告若直读 score 会误以为已通过运行时测试。安全方向正确（不执行 LLM 代码、只产 `.patch.md`），但建议把字段改为 `static_passed` 并附人工 review checklist。
6. **email.send 对收件人/内容零约束，且属默认开放权限点**：`tool/tools.py:89-98` 接受任意 `to`/`content`/`subject`；配合 P1-3 的默认全开，提示注入/滥用可把企业 SMTP 当垃圾/钓鱼中继。建议加收件人域名 allowlist、发送确认或配额（若产品定位 SaaS 多租户，此条建议上调）。
7. **短期记忆 key 仅 session_id，跨 Agent 复用同 session 会串记忆**：`memory/memory_system.py:45-62` key=`hermes:short:{session_id}`，不含 agent；`runtime.py:56-70` 会话复用只校验 company、不校验 agent_name 一致——同一 session 先后跑不同 Agent 会读到对方对话（租户内串扰，低危）。建议 key 含 agent_name 或复用校验加 agent 一致性。
8. **会话复用 check-then-act 无锁**：`runtime.py:56-70` 先查后写 `_running`，同 worker 多线程（FastAPI 同步端点的线程池）并发下两企业可用同一 session_id 竞态覆盖。建议 `start_agent` 内加锁或原子化。
9. **默认/降级凭据与中间件配置提示**：`config.py:112`/`rabbitmq.py:40` RabbitMQ 默认 `amqp://guest:guest@localhost:5672/`；TaskQueue 在 broker 不可达时静默降级内存/Redis（`task_queue.py:40-72`）——生产应显式配置并核对部署环境未用默认凭据（同 A 报告结论：需服务器侧核实）。
10. **轨迹/记忆长期留存无保留策略**：`agent_traces`、`agent_skills`、短期记忆（Redis 保留 20 条）与 evolution 各表无清理/归档策略，随使用量线性增长且含对话全文（`runtime.py:124-133`、`store.py:16-38`）。建议按租户设置留存期与归档任务。

---

## 架构与可维护性观察

- **执行路径分叉导致同一 RBAC 语义两套实现**：`runtime.call_tool`（`runtime.py:170-183`，先 `assert_tool_allowed` 再 center 内部再校验一遍）与 prompt-agent 直连 `center.execute`（`prompt_agent.py:108-110`，只靠 center 内部）是两条入口，前者传 `tenant_id`、center 内部又按 `company_id` 命名参数调用同函数（`center.py:85,89`）——目前 `tenant_id==company_id` 时行为一致，但参数语义混乱是 P1-5 fail-open 与后续回归的温床。建议收敛为单一 "Tool 执行入口 = 无条件 RBAC 闸门"。
- **人为门（Human-in-the-loop）设计整体正确**：GEPA 产出永远 pending（`gepa.py:196-219`）、approve 是唯一 apply 路径（`service.py:113-127`）、tool_code 只产 patch（`service.py:89-109`）、skill 提取按阈值+加锁模板兜底（`extractor.py:116-149`）——自进化"不自动生效"这条红线守住了，是模块最亮的部分。
- **Soul 双层（base/override）文件隔离 + 指纹隔离旧技能**（`soul.py:4-10, 246-283`）设计清晰；`_safe_name` 统一清洗 agent 名，规避了文件层路径注入。
- **重复/可简化点**：`agent_registry.py` 中 manifest 重建逻辑在 `load_custom`/`_load_one_from_db`/`list_agents` 三处复制（`:36-46, :125-134, :156-165`）；RBAC `_db_load`/`_db_persist` 每次操作都开 session（`rbac.py:63-100`），高频 grant/revoke 场景有 N+1 风险；错误处理风格不统一（部分函数返回 dict 错误、部分 raise、部分吞异常记日志）。
- **依赖注入被绕过**：多个模块直接 `from hermes.runtime import get_runtime` 反查全局单例（`center.py:80`、`rbac.py:157`、`agents/service.py:98`），使单测/独立 worker 场景必须依赖全局状态初始化顺序，也是 P1-5 fail-open 的土壤。

---

## 结论

Hermes 内核在**执行安全**上做了正确取舍：LLM 生成的代码从不被无沙箱执行（tool_code 仅产 patch 文件并人工合并）、Soul 文件名统一清洗、人为审批门完整，这三点使"自进化任意代码执行"类 P0 不存在。真正的风险集中在**多租户注册表与权限模型的语义缺口**，而非单点漏洞：Agent 名字空间全局唯一且可被任意企业覆盖/注销/读取（P1-1/P1-2）、RBAC 默认全开 + 空工具白名单反而全给（P1-3）、单企业管理员可触发全平台自进化（P1-4）、Tool 唯一执行闸门存在 fail-open 路径（P1-5）。这四类问题同源——**"公司级数据过滤（require_company）已到位，但 Agent/权限/进化这些平台级共享资源缺少企业维度归属与默认收紧策略"**。建议修复顺序：P1-1/P1-2（agent_manifests 加 `(company_id, name)` 复合唯一 + 属主校验）→ P1-3（RBAC 缺省收紧、空 tools fail-closed）→ P1-4（trigger 限平台超管）→ P1-5（center fail-closed）。A 报告与 B 报告合看：外层 API 租户过滤质量高，但**注册表/权限/调度共享面**是下一轮多租户审计的优先区域。
