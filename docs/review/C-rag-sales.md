# C · RAG 知识库与 AI 获客 代码审查报告

- 审查性质：只读静态分析（未启动服务、未跑测试、未调用 embedding/LLM/向量服务）
- 审查日期：2026-09
- 代码版本基线：本机 H:\制造业 AI 员工平台全量开发\backend（含 migrate_rag_to_per_tenant / BM25 倒排索引改造后的现状）

## 范围与阅读文件清单

主审查文件（按优先级）：
- `backend\knowledge\upload.py`（上传：扩展名白名单/大小限制/路径）
- `backend\knowledge\parser.py`（PDF/Word/Excel 解析分发）
- `backend\knowledge\cad_parser.py`（ezdxf CAD 解析）
- `backend\knowledge\splitter.py`、`embedding.py`、`vector.py`、`rag.py`、`hybrid.py`
- `backend\api\knowledge.py`（知识库 API）
- `backend\sales\discovery.py`、`scoring.py`、`parse_inquiry.py`
- `backend\api\leads.py`（线索 API）

交叉验证文件：
- `backend\config.py`（UPLOAD_DIR / MAX_UPLOAD_SIZE_MB / QDRANT_*/EMBEDDING_* 配置）
- `backend\auth\deps.py`（get_current_user / require_company 租户语义）
- `backend\models.py`（Document / DrawingParam / Bm25Chunk / Lead / Customer 等）
- `backend\main.py`（lifespan、BM25 启动重建、中间件、静态挂载）
- `backend\services\llm_parse.py`（LLM JSON 输出容错）
- `backend\channels\email\sequence.py` / `smtp.py` / `generator.py`（跟进邮件实际发送路径）
- `backend\utils\compliance.py` / `trade.py`（导出合规/语言路由依赖存在性）

## 发现统计：P0 0 / P1 3 / P2 15

总体结论先行：**未发现可确证的 P0 级（跨租户数据泄露/越权/注入/RCE/XXE）问题**。租户隔离这条主线上（auth 依赖逐端点 require_company + 每租户物理集合 + 查询均带 company_id 过滤）实现是干净的；但向量库数据完整性、Qdrant 降级路径、文档解析资源上限三处存在会造成**静默数据损坏/丢失/资源耗尽**的 P1 级缺陷，且全部集中在「上传→入库」这条主链路上，建议优先处理。

---

## P0 严重

未发现证据充分、可确证的 P0 问题。说明：上传白名单仅按扩展名、无 MIME/魔数校验，但平台**不对外静态服务知识库原文件**（main.py 只挂载了 uploads/site 官网素材目录，见 main.py:651-657），解析产物仅为文本入库，未发现经文件内容直达执行/出网/越权的路径；XML 类解析走标准库 xml.etree / 库内实现，未见 XXE 面。

---

## P1 中等

### P1-1｜【backend\knowledge\vector.py:26,129-131,159-160】Qdrant 点 ID 用进程内自增计数器，进程重启后从头计数 → 新上传批量覆盖既有向量点（静默索引损坏）

- 问题描述：`_memory_counter = itertools.count(1)` 是模块级计数器；`save_vectors` 在 Qdrant 分支同样使用 `PointStruct(id=_next_point_id(cid), ...)` 生成点 ID（vector.py:159-160）。Qdrant 集合跨进程持久化，而后端每次重启后该计数器复位为 1。
- 影响：Qdrant 的 upsert 对同 ID 是"覆盖更新"。重启后任意一次新上传（假设 5 个 chunk）会用 ID 1~5 覆盖集合里历史最早的 5 个点——旧文档前几个切片被静默替换为新文档内容、payload.document_id 也被改写，旧文档删除时只能删掉残余切片，出现「跨文档内容混杂、检索结果错乱、删除不干净」的确定性数据损坏，且全程无任何报错。
- 修复建议：点 ID 改用与重启无关的稳定值——推荐 `f"{company_id}-{document_id}-{chunk_idx}"` 或 uuid4（每 chunk 一个）；或持久化计数器。修复后需对存量集合做一次全量重建（删集合重新上传/重灌）。

### P1-2｜【backend\knowledge\vector.py:85-104 + backend\config.py:79】Qdrant 内存降级硬编码开启、一旦降级永不重连、内存向量不落盘 → Qdrant 瞬时故障即静默吞掉企业知识库

