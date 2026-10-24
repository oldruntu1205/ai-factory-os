"""
tests/test_hermes_mcp_scheduler_audit.py —— 中台三件套验收测试

覆盖：
  Part 1  MCP Gateway：健康检查 / tools.list 只读过滤 / JSON-RPC 协议 /
                        tools.call 成功 / 写工具默认拒绝 / API Key 鉴权
  Part 2  Cron 调度：表达式解析真值表 / 到点投递与同分钟去重 / 手动触发 / 状态与下次运行
  Part 3  审计回放：参数脱敏 / 指纹稳定性 / 台账查询 / 会话回放 / 统计

运行：python tests/test_hermes_mcp_scheduler_audit.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _test_sqlite import isolate_sqlite  # noqa: E402

# 原先写 `./test_mcp_{pid}.db`：相对仓库根落库，且清理只在 main() 末尾——
# 被 pytest 导入时根本不执行 main()，于是永远不清理（实测累积 45 个 / 20.95MB）。
# 现在只写系统临时目录，并由 atexit 保证任何退出路径都清理。
_TEST_DB = isolate_sqlite("test_mcp")

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ============ Part 2 单元：cron 解析 ============

def test_cron_unit():
    from hermes.scheduler.cron import CronExpression, CronParseError

    print("\n[0] Cron 表达式解析（单元）")
    c = CronExpression("0 8 * * 1-6")
    check("工作日 08:00 命中（周一）", c.matches(datetime(2026, 9, 14, 8, 0)))
    check("工作日 08:00 不命中（周日）", not c.matches(datetime(2026, 9, 13, 8, 0)))
    check("工作日 08:01 不命中", not c.matches(datetime(2026, 9, 14, 8, 1)))

    c2 = CronExpression("*/15 * * * *")
    check("*/15 命中 :30", c2.matches(datetime(2026, 9, 14, 10, 30)))
    check("*/15 不命中 :31", not c2.matches(datetime(2026, 9, 14, 10, 31)))

    c3 = CronExpression("30 9 * * 1-5")
    nxt = c3.next_after(datetime(2026, 9, 12, 10, 0))   # 周六
    check("周末求下次运行 = 周一 09:30",
          nxt == datetime(2026, 9, 14, 9, 30), str(nxt))

    try:
        CronExpression("0 8 * *")
        check("字段数不足应报错", False)
    except CronParseError:
        check("字段数不足应报错", True)

    try:
        CronExpression("99 8 * * *")
        check("分钟越界应报错", False)
    except CronParseError:
        check("分钟越界应报错", True)

    # 周 7 归一为周日
    c4 = CronExpression("0 12 * * 7")
    check("周 7 等价周日", c4.matches(datetime(2026, 9, 13, 12, 0)))


# ============ Part 3 单元：审计脱敏 ============

def test_audit_unit():
    from hermes.audit.service import mask_params, params_hash

    print("\n[0] 审计脱敏与指纹（单元）")
    masked = mask_params({"product_id": "A1", "token": "super-secret-value",
                          "password": "pwd", "apiKey": "k", "note": "x" * 500})
    check("token 被脱敏", masked["token"] == "***")
    check("password 被脱敏", masked["password"] == "***")
    check("apiKey 被脱敏（子串匹配）", masked["apiKey"] == "***")
    check("业务字段保留", masked["product_id"] == "A1")
    check("超长值截断", len(masked["note"]) == 200)

    h1 = params_hash({"a": 1, "b": 2})
    h2 = params_hash({"b": 2, "a": 1})
    check("指纹与键序无关", h1 == h2)
    check("指纹为 sha256 长度", len(h1) == 64)
    check("不同参数指纹不同", params_hash({"a": 1}) != params_hash({"a": 2}))


# ============ Part 2 单元：调度去重 ============

def test_scheduler_unit():
    from hermes.scheduler.service import SchedulerService
    from hermes.scheduler.jobs import JOBS

    print("\n[0] 调度投递与去重（单元）")

    class _Q:
        _mode = "memory"

        def __init__(self):
            self.calls = []

        def enqueue(self, task, payload, priority=5):
            self.calls.append((task, payload))
            return {"task_id": f"t{len(self.calls)}", "status": "queued"}

    q = _Q()
    svc = SchedulerService(queue=q, runtime=None, enabled=True)

    # 用一个必然命中的时刻驱动：把任务 cron 改成每分钟
    svc.jobs = [dict(JOBS[0], cron="* * * * *")]
    from hermes.scheduler.cron import CronExpression
    svc._compiled = {"production.delay_scan": CronExpression("* * * * *")}
    svc._target_companies = lambda: [1, 2]

    t = datetime(2026, 9, 12, 9, 0)
    out1 = svc.tick(t)
    check("到点投递到所有企业", len(out1) == 2, str(out1))
    out2 = svc.tick(t)
    check("同一分钟不重复投递", len(out2) == 0, str(out2))
    out3 = svc.tick(datetime(2026, 9, 12, 9, 1))
    check("下一分钟再次投递", len(out3) == 2)
    check("payload 携带 company_id 与 agent",
          out3[0].get("company_id") == 1 and q.calls[0][1].get("agent") == "production_agent")

    st = svc.status()
    check("status 含任务数与下次运行",
          len(st["jobs"]) >= 1 and bool(st["jobs"][0]["next_run"]), str(st["jobs"])[:200])
    check("dispatch_count 累计", st["dispatch_count"] == 4, str(st["dispatch_count"]))


# ============ HTTP 端到端 ============

TEST_EMAIL = f"mcptest-{os.getpid()}@example.com"
TEST_PASSWORD = "Test123456"


def _bootstrap(client):
    """用真实注册接口建租户 + 管理员（本构建启动时不注入演示账号，测试自行造数）。"""
    r = client.post("/auth/register", json={
        "company": "验收测试企业", "email": TEST_EMAIL,
        "password": TEST_PASSWORD, "name": "验收管理员"})
    if r.status_code != 200:
        # 复用已存在的测试租户（同 pid 重复运行场景）
        r = client.post("/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    j = r.json()
    return {"Authorization": f"Bearer {j['access_token']}"}, j["company_id"]


def test_mcp_http(client, headers, company_id):
    print("\n[1] MCP Gateway（HTTP / JSON-RPC）")

    r = client.get("/mcp/health")
    check("健康检查", r.status_code == 200 and r.json()["server"] == "hermes-mcp", r.text)

    r = client.get("/mcp/tools", headers=headers)
    check("tools.list 鉴权通过", r.status_code == 200, r.text)
    names = [t["name"] for t in r.json()["tools"]]
    check("只读工具在列（erp.get_stock）", "erp.get_stock" in names, str(names))
    check("写工具默认不在列（email.send）", "email.send" not in names, str(names))
    check("inputSchema 已声明",
          any(t["name"] == "erp.get_stock" and t["inputSchema"].get("required") == ["product_id"]
              for t in r.json()["tools"]))

    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {}}, headers=headers)
    check("initialize 返回协议版本",
          r.status_code == 200 and r.json()["result"]["protocolVersion"], r.text)
    check("响应回显 Mcp-Session-Id", bool(r.headers.get("Mcp-Session-Id")))

    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                  "params": {}}, headers=headers)
    check("tools/list 返回 tools 数组", isinstance(r.json()["result"]["tools"], list))

    # tools/call：只读工具走通（dashboard.snapshot 纯 DB，无外部依赖）
    sess = f"mcp-test-{os.getpid()}"
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "dashboard.snapshot", "arguments": {}}},
                    headers={**headers, "mcp-session-id": sess})
    body = r.json()
    check("tools/call 成功且 isError=false",
          r.status_code == 200 and body["result"]["isError"] is False, str(body)[:300])
    check("结果含 structuredContent", "structuredContent" in body["result"])
    check("structuredContent 拿到经营快照",
          "total_amount" in str(body["result"]["structuredContent"]), str(body["result"])[:200])

    # 写工具在只读模式被拒
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "crm.create_customer",
                                             "arguments": {"name": "MCP 测试客户"}}},
                    headers=headers)
    body = r.json()
    check("写工具默认被拒（只读模式）",
          "error" in body and "只读" in body["error"]["message"], str(body)[:200])

    # 未知方法
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "hack.everything",
                                  "params": {}}, headers=headers)
    check("未知方法返回 -32601（而非 500）",
          r.json()["error"]["code"] == -32601, str(r.json())[:200])

    # REST 便捷端点
    r = client.post("/mcp/tools/dashboard.snapshot/call", json={"arguments": {}},
                    headers=headers)
    check("REST 单工具调用", r.status_code == 200 and r.json()["isError"] is False, r.text[:200])

    # 无鉴权
    r = client.get("/mcp/tools")
    check("无令牌被拒", r.status_code == 401, str(r.status_code))

    return sess


def test_api_key_auth(client, company_id):
    print("\n[1b] MCP 企业 API Key 鉴权")
    from database import SessionLocal
    from models import ApiKey
    import uuid

    key = f"mcp-test-{uuid.uuid4().hex}"
    db = SessionLocal()
    try:
        db.add(ApiKey(name="mcp-test", key=key, company_id=company_id, enabled=True))
        db.commit()
    finally:
        db.close()

    r = client.get("/mcp/tools", headers={"Authorization": f"Bearer {key}"})
    check("API Key 可列出工具", r.status_code == 200 and r.json()["count"] >= 1, r.text[:200])

    r = client.post("/mcp/tools/dashboard.snapshot/call", json={"arguments": {}},
                    headers={"Authorization": f"Bearer {key}"})
    check("API Key 可调用工具", r.status_code == 200 and r.json()["isError"] is False)

    # 停用后拒绝
    db = SessionLocal()
    try:
        row = db.query(ApiKey).filter(ApiKey.key == key).first()
        row.enabled = False
        db.commit()
    finally:
        db.close()
    r = client.get("/mcp/tools", headers={"Authorization": f"Bearer {key}"})
    check("停用的 Key 被拒", r.status_code == 403, str(r.status_code))


def test_scheduler_http(client, headers, company_id):
    print("\n[2] 定时调度（HTTP）")

    r = client.get("/hermes/scheduler/status", headers=headers)
    check("调度状态可读", r.status_code == 200, r.text[:200])
    st = r.json()
    check("调度器运行中", st.get("running") is True, str(st.get("running")))
    check("任务数 >= 3", len(st.get("jobs", [])) >= 3)
    check("每个任务有下次运行时间",
          all(j.get("next_run") for j in st["jobs"]), str(st["jobs"])[:300])
    check("cron 全部合法", all(j.get("valid") for j in st["jobs"]))

    r = client.post(f"/hermes/scheduler/jobs/quality.daily_summary/run?company_id={company_id}",
                    headers=headers)
    check("手动触发一次", r.status_code == 200 and r.json().get("dispatched") >= 1, r.text[:200])

    r = client.post("/hermes/scheduler/jobs/not-exist/run", headers=headers)
    check("未知任务 404", r.status_code == 404, str(r.status_code))

    r = client.post("/hermes/scheduler/reload", headers=headers)
    check("重载任务配置", r.status_code == 200 and r.json()["valid"] >= 3, r.text[:200])


def test_audit_http(client, headers, company_id, session_id):
    print("\n[3] 审计与回放（HTTP）")

    r = client.get(f"/hermes/audit/tool-calls?company_id={company_id}", headers=headers)
    check("台账可查询", r.status_code == 200, r.text[:200])
    data = r.json()
    check("已记录刚才的 MCP 调用", data["total"] >= 1, str(data["total"]))

    item = next((x for x in data["items"] if x["tool_name"] == "dashboard.snapshot"), None)
    check("台账含 dashboard.snapshot 记录", item is not None)
    if item:
        check("来源标记为 mcp", item["source"] == "mcp", item["source"])
        check("调用成功", item["ok"] is True)
        check("有耗时数据", item["latency_ms"] >= 0)

    # 参数脱敏（用带 token 的 ERP 工具验证）
    client.post("/mcp/tools/erp.get_stock/call",
                json={"arguments": {"product_id": "A1", "token": "should-be-masked"}},
                headers=headers)
    r = client.get(f"/hermes/audit/tool-calls?company_id={company_id}&tool_name=erp.get_stock",
                   headers=headers)
    erp = r.json()["items"][0]
    check("审计参数中 token 已脱敏", erp["params"].get("token") == "***", str(erp["params"]))
    check("业务参数保留", erp["params"].get("product_id") == "A1")

    r = client.get(f"/hermes/audit/replay/{session_id}?company_id={company_id}", headers=headers)
    check("会话回放可用", r.status_code == 200, r.text[:200])
    rep = r.json()
    check("回放时间线 >= 1 步", rep["steps"] >= 1, str(rep.get("steps")))
    check("回放含累计耗时", rep["total_latency_ms"] >= 0)

    r = client.get(f"/hermes/audit/replay/not-exist-session?company_id={company_id}",
                   headers=headers)
    check("无记录会话回放 404", r.status_code == 404, str(r.status_code))

    r = client.get(f"/hermes/audit/stats?company_id={company_id}&days=1", headers=headers)
    check("统计可用", r.status_code == 200 and "fail_rate" in r.json(), r.text[:200])
    st = r.json()
    check("统计含 Top 工具", len(st["top_tools"]) >= 1, str(st["top_tools"]))

    # 多租户隔离：他企 company_id 查询被拒
    r = client.get("/hermes/audit/tool-calls?company_id=99999", headers=headers)
    check("跨租户查询被拒（403）", r.status_code == 403, str(r.status_code))


def main():
    test_cron_unit()
    test_audit_unit()
    test_scheduler_unit()

    with TestClient(app) as client:
        headers, company_id = _bootstrap(client)
        session_id = test_mcp_http(client, headers, company_id)
        test_api_key_auth(client, company_id)
        test_scheduler_http(client, headers, company_id)
        test_audit_http(client, headers, company_id, session_id)

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 60)
    try:
        if os.path.exists(_TEST_DB):
            os.remove(_TEST_DB)
    except Exception:
        pass
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
