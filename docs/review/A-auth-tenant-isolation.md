# A · 认证与多租户隔离 代码审查报告

> 审查类型：只读静态代码审查（未启动服务、未跑测试、未修改任何项目代码）
> 技术栈：FastAPI + SQLAlchemy 2（ORM）+ PyJWT(jose) + bcrypt(passlib)，多租户 SaaS，SQLite（开发）/ PostgreSQL（生产）
> 审查模块：认证与多租户隔离（密码/JWT/依赖注入/登录注册/平台超管/企业管理/成员管理/模型/会话/配置）
> 审查日期：2026-09（代码现状，含 2026-08 多轮安全修复）

## 范围 / 阅读文件清单

主审文件：
- `backend/auth/security.py`（密码哈希）
- `backend/auth/jwt.py`（JWT 签发/校验）
- `backend/auth/deps.py`（当前用户解析、租户上下文、require_company）
- `backend/auth/router.py`（注册/登录/me，进程内限流）
- `backend/api/platform.py`（平台超管：统计/企业/认证审核/账单/开票/LLM 配置）
- `backend/api/company.py`（企业设置/执照上传下载审核/改密/操作日志/SMS）
- `backend/api/members.py`（成员邀请/接受/角色/停用/移除）
- `backend/models.py`、`backend/database.py`、`backend/config.py`

关联参考（多租户隔离模式一致性抽查）：
- `backend/main.py`（路由挂载/CORS/种子）；`backend/utils/audit.py`、`backend/utils/sms.py`
- `backend/api/agent.py`、`knowledge.py`、`crm.py`、`quotation.py`、`leads.py`、`dashboard.py`、`agent_chat.py`、`billing.py`、`channels.py`、`integrations.py`、`evolution.py`、`hermes.py`、`marketplace.py`、`cms.py`、`system.py`、`account.py`
- `backend/channels/email/smtp.py`、`backend/channels/email/sequence.py`、`backend/channels/wecom/robot.py`、`backend/channels/wecom/bot_client.py`
- `frontend/src/pages/PlatformAdmin.jsx`（核对平台审核 UI 是否展示执照原件）

## 发现统计：P0 0 / P1 3 / P2 10

未发现 P0（无未鉴权敏感数据读取、无跨租户数据读取型 IDOR——核心 CRUD 均带 require_company / company_id 过滤；无 JWT 算法/密钥级漏洞；密码哈希为 bcrypt）。

---

## P1 中等偏高

### P1-1｜跨租户企微群消息推送（可借任意企业机器人发任意内容）
- **文件**：`backend/api/channels.py:166-173`（`wecom_robot`），配合 `backend/channels/wecom/robot.py:14-19`、`channels/config_utils.resolve`
- **问题**：`cid = data.company_id or current["company_id"]` —— `data.company_id` 完全由调用者控制且**未校验是否等于当前用户所属企业**（对照同文件 `channel_config_save` L212、`email_logs` L152 都做了 `!= current["company_id"]` 校验，此端点漏了）。`send_robot_text/markdown` 内部按 `company_id` 经 `resolve()` 读取**该企业自己配置的企微群机器人 webhook key** 并发起请求。
- **影响**：任何已登录用户（含 staff、甚至只经过 `require_verified` 校验的低权账号）可枚举/猜测 `company_id`，把任意内容（钓鱼、骚扰、抹黑、外部链接）推送到**其他企业已接入的企微群**——借官方机器人身份对外发布，攻击面是跨租户的「动作注入」+ 借力发送，可造成目标企业客户/员工信任链滥用与运营干扰。企业机器人 webhook key 本身不被读出（只被服务端使用），故未定 P0。
- **修复建议**：`wecom_robot`（以及同类以 body 传入 company_id 的渠道端点）先执行 `if data.company_id is not None and int(data.company_id) != int(current["company_id"]): raise 403`，或直接强制使用 `current["company_id"]` 不允许 body 覆盖；统一收敛到 `require_company`。

