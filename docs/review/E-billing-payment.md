# E · 支付/账单/开票闭环 代码审查报告

> 审查性质：只读静态分析（未启动服务/未跑测试）。审查时间：2026-09（模块近期大改后）。
> 审查视角：安全（越权/伪造/鉴权/篡改）＋正确性（状态机/并发/金额）＋可维护性（provider 抽象/演示与真实路径切换）。

## 范围 / 阅读文件清单

| 文件 | 角色 |
|---|---|
| `backend/api/billing.py`（505 行，全文） | 客户侧：套餐/账单/支付（凭证提交）/开票信息/开票申请 |
| `backend/api/platform.py`（662 行，全文） | 平台侧：账单审核 review_bill、开票 issue/reject、收款账户/发票平台配置、财务统计 |
| `backend/utils/invoice_provider.py`（202 行，全文） | 发票平台抽象：mock（本地 PDF）/百望云/诺诺占位 + 工厂 |
| `backend/tasks/billing.py`（52 行，全文） | Celery 24h 缓冲过期自动取消任务 |
| `backend/tasks/celery_app.py` | beat 调度（expire-grace-bills 每小时） |
| `backend/constants/plans.py` | 套餐定义（价格/周期/容量） |
| `backend/models.py`（Bill / InvoiceRequest / Company / SiteContent / User） | 表结构 |
| `backend/auth/deps.py` | 当前用户/租户/角色判定 |
| `backend/database.py` | 引擎/会话/轻量迁移 |
| `backend/main.py`（静态挂载段） | /static/site 匿名挂载 |
| `backend/utils/trade.py`（202 行） | **与支付模块无关**（外贸汇率/HS/运费/关税估算工具，无资金流），仅略读确认后排除 |
| `frontend/src/pages/Billing.jsx` / `PlatformAdmin.jsx`（关键段） | 付款弹窗演示警示、审核/开票交互 |
| `docs/支付体系与开票方案建议.md` | 设计意图（演示回退为既定设计、遗留事项清单） |

状态机总览：`upgrade 生成 unpaid → pay 上传凭证 → pending_review（套餐临时生效，24h 缓冲）→ 财务 review：approve=paid（正式生效）/ reject=回退；过期任务=expired（回退）`；开票：`paid 账单 → invoice-request(pending) → platform issue=issued / reject=rejected`。

---

## 发现统计：P0 0 / P1 6 / P2 9

未发现可直接稳定触发（无需竞态时序/无外部前提）的 P0 级资损或越权；账户与租户校验、金额服务端定价这两条主线是健康的（详见"结论"）。但**整条支付状态机没有任何并发防护（无锁/无 CAS/无版本列），所有"读取-判断-写回"在双进程（API + Celery worker）下都存在竞态窗口，涉及钱的方向一旦撞上即双向资损**，其中审核×自动取消、重复开票两处优先修。

---

## P0 严重

未发现。（无证据表明存在可稳定触发的资损/越权/数据损坏；并发类问题按触发条件归入 P1。）

---

## P1 中等

### P1-1 ｜【backend/api/platform.py:342-388 × backend/tasks/billing.py:21-50】财务审核与 24h 自动取消并发竞态：双向资损
**问题描述**：`review_bill` 与 `expire_grace_bills` 是两个独立进程（FastAPI 服务与 Celery worker，各自独立 DB session），对同一行 `bills` 都是"先 SELECT 校验 `status == 'pending_review'`，后整行 UPDATE 覆盖"，**没有任何锁、乐观锁或版本列**。
- 时序 A：task 已读到过期 pending_review 账单 → 财务恰好 approve 并 commit（paid，钱已确认到账）→ task 随后 commit（`_revert_plan` 回退套餐 + `status='expired'`，tasks/billing.py:33-36）→ **已收款客户被标记 expired 且套餐被回退**（资损/客诉，approve 分支注释"套餐已在提交凭证时临时生效，正式确认无需重复应用"（platform.py:362）意味着事后无人再补 apply）。
- 时序 B：财务先读 pending_review → task 先 commit（expired+回退）→ 财务 commit `status='paid'`（UPDATE 无 `WHERE status=` 条件）→ **缓冲已过期、实际未付款的客户被标记 paid，白嫖正式套餐**。
**影响**：双向资损，且状态与公司套餐永久错乱（回滚需人工修库）。触发概率取决于任务执行瞬间与人工点击重合（每小时的窄窗口），但一旦发生无任何防线。
**修复建议**：所有状态迁移改为**条件更新**：`UPDATE bills SET status='paid', paid_at=... WHERE bill_no=? AND status='pending_review'`，检查 `rowcount==1` 才继续（reject/expire 同理）；或将 read-verify-write 包进同一事务并用 `SELECT ... FOR UPDATE`。approve 通过后建议复核/重算公司套餐（不依赖"提交时已生效"的假设）。