- 问题描述：`QDRANT_FALLBACK_MEMORY = True`（config.py:79，无环境变量开关）；`_get_client()` 首次连通失败即置全局 `_use_memory=True` 并返回 None（vector.py:99-104），后续所有调用被 `if _client is not None or _use_memory: return _client` 短路，**永远不会再探测/重连 Qdrant**。降级期间 `save_vectors` 写入进程内存 `_memory_store`，文档状态仍被置为 "vectorized"（api/knowledge.py:85-87）。
- 影响：生产环境 Qdrant 任何瞬时不可用（重启窗口、网络抖动、发布期），期间所有企业上传的向量只进进程内存；进程重启后全部丢失，而 Document 表仍显示 vectorized、BM25 稀疏索引（SQL）仍在——表现为「重启后 AI 助手/工艺助手知识检索静默变空或仅剩稀疏命中」，无任何告警；多 worker 部署下各 worker 内存库互相不可见，检索结果随路由不一致。单次上传的"降级期数据丢失"是真实且无声的。
- 修复建议：① `QDRANT_FALLBACK_MEMORY` 改为环境变量显式开启并默认 False（演示环境自行开启）；② 降级只对"检索"放行、对"写入"报错（或写入降级时把 Document 状态置为 failed/pending 并告警）；③ 定期重探连接，恢复后把内存积压 flush 到 Qdrant 或提示重传。

### P1-3｜【backend\api\knowledge.py:55-91 + backend\knowledge\parser.py:13-65】文档解析/向量化入库全同步执行且无"抽取内容总量"上限 → 单请求内存/CPU/DB 资源爆炸（xlsx/docx 解压放大、超长 txt、超多页 PDF）

- 问题描述：上传 → 解析 → 切片 → embedding → 写 Qdrant + 写 bm25_chunks + 写 DrawingParam 全在一个 HTTP 请求内同步完成（api/knowledge.py:70-119）。解析侧没有输出总量控制：`parse_pdf` 逐页累加全文无页数/字符上限（parser.py:13-20）；`parse_txt` 全量读入（parser.py:53-55）；`parse_excel` 用 `pd.read_excel(sheet_name=None)` 把**所有 sheet 完整读入内存后才** `head(MAX_PARSE_ROWS)`（parser.py:45-49，截断不省内存）；docx/xlsx 是 zip 容器，50MB 上限（config.py:58）可被压缩放大数倍到数十倍后再解压解析。
- 影响：一个认证租户上传 50MB 恶意 xlsx（数值型数据压缩比极高、行数数百万）即可让单 worker 内存暴涨甚至 OOM 整进程（波及全部租户）；50MB 纯 txt ≈ 6 万+ chunk，同步 embedding（且 embedding 失败还走每 chunk 哈希降级）+ 6 万行 BM25 落库 + 6 万点 upsert 会让请求挂起数分钟并占满线程池；并发多上传可直接拖垮服务。Excel `head()` 在读完整个 sheet 之后才生效，说明"行数上限"防护形同虚设。
- 修复建议：① 解析前用 zipfile 预检 docx/xlsx 解压总大小上限；② 解析后立即对 text 设 MAX_CHARS（超过截断并告警），Excel 用 `nrows=MAX_PARSE_ROWS` 参数让 pandas 流式限行读取；③ 入库流水线改为后台任务队列（Celery/RQ），请求内只落"pending"状态，超限文件返回 4xx 并删除；④ chunk 数设上限（如 5k）并估算 embedding 成本。

---

## P2 轻微