### P1-2｜email_followup 端点未绑定当前租户：跨租户改线索状态 + 借他企业名义发跟进邮件
- **文件**：`backend/api/channels.py:116-120`（`email_followup`）→ `backend/channels/email/sequence.py:217-279`（`send_follow_up`）
- **问题**：`send_follow_up(data.lead_id, data.company_id, step=...)` 直接把调用者提交的 `lead_id` + `company_id` 透传给发送函数，**无任何 `require_company` / 归属校验**。`send_follow_up` 内部按 `(Lead.id == lead_id, Lead.company_id == company_id)` 过滤查询（L225-226）——攻击者只要枚举对方企业 id + 自增 lead_id，即可命中**其他企业的线索**，随后发生两件事：① L269-275 把对方线索的 `follow_up_step / follow_up_at / variant / status(last_error)` **跨租户写改**（打乱其邮件序列状态机）；② 以其名义向该线索客户邮箱发送第 N 封跟进邮件（附件取对方企业与客户名匹配的报价 PDF，L242-250）。对比 `backend/api/leads.py` 的 `follow_lead/send_sequence`（L244-270）均先 `require_company`，此端点属**漏检旁路**。
- **影响**：跨租户数据篡改 + 借目标企业身份（域名/内容/附件）向其客户发信；可用于骚扰客户、干扰对手跟进节奏、借力发送垃圾邮件。属于「认证后跨租户写」，注册开放时任何账号即可利用。
- **修复建议**：进入 `send_follow_up` 前先 `require_company(data.company_id, current)` 并校验 lead 归属（`Lead.id` + `current.company_id` 双重条件查询后再发信）；或删除该端点、统一走 `leads.py` 已隔离的路径。

### P1-3｜平台超管执照审核链路无法查看执照原件（「company_id 返回 0」修复的直接遗留）
- **文件**：`backend/auth/deps.py:39-48`（platform_admin 恒返回 `company_id=0`）；`backend/api/company.py:213-223`（`download_license`）；`backend/api/company.py:231-239`（`approve_license`）；`backend/api/platform.py:232-244`（`/platform/verifications`）；`frontend/src/pages/PlatformAdmin.jsx:279-323`
- **问题**：平台超管在 deps 中被压成 `company_id=0`，但业务端点仍在做整型比较且**未给超管豁免**：
  1. `download_license`（L217）`if company_id != current["company_id"]: 403` → 超管请求任何真实企业（id≠0）的执照图**恒 403**；
  2. `approve_license`（L236-239）先要求 `role == "platform_admin"`，随即又要求 `company_id == current["company_id"]`（=0）——**自相矛盾，任何企业都进不去**，该端点形同死代码（审核真正走 `/platform/companies/{id}/verify`）；
  3. 平台端 `/platform/verifications` 只回 `has_license` 布尔、不回图片路径/内容；
  4. 前端 `PlatformAdmin.jsx` 审核队列**根本不展示执照图片**，只有「通过/拒绝」按钮。
- **影响**：执照人工审核全程**无法查看原件**（唯一图片端点对超管 403、平台端点不给图、前端不调图）——审核只能盲点，认证门（`license_verified` 决定业务功能是否可用）存在被误审通过无证/不符企业的风险；同时 `approve_license` 成为不可达死代码。这是已知修复「平台超管登录 company_id 返回 0」后未同步改造业务层判断的直接遗留。
- **修复建议**：① `download_license`/`approve_license` 增加超管豁免（`current["role"] == "platform_admin"` 时放行目标企业）；② `/platform/verifications` 返回可鉴权访问的图片 URL 或复用豁免后的 download；③ 前端审核卡片展示执照图；④ 删除重复且不可达的 `/company/license/approve`，平台审核统一收敛到 `/platform/companies/{id}/verify`。

---

## P2 中等偏低 / 一致性与纵深防御缺口

1. **登录不检查账号状态**：`backend/auth/router.py:121-126` —— `login` 只比对邮箱+密码，**不检查 `user.status == "disabled"`**；被成员管理停用或被平台挂起（`platform.py:283` 置 disabled）的账号仍能登录成功并拿到 token（前端进入后每个业务请求才被 `deps.py:35-36` 拦成 403）。建议 login 即返回 403「账号已被停用」，避免无效 token 分发与前端误导。

2. **suspend 恢复不幂等，会复活被单独停用的成员**：`backend/api/platform.py:282-288` —— `suspend_company` 把全企业 user 置 `disabled`，恢复时又无差别全置 `active`，此前被管理员单独停用的成员一并复活；且企业被挂起期间重新激活的成员状态被覆盖。建议挂起/恢复用企业级标记（如 company.status 或 license_status）而非扁平化覆盖 user.status，恢复时只恢复由本次挂起置位的那批用户。