### P1-2 ｜【backend/api/billing.py:315-384】`POST /billing/pay` 非幂等：并发双击/重放可致套餐时长翻倍、双凭证
**问题描述**：提交凭证接口对 `unpaid` 状态的判断与后续写库之间无并发保护。两个并发请求可同时通过 `bill.status != "unpaid"` 检查（billing.py:332-334），随后各自：写一张凭证文件、`_apply_plan`（billing.py:363）基于当时读到的 `c.plan_expires_at` 叠加 365 天、commit。若第二个请求在公司对象上读到的是第一个请求已提交的到期日，则**套餐到期时间被叠加两次（多送一年）**；同时留下两张凭证图、`voucher_path`/`voucher_note` 互相覆盖。
**影响**：双击/网络重放 → 平台资产损失（免费多送订阅期）；审核界面出现双凭证造成对账困惑。
**修复建议**：状态抢占用原子条件更新（`UPDATE bills SET status='pending_review', ... WHERE bill_no=? AND status='unpaid'`，rowcount==1 才允许后续），`_apply_plan` 在同一事务内执行；账单号唯一、凭证文件名已含随机串，无需其它改动。

### P1-3 ｜【backend/api/platform.py:585-638】`issue` 并发双击可重复开票；且"先调外部平台、后落本地库"顺序错误（真实平台接入前置改造项）
**问题描述**：`issue_invoice` 检查 `req.status != "pending"`（platform.py:597）后直接调 `provider.issue(...)`（platform.py:614），完成外部调用后才 `req.status="issued"` 并 commit（platform.py:620-626）。两点缺陷：
1. 两个并发请求可同时通过 pending 检查 → mock 下生成两张 PDF/两个发票号；**百望云/诺诺接入后 = 同一申请开出两张真实税务发票**（直接资损 + 税务事故）。
2. 顺序是"先外呼、后本地提交"：若外部平台开票成功而本地 commit 失败/超时/进程崩溃，库中申请仍是 pending → 财务重试 → **再次开票**。真实平台下无两阶段保障（outbox / 回调确认）就无法安全重试。
**影响**：当前 mock 演示下为双 PDF（浪费）；真实 provider 接入后即资损级。属于"真实平台启用前必须整改"项。
**修复建议**：① issue 入口先原子占位：`UPDATE invoice_requests SET status='issuing' WHERE id=? AND status='pending'`（rowcount 守卫，唯一通过者才外呼）；② 真实平台接入设计改为「本地置 issuing/记录 intent → 外呼 → 以平台流水号幂等对账/回调确认落 issued」，保证重复点击与重试不双开；③ `req_no` 与 `bill_no` 建立唯一约束（bill_no 唯一索引）作为兜底。