1. 【api/knowledge.py:88-91】解析失败时 `HTTPException(500, detail=f"文档处理失败: {e}")` 把解析器异常原文（含库内部信息/可能路径）透出给客户端；且 doc 行与磁盘文件已落库后失败不清理，反复失败上传会积累 status=failed 的孤儿文件占磁盘（靠用户手动删除）。建议：detail 只返回通用文案+doc_id，错误详情进日志；失败文件入库即删。
2. 【api/knowledge.py:58-67】`file.filename` 原样写入 `Document.filename`(String(255)，models.py:134)，无长度/字符校验；Postgres 下超 255 字符文件名会在 db.commit()（位于 try 之前）抛 IntegrityError → 通用 500，且 save_file 已写入的磁盘文件成孤儿。同理 /leads/import 各字段无长度上限（Lead.company_name 等列有长度约束，models.py:271-275）。建议上传前对文件名做长度截断/清洗（也顺带缓解文件名中 `<`/`>`/引号在 JSON/HTML 渲染时的存储型注入面——本报告未在前端证实 XSS，仅防御性建议）。
3. 【api/knowledge.py:145-170,173-191】删除/清空流程非原子：先删 Qdrant 向量（外部存储、立即生效），再删 DrawingParam/BM25/SQL/文件；任一步骤异常（如 Qdrant 连接抖动、os.remove 权限）都会留下「向量已删但 Document 仍在/参数已删一半」的中间态，且清空端点 delete_company_collection 抛错时前面已提交的 BM25 SQL 删除无法回滚。建议按「DB 事务先行置 deleted 标记 + 后台兜底清理 + 幂等重试」重构，或至少把外部删除包在重试里。
4. 【hybrid.py:309-337 与 347-370】`_flush_unsynced` 补写 BM25 SQL 成功后不推进 `idx.synced_max_id`，紧接的增量同步 `id > synced_max_id` 会把刚补写的行再次 `add` 进内存索引 → 同一 chunk 双份入内存（词频 df 翻倍、打分失真）。仅在落库失败路径触发（低频），但会静默污染稀疏检索统计。建议补写成功后把新行最大 id 一并写回 synced_max_id（补写与增量同步原子化）。
5. 【hybrid.py:399-413】RRF 融合以 `text` 作为唯一去重 key：两份不同文档的完全相同文本块会合并为一条结果，来源(source)可能被稀疏侧覆盖——同一型号出现在两个文档（如两份不同报价图）时检索结果丢文档来源、引用展示错乱。建议去重 key 加 document_id。
6. 【rag.py:22-33 + hermes\memory\memory_system.py:98-101】Qdrant 模式下对「该租户从未建集合」的查询 `query_points` 会抛集合不存在异常，`rag._search` 无空结果兜底；hybrid_search 因包了 try 而安全，但 Hermes 记忆检索 `search_long_term_memory`（直调 rag.search）没有兜底——新租户尚未上传任何文档时，AI 员工调用长期记忆会硬失败而非返回空。建议 _search 统一 catch 404 返回 []。
7. 【embedding.py:64-77】单批 embedding 推理瞬时失败即整批降级为哈希向量入库（维度恰与模型一致时可通过 vector.py:142 的维度校验），与库中既有模型向量混存于同一集合——不同语义空间混检，检索质量静默劣化且不可回溯。建议：降级批次单独标记 payload（如 embedding=hash）并在检索时排除，或写入失败而不是混存。
8. 【upload.py:18-35 + main.py 无请求体中间件】无 Content-Length 预检：超大 multipart 会先由 Starlette 完整 spool（>1MB 落临时盘）才轮到 handler 的 read(max+1) 拒绝——认证租户可并发大上传做磁盘/带宽 DoS。建议入口中间件按 Content-Length 先 413。
9. 【parser.py:28-40】`parse_doc` 的 antiword `subprocess.run` 无 timeout：恶意/畸形 .doc 可使 subprocess 挂起占住工作线程；且整个函数吞异常返回空串，失败不可观测。建议加 timeout（如 10s）+ 失败告警。cad_parser.py:248-268 的 odafc DWG 转换同理（外部转换器无超时）。
10. 【cad_parser.py:192-235】DXF 解析中 INSERT/TABLE/块定义三处 `except Exception: pass` 会静默吞掉真实错误，导致只抽出部分文本却无任何告警（CAD 助手答非所问的来源之一）。建议至少 logger.warning 并汇总"跳过实体数"。
11. 【api\leads.py:171-218】/leads/list、/stats 无分页全量 `.all()` 加载，线索量增长后响应膨胀/内存占用线性增长；知识库 /knowledge/list 同理。建议分页或游标。
12. 【api\leads.py:90-156】/leads/import 单请求内最多 200 条 × (LLM 询盘解析 + LLM 评分) + 可选 200 封真实邮件同步执行，/discover 最多 50 条 × LLM 评分——请求耗时可达分钟级、占线程、无任务化/配额控制（结合 P1-3 属同族问题，量级较小）。建议后台任务化并按套餐加配额。
13. 【api\leads.py:362-386 + channels\email\sequence.py:197-214】track/unsubscribe 为公开端点且仅凭 16 位 hex track_id（该 ID 出现在每一封外发邮件链接中，会经邮件网关/转发/IT 日志扩散）：持有者即可把对应线索置为 unsubscribed 或刷 open_count。影响面有限（单条线索、非跨租户）但属设计缺口，建议退订做二次确认或限频。
14. 【sales\discovery.py:136-149】`seed = abs(hash(keywords))` 依赖 PYTHONHASHSEED，同一关键词在不同进程/worker/重启后的 Mock 结果**不可复现**（与注释宣称"可重复"相悖），且 /discover 默认 MockProvider（真实源是 NotImplementedError 占位）——生产若未注入真实 provider 会给租户生成虚构企业并产生 follow 邮件（收件人 purchase@假域名）。建议：演示 Provider 明确按环境开关（SEED_DEMO 同款机制），或给发现结果打 source="mock" 警示。
15. 【sales\parse_inquiry.py:37-44 + scoring.py:100-114】LLM 输出字段域校验缺失：country 未校验为两位国家代码（任意串进入 compliance 筛查与国家→语言路由）、intent 未限制在 high/medium/low、product/notes 长度未截断后仍直接入库并进入后续评分/邮件模板 prompt；同时询盘原文属**外部不可信文本**，未经隔离直接拼入 LLM prompt（parse_inquiry 与 build_sequence），存在提示注入面——攻击者可借询盘正文影响解析结果/评分/邮件生成（影响限本租户，故列 P2）。评分解析用贪婪 `\{.*\}` + int() 强转整体有 try 兜底不会 500，但 LLM 输出 "87.5" 这类小数会回退规则分。建议：字段域校验白名单化 + 长度截断 + prompt 中把外部文本用定界符包裹并声明不可信。