3. **成员接受邀请无密码策略**：`backend/api/members.py:111-117`（`accept_invite` 直接 `hash_password(data.password)`，无长度/复杂度校验），与注册端 `auth/router.py:76-78`（≥8 位且含字母+数字）不一致 —— 被邀请成员可设 `"1"` 这类弱口令。另该端点无 IP 限流（注册端有 10 次/小时）。建议复用同一密码策略函数并在 accept 处执行。

4. **密码策略三处不一致**：注册 ≥8 位含字母数字（`auth/router.py:76-78`）；`/company/password` ≥6 位纯长度（`api/company.py:330`）；`/account/change-password` ≥6 位纯长度（`api/account.py:79`）。改密路径实际允许把密码降到比注册门槛更弱。建议统一策略并沉淀到 `auth/security.py`。

5. **改密后旧 JWT 仍有效至过期（最长 7 天）**：`backend/auth/jwt.py:11-20` 无 `jti`/token 版本号，`deps.py` 只查用户与 `status`，不校验密码变更时间/会话版本 —— 密码泄露后攻击者持有的旧 token 在 `ACCESS_TOKEN_EXPIRE_DAYS`（config.py:53，默认 7）内持续可用。建议 user 表加 `token_version`（或 `password_changed_at`），改密时自增，deps 校验 token 声明与 DB 一致。

6. **upload_license 无 admin 门禁 + 状态机不一致**：`backend/api/company.py:160-210` —— 任何已登录成员（staff）都可覆盖本企业执照图并把 `license_status` 置 `pending`（L196-197），却**不重置 `license_verified`**：已认证企业可被 staff 刷成 `pending` 而业务能力（`require_verified` 只看 `license_verified`）不受影响，形成 `pending + verified=True` 不一致态并干扰平台审核队列。建议 upload 至少限 admin，且状态切换时保持 `license_status` 与 `license_verified` 联动（pending 即降级或等待 reject 才降级的二选一，写清楚）。

7. **/account/change-password 验证码无失败次数锁定**：`backend/api/account.py:87-99` 验证码比对失败不计数、不失效（对比 `api/company.py:425-434` 的 `/company/sms/verify` 有连续 5 次作废机制）。6 位数字码在 10 分钟窗口内可无限尝试；虽叠加旧密码校验（L84），纵深不足。建议复用同一失败计数逻辑。

8. **注册/邀请接口邮箱枚举**：`backend/auth/router.py:81-82`（「该邮箱已注册」）与 `backend/api/members.py:72-74`（invite 时「该邮箱已注册」）对**全平台**已注册邮箱给出明确可区分响应，配合换 IP 即可批量探测平台邮箱池（登录端点已统一文案，注册/邀请未做）。建议与登录一致返回模糊文案，或对注册失败的邮箱探测告警。

9. **注册两段提交可残留孤儿企业**：`backend/auth/router.py:84-96` —— 先 `commit` 企业再 `commit` 用户；同邮箱并发注册时第二个 commit 抛 `IntegrityError`（email unique）→ 用户收到 500，但**空壳企业已落库且永无管理员**。建议：先查后插加唯一约束兜底 + 异常回滚已建企业（或单事务含 email 预检）。

10. **渠道外发缺乏企业级审计归属与频控**：
    - `channels.py:98-105` `email/send` 使用全局 SMTP 且模型无 company_id 字段 → `smtp.py:80` `_log_email(company_id or 0)` 把日志记到 company 0，企业日志页（`channels.py:147-163`）查不到自己发过的开发信（审计归属丢失）；
    - `channels.py:85-89` `whatsapp/send` 用平台统一号对任意 `to` 发送（仅 `require_verified` 拦未认证企业）→ 认证用户可借平台号向任意号码发消息，有平台号被举报/封禁的滥用风险；
    - `channels.py:92-113` `email/generate|sequence` 登录即可调用且**无限流**，每次触发 LLM 生成 → 注册开放下可脚本化烧平台 LLM 额度。
    建议：外发统一带 `current.company_id` 记日志、加企业级频控/额度、对敏感 LLM 生成端点限流。
    - 附带：`channels.py:183-186` `wecom/bot/test` 无企业归属校验（任意成员可用任意 bot_id/secret 触发订阅测试，而 `bot_client.py:10` 注明同 bot 单连接、测试会踢掉常驻连接）——至少应限本企业配置的 bot 且限 admin。

---

## 架构与可维护性观察