### P1-4 ｜【backend/main.py:654-657 × billing.py:60-62/355 × invoice_provider.py:45-47/126】转账凭证与发票 PDF 落在匿名静态目录 `/static/site`，无登录/租户鉴权
**问题描述**：`app.mount("/static/site", StaticFiles(...))` 对 `uploads/site` 全量匿名可下载（无任何鉴权中间件；main.py 自身注释也写明"报价 PDF 与营业执照改由鉴权接口提供"的安全策略，但凭证与发票未照此办理）。敏感物：
- 转账凭证回单影像 `/static/site/payments/{bill_no}_{8hex}.{png|jpg|webp}`（billing.py:355），含打款金额/付款账户/回单信息；
- 电子发票 PDF `/static/site/invoices/{req_no}.pdf`（invoice_provider.py:126），含买卖双方税号/银行账号/金额/地址电话，且 PDF 内还印有 `Bill No`（invoice_provider.py:66），可作为凭证文件名前缀的拼图。
文件名含 40+32 bit 随机串、目录不可列举 → 直接枚举难，但 URL 会出现在客户账单页/审核页的 `<a href>` 中（Billing.jsx:331、PlatformAdmin.jsx:521），客户下载后转发（财务群/微信/邮件）即泄露给未登录第三方，且**永久有效、无失效机制、无企业归属校验**。
**影响**：跨租户/匿名读取企业财务票据与回单（发票税号+银行账号属敏感 PII/财务数据），合规风险。
**修复建议**：凭证/发票迁出匿名静态目录，改经鉴权接口下发：`GET /billing/invoices/{bill_no}/voucher`、`GET /billing/invoice-requests/{id}/pdf`（登录 + `company_id == current` 校验）用 `FileResponse` 返回；平台侧接口加 `platform_admin` 校验。若保留静态，至少加"签名 URL + 时效"。

### P1-5 ｜【backend/api/billing.py:41-58/304-313/315 × backend/api/platform.py:600-626】演示能力在"真实环境"可被静默误用（收款账户回退 + 发票 provider 默认 mock），缺强门禁
**问题描述**：两处"演示数据回退/默认"在正式收费后没有服务端强门禁：
1. **收款账户**：`_get_pay_account` 未配置时回退演示账户（"AI Factory 科技… 工商银行… 3901…"，billing.py:41-58），`GET /billing/pay-account` 虽返回 `is_demo`（billing.py:310-312），前端也有警示文案（Billing.jsx:455/471），但 `upgrade`/`pay` 全流程畅通——若平台方上线时忘记在「财务管理 → 收款账户设置」维护真实账户，**客户会照演示账号真实打款**（账号不存在→退款纠纷；若账号恰好为他人持有→资金被骗），且系统无任何阻断。
2. **发票平台**：`invoice_config` 未配置时 `provider` 默认 `"mock"`（platform.py:602，invoice_provider.py:199 同样默认 mock），一键开票会**静默生成"演示发票"并回填 `invoice_no=DEMO...`、状态置 `issued`**——客户侧看到"已开票+发票号+PDF"，实际拿到的是页面上写着"本文件为演示发票，不具备税务效力"（invoice_provider.py:59）的假发票。财务若未察觉，将造成税务合规事故与客诉。
**影响**：正式收费期的资损/客诉/税务合规事故（两处均属"演示分支被真实环境误用"，与 git log 中"对公账户未配置回退演示数据"的已知背景直接对应）。
**修复建议**：① 上线开关：`pay-account`/`upgrade`/`pay` 在 `is_demo` 时返回 503/明确报错（或平台配置 gate 字段，配置真实账户前禁用收款流程），而非仅前端文案；② `issue_invoice` 在 `provider == "mock"` 时要求二次确认（dialog 明示"演示票无税务效力"）或将 mock 限定在 `APP_ENV != production`；③ 配置页对"当前为演示开票/演示收款"给出常驻红色警告条。