---

## 架构与可维护性观察

- **上传→入库主链路无任何后台化/幂等/配额设计**：解析、embedding、Qdrant upsert、BM25 SQL、参数抽取全同步串行在请求内，且 Document 状态机（uploaded→parsed→vectorized/failed）在异常分支并不总是与外部存储（Qdrant/磁盘/BM25 SQL）保持一致——外部存储失败时 DB 状态仍可能显示成功。这是本次审查中"故障态不可观测"的根源，建议引入任务表 + 状态机收敛。
- **内容三份冗余存储**：chunk 文本同时存在于 Qdrant payload、bm25_chunks 表、进程内 _bm25_store，且 bm25_chunks 与 Qdrant payload 均随文档增长无上限治理（租户删除企业/注销时若未调 clear_company，Qdrant 集合、uploads 目录、bm25_chunks 均成孤儿——已确认 clear/delete API 存在，但未见到企业注销路径的统一回收入口，建议核实）。跨进程一致性靠每检索一次 SQL 增量拉取（hybrid.py:340-370）换取，检索路径隐含一次 DB 查询成本，量大后需关注。
- **多租户隔离主线是干净的**（值得肯定）：per-tenant Qdrant 集合名由 int 强转 company_id 拼接（vector.py:44-46）、内存降级库按 company_id 分桶、BM25/DrawingParam/Leads 全部 company 过滤 + require_company 双校验；上传文件永不使用原始文件名落盘（uuid 重命名），知识库原文件不对外静态服务。这些是本模块安全地基，后续改动应保持。
- **解析器扩展性**：_PARSERS 注册表 + 统一 parse_file 入口模式良好，但 .doc（antiword 子进程）、.dwg（odafc 外部转换器）两个"尽力而为"分支不可靠且无超时，建议从白名单剔除或明确标记为"预览解析"。
- **LLM 输出容错整体合格**：llm_parse.parse_json_output + safe_int/safe_str + scoring/parse_inquiry 大 try 兜底 + 评分钳制 0-100、grade 用服务端 grade_of 重算而非信任 LLM，设计正确；剩余问题集中在字段域校验缺失（P2-15）。

---

## 结论

本模块代码质量整体处于中上水平：租户隔离模型（物理集合 + 双校验）与 LLM 输出容错做得扎实，未见可确证的越权/泄露/注入类 P0 漏洞。真正的风险集中在"数据通道的故障语义"上：① Qdrant 点 ID 重启复位导致的历史点被覆盖（P1-1）是确定性静默数据损坏，任何"重启+新上传"都会发生，属于**必须尽快修**的正确性缺陷；② Qdrant 内存降级默认开启且永不重连（P1-2）让任何瞬时故障演变成无声的知识库丢失；③ 解析-入库全同步且无内容总量上限（P1-3）是唯一可被普通租户利用、并可能波及其他租户的资源耗尽面。建议按 P1-1 → P1-2 → P1-3 的顺序修复，并补一条"上传后 Document 状态与外部存储一致性"的端到端校验（含故障注入测试），即可把该模块的主要风险收敛到可接受水平。P2 清单中的 3/6/8/12/14 与上述主链路相关，可随重构一并处理。