- **多租户过滤整体质量高**：核心业务模块（agent/knowledge/crm/quotation/leads/dashboard/agent_chat/marketplace/evolution/hermes/system/integrations）均使用 `require_company`（`auth/deps.py:62-65`），且对象查询普遍带 `company_id` 复合条件（如 `agent.py:106-107`、`knowledge.py:150-151`），漏检集中在**少数字段透传型渠道端点**（P1-1/P1-2）。`role` 白名单、DB 层 CheckConstraint（`models.py:80-83`）、token 仅信任 `sub`、角色每次回源 DB 读取（`deps.py:32/55`，不信任 token 内 role/company_id 声明）都是正确的安全设计。
- **两套租户校验风格并存且语义分裂**：`require_company(company_id, current)` 与散落各处的手写 `if company_id != current["company_id"]: 403` 并存（company/members/billing/channels/config 等）。手写风格**全部不豁免 platform_admin**（超管 company_id=0 被压平后直接撞墙），P1-3 即由此产生。建议把「platform_admin 豁免 / 企业成员强制相等」的语义收进 `require_company` 一类统一 helper。
- **admin 判定三套实现并存**：`utils/audit.py:16-19` `require_admin`（只认 `"admin"`）、`evolution.py:29-32` / `hermes.py:31-34` / `billing.py:88-91` 局部 `_require_admin`（认 `admin` + `platform_admin`）、`platform.py:33-35` / `cms.py:119-122` `_require_platform`。语义不一致易踩坑（platform_admin 在 members/company 的 admin 操作里会被拒，属预期但难维护）。建议统一为一个 `require_role(current, roles)` + 集中路由前缀门禁。
- **platform_admin 的 `company_id=0` 哨兵值泄漏到业务层**：deps 把超管身份表达成数值 0 后，业务层到处做整型比较（P1-3、`company.py:86/172/217/264`、`billing.py:145/198/246...`），一旦某端点漏豁免就静默 403/或反向放行。建议 deps 返回结构带 `is_platform: bool`，业务判断用角色字段而非数字 0。
- **限流/防暴均为进程内内存 dict 且无锁**：`auth/router.py:29-33`、`company.py:39-40` 注释已自认「多 worker 需 Redis」。单 worker 部署下 append/遍历存在轻量竞态但可接受；上生产多 worker 前必须换共享存储，否则限流全部失效。
- **重复/死代码**：`/company/license/approve` 与 `/platform/companies/{id}/verify` 重复且前者不可达（P1-3）；`company.py:226-228 _schedule_auto_approve` 为废弃占位；`platform.py`、`company.py` 均出现 `from datetime import datetime` 重复导入（`platform.py:13/15`）。
- 正面确认项：CORS 未用 `*` + credentials（`main.py:516-526`）；SMS 验证码 `secrets.randbelow` 生成、接口不回显（`utils/sms.py:16-18`、`company.py:399`）；登录失败统一文案（`router.py:122-124`）；执照按企业 ID 命名空间落盘（`company.py:189-193`）且下载鉴权；上传大小先截断后读防内存撑爆（`company.py:180-183`）；发票/LLM/渠道配置密钥读取均脱敏或掩码（`platform.py:61-66/519-535`、`channels.py:196`）。

---

## 结论

该模块整体处于**「已做系统性安全加固、残留少量真实旁路与一处修复遗留」**的状态：多租户读取型隔离（最危险的跨企业数据访问）在全部核心 CRUD 上落实到位且写法统一，JWT 仅信任 `sub`、角色/状态每次回源 DB、密码 bcrypt、注册/登录限流与统一文案、DB 层角色约束均属正确做法，未发现 P0。真正值得立即处理的是三处 P1：① `channels/wecom/robot` 与 ② `channels/email/followup` 两个**字段透传端点漏掉租户绑定**（P1-1/P1-2），形成跨租户「借名义外发 + 数据篡改」旁路；③ 平台超管 `company_id=0` 修复未下沉到业务层，导致**执照审核全程无法查看原件**并把一个审核端点变成死代码（P1-3）。P2 层则以「密码策略三处不一致」「改密不吊销旧 token」「SMS/LLM 验证与频控纵深不一」等一致性缺口为主，均为低成本可收敛项。建议修复顺序：P1-2 → P1-1 → P1-3，随后统一密码策略与 admin/租户校验 helper，消除手写 `company_id` 比较。