### P1-6 ｜【backend/constants/plans.py:16-33 × backend/api/billing.py:105-110/278-282 × frontend/src/pages/Billing.jsx:246-247】套餐定价三要素自相矛盾：`price`/`period="月"` 与 `PLAN_DAYS=365` 不一致
**问题描述**：starter/professional 标价 `1999/4999`、`period: "月"`（plans.py:16/21），前端套餐卡显示"¥1,999/月"（Billing.jsx:246-247）；但 `PLAN_DAYS` 三个套餐全为 365（plans.py:33），升级账单 `amount = price` 单期金额（billing.py:278）、`_apply_plan`/`period_end` 到期时间 +365 天（billing.py:105-110/282）。即**一次收 1999 元，授予 365 天服务，却对外标注"1999 元/月"**。若产品意图为月付，则每年少收约 11 个月收入（平台资损）；若意图为年付，则前端/合同展示"元/月"构成误导（客诉/虚假宣传风险）。同仓库内技能包计费口径是"元/月"（main.py:133 附近 1299、Billing.jsx:284/295），与底座套餐周期不同轨，财务口径混乱。文档（docs/支付体系与开票方案建议.md）未对月/年做澄清。
**影响**：收入模型失真，金额类 bug 中最直接的一条（每单差 11/12 的收入或对客户的错误承诺）。
**修复建议**：确定唯一计费周期语义后统一：若年付 → `period="年"`、文案与前端同步改"元/年"（或标"一次性买断 365 天"）；若月付 → `PLAN_DAYS=30` 且重新设计金额（12 期/自动续费另议，见方案文档 P2 阶段）。建议在 `PLANS` 增加"period ↔ days ↔ 展示价格"一致性校验（启动断言或单元测试），杜绝再次分叉。

---

## P2 轻微

1. **【platform.py:403-404】财务总览"待收金额/待收笔数"统计 `status == "pending"`，而 bills 状态枚举并无 `pending`**（models.py:576-581 为 unpaid/pending_review/…），恒为 0。应统计 `unpaid + pending_review`。（`total_revenue` 按 paid 统计正确。）
2. **【billing.py:446-450 + platform.py:641-654】开票申请被驳回后无法修改重提**：`create_invoice_request` 的重复检查不分状态，rejected 也拦截；且无"重新申请"接口 → 抬头/税号填错被驳回即流程死锁（只能平台手工删库）。建议：rejected 允许重提（新建覆盖旧申请，或支持客户撤回后重提）。
3. **【billing.py:257-262 + tasks/billing.py】`unpaid` 账单无过期/取消机制**：客户生成账单后不付款，账单永久悬挂，且 upgrade 的"处理中账单"拦截包含 unpaid（billing.py:259）→ 后续任何升级被幽灵账单永久阻塞。建议：unpaid 加自动过期（如 7 天）或允许客户主动取消未支付账单。
4. **【billing.py:447-450】并发开票申请绕过重复检查**：dup 检查是应用层 SELECT，`invoice_requests.bill_no` 无唯一约束 → 双击可建两条申请（platform 可能开出两张票）。建议 bill_no 加唯一索引 + 捕获 IntegrityError（与 P1-3 同族兜底）。
5. **【models.py:589/617（及 Quotation 247 等）】金额列全用 `Float`**：当前金额源为整数价暂无损；一旦引入折扣/按量计费/价税分离即精度风险。建议金额统一 `Numeric(12,2)` + Decimal。另 mock PDF 无价税分离与金额大写（真实电子发票格式要求），真实平台对接前需补齐票面要素。
6. **【billing.py:336-350】凭证仅校验 `content_type` 头、不校验真实文件内容**：可上传伪装 image/png 的任意字节。文件名随机+扩展名固定，无直接 XSS/RCE，但降低伪造凭证成本。建议解码校验（imghdr/Pillow）或至少校验 magic bytes。
7. **【platform.py:617-618】`issue` 失败把内部异常原文透传前端**（`detail=f"开票调用失败：{e}"`）：可能泄露平台/网络/第三方响应细节（仅 platform_admin 可见，低危）。建议统一脱敏文案并落日志。
8. **【人工审核环节（流程提示，非代码缺陷）】凭证伪造无法由代码识别**：approve 只凭财务肉眼看回单图（platform.py:356-363，前端无对账输入框），PS/截图伪造回单即可通过（系统无银行流水交叉验证）。属对公转账人工模式固有风险，建议审核 SOP 强制"登录网银核对入账 + 备注栏记录对账凭据"，并把"已核对到账"作为 approve 必填项写入 review_note；上传侧登记原图哈希留痕。
9. **【tasks/celery_app.py:46-49（运行前提）】24h 自动取消依赖 Celery beat + Redis 常驻**：若生产部署未同时启动 worker/beat，`expire_grace_bills` 不执行 → 未审核通过的客户无限期享受临时套餐（平台收不到款）。属部署配置风险，上线清单需确认 worker 与 beat 均在跑（或改用 FastAPI lifespan 内嵌扫描任务兜底）。

