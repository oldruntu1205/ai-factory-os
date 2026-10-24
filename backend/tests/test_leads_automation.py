"""
tests/test_leads_automation.py —— 「全自动获客」状态/开关/运行审计 验收

覆盖 2026-10-06 新增的可见层（页面标题自称"全自动获客"，但自动调度是否真在跑
此前完全不可见，只能靠猜）：

  GET  /leads/automation/status     调度配置 / 邮件通道 / 企业开关 / 上次自动运行 / 近 7 天结果
  PUT  /leads/automation/settings   两个开关（落 channel_configs，不建新表）
  POST /leads/run-sequence          手动触发 —— 审计必须与自动分开

**本文件要守住的核心不变量**：
  1. `last_auto_run` 只能由 Celery 自动任务写入；人手点的动作写 `last_manual_run`。
     二者绝不可混——否则页面会把"我点的"说成"自动跑的"，正是本次要修的毛病。
  2. 开关**未设置时按启用处理**（沿用旧行为）；显式关闭后自动任务必须跳过本企业。
  3. 审计写入失败不得影响发现/发信本身（`_record_automation_run` 内部吞异常）。

运行：python tests/test_leads_automation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _test_sqlite import isolate_sqlite  # noqa: E402

# 测试库只写系统临时目录，退出即清理（与 tests/test_api.py 同规）
_TEST_DB = isolate_sqlite("test_leads_automation")

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def _register(client, email, company_name):
    """注册 + 审核通过（线索接口要求执照已审核），返回 (company_id, headers)"""
    r = client.post("/auth/register", json={
        "company": company_name, "email": email, "password": "Test1234"})
    assert r.status_code == 200, r.text
    cid = r.json()["company_id"]
    r = client.post("/auth/login", json={"email": email, "password": "Test1234"})
    token = r.json()["access_token"]

    from database import SessionLocal
    from models import Company
    db = SessionLocal()
    try:
        c = db.query(Company).filter(Company.id == cid).first()
        c.license_status = "approved"
        c.license_verified = True
        db.commit()
    finally:
        db.close()
    return cid, {"Authorization": f"Bearer {token}"}


def _status(client, cid, headers):
    r = client.get("/leads/automation/status",
                   params={"company_id": cid}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def main():
    global PASS, FAIL
    print("=" * 60)
    print("「全自动获客」状态 / 开关 / 运行审计 验收")
    print("=" * 60)

    with TestClient(app) as client:
        cid_a, hdr_a = _register(client, "auto_a@test.com", "自动化测试A")
        cid_b, hdr_b = _register(client, "auto_b@test.com", "自动化测试B")

        # ---------- 1. 状态接口：只报可核实的值 ----------
        print("\n[1] GET /leads/automation/status")
        st = _status(client, cid_a, hdr_a)
        check("返回调度配置列表", isinstance(st.get("schedule"), list) and len(st["schedule"]) == 2,
              str(st.get("schedule")))
        labels = {s["label"] for s in st["schedule"]}
        check("两条自动任务都被识别", labels == {"每天自动发现客户", "每天自动推进邮件跟进"},
              str(labels))
        check("周期说成人话", all(s["every_label"] == "每 1 天" for s in st["schedule"]),
              str([s["every_label"] for s in st["schedule"]]))
        check("开关默认启用（沿用旧行为，不是静默停掉）",
              st["settings"] == {"auto_discovery": True, "auto_followup": True},
              str(st["settings"]))
        check("未跑过时 last_auto_run 为空（不编造）", st["last_auto_run"] is None,
              str(st["last_auto_run"]))
        check("近 7 天邮件结果初始为 0", st["email_last_7d"]["total"] == 0,
              str(st["email_last_7d"]))

        from config import SMTP_USER
        check("邮件通道状态与 SMTP 配置一致",
              st["email_channel"]["usable"] == bool(SMTP_USER),
              f"usable={st['email_channel']['usable']} SMTP_USER_set={bool(SMTP_USER)}")
        check("带诚实说明（数据源 + 调度前提）",
              "演示" in st.get("source_note", "") and "Celery beat" in st.get("scheduler_note", ""),
              str(st.get("source_note"))[:60])

        # ---------- 2. 权限 ----------
        print("\n[2] 权限与多租户")
        r = client.get("/leads/automation/status", params={"company_id": cid_a})
        check("无 token 401", r.status_code == 401, str(r.status_code))
        r = client.get("/leads/automation/status", params={"company_id": cid_b}, headers=hdr_a)
        check("跨企业读状态 403", r.status_code == 403, str(r.status_code))
        r = client.put("/leads/automation/settings", headers=hdr_a, json={
            "company_id": cid_b, "auto_discovery": False, "auto_followup": False})
        check("跨企业写开关 403", r.status_code == 403, str(r.status_code))

        # ---------- 3. 开关读写 ----------
        print("\n[3] PUT /leads/automation/settings")
        r = client.put("/leads/automation/settings", headers=hdr_a, json={
            "company_id": cid_a, "auto_discovery": False, "auto_followup": True})
        check("保存成功", r.status_code == 200 and r.json().get("saved") is True, r.text)
        st = _status(client, cid_a, hdr_a)
        check("关闭状态被读回", st["settings"] == {"auto_discovery": False, "auto_followup": True},
              str(st["settings"]))
        from channels.config_utils import get_automation_flags
        check("底层读取口径一致",
              get_automation_flags(cid_a) == {"auto_discovery": False, "auto_followup": True},
              str(get_automation_flags(cid_a)))
        check("只关自己企业（B 不受影响）",
              _status(client, cid_b, hdr_b)["settings"] == {"auto_discovery": True, "auto_followup": True})

        # ---------- 4. 手动触发：写 manual，不碰 auto ----------
        print("\n[4] 手动触发只写 manual 审计")
        r = client.post("/leads/run-sequence", headers=hdr_a, json={"company_id": cid_a})
        check("手动触发成功", r.status_code == 200, r.text)
        st = _status(client, cid_a, hdr_a)
        check("last_manual_run 有值", st["last_manual_run"] is not None, str(st["last_manual_run"]))
        check("manual 的 action 前缀正确",
              (st["last_manual_run"] or {}).get("action") == "automation.manual_followup",
              str(st["last_manual_run"]))
        check("★ 手动触发不改写 last_auto_run（不能说成自动跑的）",
              st["last_auto_run"] is None, str(st["last_auto_run"]))

        # ---------- 5. 自动任务：写 auto，且带 mock 标记 ----------
        print("\n[5] Celery 自动任务写 auto 审计")
        import sales.discovery as _disc
        import channels.email.sequence as _seq
        _orig_discover = _disc.discover_leads
        _orig_due = _seq.run_due_follow_ups

        # 替换真实发现/发信，令本用例不依赖 LLM 与 SMTP
        _disc.discover_leads = lambda company_id, limit=20, **kw: [{"id": 1}, {"id": 2}]
        try:
            from tasks.followup import scheduled_discovery
            # 此时 auto_discovery 已被关闭 → 必须跳过
            before = _status(client, cid_a, hdr_a)["last_auto_run"]
            out = scheduled_discovery(company_id=cid_a, limit=2)
            check("开关关闭时任务跳过本企业", cid_a in (out.get("skipped") or []), str(out))
            after = _status(client, cid_a, hdr_a)["last_auto_run"]
            check("跳过后不写自动审计", before == after, f"{before} -> {after}")

            # 打开开关 → 必须执行并写审计
            client.put("/leads/automation/settings", headers=hdr_a, json={
                "company_id": cid_a, "auto_discovery": True, "auto_followup": True})
            out = scheduled_discovery(company_id=cid_a, limit=2)
            check("开关打开后任务执行", cid_a not in (out.get("skipped") or []), str(out))
            st = _status(client, cid_a, hdr_a)
            check("★ last_auto_run 写入且 action 为 auto_discovery",
                  (st["last_auto_run"] or {}).get("action") == "automation.auto_discovery",
                  str(st["last_auto_run"]))
            check("审计如实标注是演示数据源",
                  (st["last_auto_run"] or {}).get("detail", {}).get("mock") is True,
                  str(st["last_auto_run"]))
            check("自动运行不覆盖手动记录", st["last_manual_run"] is not None)
        finally:
            _disc.discover_leads = _orig_discover

        # ---------- 6. 自动跟进任务：跑过但 0 封也要留痕 ----------
        print("\n[6] 自动跟进：0 封也要留痕")
        _seq.run_due_follow_ups = lambda company_id=None: {
            "sent": [], "count": 0,
            "summary": {"sent": 0, "simulated": 0, "error": 0},
            "delivery": "none", "by_company": {}, "scanned_companies": [cid_a],
        }
        try:
            from tasks.followup import email_follow_up_sequence
            email_follow_up_sequence()
            st = _status(client, cid_a, hdr_a)
            check("★ 0 封也写 last_auto_run（避免看起来像没跑）",
                  (st["last_auto_run"] or {}).get("action") == "automation.auto_followup",
                  str(st["last_auto_run"]))
            check("0 封的 detail 计数为 0",
                  (st["last_auto_run"] or {}).get("detail", {}).get("count") == 0,
                  str(st["last_auto_run"]))
        finally:
            _seq.run_due_follow_ups = _orig_due

        # ---------- 7. 近 7 天「真发 / 只生成」区分 ----------
        print("\n[7] 近 7 天结果区分真发与只生成")
        from database import SessionLocal
        from models import EmailLog
        db = SessionLocal()
        try:
            db.add(EmailLog(company_id=cid_a, recipient="x@y.de", subject="s1", status="sent"))
            db.add(EmailLog(company_id=cid_a, recipient="z@y.de", subject="s2", status="simulated"))
            db.add(EmailLog(company_id=cid_b, recipient="b@y.de", subject="s3", status="sent"))
            db.commit()
        finally:
            db.close()
        st = _status(client, cid_a, hdr_a)
        check("真发计数", st["email_last_7d"]["sent"] == 1, str(st["email_last_7d"]))
        check("只生成计数", st["email_last_7d"]["simulated"] == 1, str(st["email_last_7d"]))
        check("★ 不串企业", st["email_last_7d"]["total"] == 2, str(st["email_last_7d"]))
        check("B 企业各自独立",
              _status(client, cid_b, hdr_b)["email_last_7d"]["total"] == 1)

        # ---------- 8. 企业隔离：B 看不到 A 的运行记录 ----------
        print("\n[8] 运行记录按企业隔离")
        stb = _status(client, cid_b, hdr_b)
        check("B 的 last_auto_run 仍为空", stb["last_auto_run"] is None, str(stb["last_auto_run"]))
        check("B 的 last_manual_run 仍为空", stb["last_manual_run"] is None, str(stb["last_manual_run"]))

    print("\n" + "=" * 60)
    print(f"测试结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