---

## 架构与可维护性观察

- **发票平台抽象层干净**（invoice_provider.py）：统一 `issue(req, config) -> {invoice_no, pdf_url, provider, provider_req_no}` + 工厂，mock 与真实平台接口契约一致；百望云/诺诺占位在未配置/未接入时**显式抛错**而非静默 mock，方向正确。主要缺口在调用侧（platform.py issue）：缺少"provider==mock 时的强提示/确认"与"外呼前的状态抢占"（见 P1-3/P1-5）。
- **金额与计费口径**：金额服务端定价（UpgradeIn 只有 company_id+plan，不信任前端金额）✅；无折扣/改价通道 → 无前端金额篡改面 ✅。但价格-周期-天数数据源（plans.py）缺乏一致性校验（P1-6），且金额列 Float（P2-5）是后续引入折扣时的隐患。
- **越权防线整体健康**：客户侧全部按 `current["company_id"]` 匹配（billing.py:145/198/246/328/390/441/492 等），平台侧统一 `_require_platform`；`get_current_user` 对伪 platform_admin（带 company_id 的超管）有专门拦截（auth/deps.py:39-48）；普通 staff 可看本公司账单/开票信息但不可操作（写操作均有 `_require_admin_billing`）——未见 IDOR。
- **幂等与状态机**：`upgrade` 的"处理中账单拦截"（billing.py:257-262）、bill↔invoice 一对一语义、快照回退（prev_snapshot）设计正确；系统性短板是**所有状态迁移均无乐观锁/CAS**，多个写路径（pay、review、expire、issue、invoice-request）同一模式，建议在 Bill/InvoiceRequest 增加 version 列或统一"条件 UPDATE + rowcount"封装，一次性覆盖 P1-1/2/3 与 P2-4。
- **审计日志**：升级/支付/审核/开票/配置五类动作均写 audit_log 且 try/except 包裹不阻断主流程 ✅。
- **遗留**：无退款与自动续费（方案文档明确后续阶段，符合预期）；静态凭证存储方案文档也标注"生产可改对象存储"——与 P1-4 配套，建议一并纳入上线清单。

---

## 结论

该模块的**单线程正确性与权限隔离写得相当扎实**：金额全由服务端按套餐定价、企业间 IDOR 全部拦截、角色门禁完整、快照回退语义清晰、演示路径有前端警示与文档背书——因此没有发现可直接触发的 P0。真正的系统性风险在**并发**：账单状态机五条写路径（pay/review/expire/issue/request）全部是"读取-判断-覆盖写回"且无任何锁或条件更新，API 服务与 Celery worker 双进程下，审核×自动取消、重复支付、重复开票三个场景都可能在特定时序下造成资损；其次是**演示能力缺服务端强门禁**（演示收款账户回退、发票 provider 默认 mock 静默开出演示票）与**敏感凭证/发票匿名静态暴露**、**套餐月/年定价口径矛盾**——这五类应在任何"真实收费"动作前处理完毕。整体判断：可以作为演示/试运营版本运行，但距"正式收款上线"还差一轮并发加固 + 演示门禁 + 文件鉴权改造。

### 优先修复建议（Top 2）
1. **P1-1/P1-2/P1-3 并发族**：给 Bill/InvoiceRequest 状态迁移全部改"条件 UPDATE（WHERE status=期望前态）+ rowcount 校验"，一个改动覆盖审核×过期竞态（双向资损）、重复支付翻倍延期、重复开票三处；这是唯一可能造成实际资金/服务损失的一类缺陷。
2. **P1-5 演示门禁**：上线真实收款前，后端在 `is_demo`（收款账户未配置）时拒绝 upgrade/pay，`issue` 在 provider=mock 时强制二次确认——把"靠前端文案提醒、靠财务记得配置"变成系统级强制，避免客户打到演示账号或收到"已开票"的假发票。
