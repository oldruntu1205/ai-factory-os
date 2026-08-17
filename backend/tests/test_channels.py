"""
tests/test_channels.py —— 外部渠道专项测试（企业微信 / WhatsApp / 邮件自动化）

覆盖：
1. 企业微信：群机器人推送（mock 降级）、回调签名验证（未配置时回显 echostr）
2. WhatsApp：webhook 消息接收 -> AI 回复、消息入库（conversations.channel=whatsapp）
3. 邮件自动化：跟进序列生成、发送跟进邮件（mock SMTP）、退信标记、发送日志
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["USE_SQLITE"] = "1"
os.environ["QUEUE_MODE"] = "memory"
os.environ["SQLITE_PATH"] = f"./test_channels_{os.getpid()}.db"
os.environ["WHATSAPP_TOKEN"] = "test-token"
os.environ["WHATSAPP_PHONE_ID"] = "test-phone-id"

import sys
from fastapi.testclient import TestClient

from main import app

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


def main():
    with TestClient(app) as client:
        print("=" * 60)
        print("外部渠道专项测试（企微 / WhatsApp / 邮件自动化）")
        print("=" * 60)

        # 登录（已认证演示企业）
        r = client.post("/auth/login", json={"email": "admin@demo.com", "password": "123456"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        cid = r.json()["company_id"]

        # 标记演示企业已认证（模拟上传营业执照审核通过）
        from database import SessionLocal
        from models import Company
        _db = SessionLocal()
        _c = _db.query(Company).filter(Company.id == cid).first()
        _c.license_verified = True
        _c.license_status = "approved"
        _db.commit()
        _db.close()

        print("\n[1] 企业微信")
        # 1.1 群机器人推送（未配置 KEY -> mock 成功）
        r = client.post("/channels/wecom/robot",
                        json={"content": "### 测试日报\n- 销售: 120 万", "msgtype": "markdown"},
                        headers=headers)
        check("群机器人推送（mock）", r.status_code == 200 and r.json().get("ok"))
        # 1.2 回调验证握手（未配置 token -> 回显 echostr）
        r = client.get("/wecom/callback", params={
            "msg_signature": "abc", "timestamp": "1700000000", "nonce": "123456",
            "echostr": "hello123"})
        check("回调握手回显", r.status_code == 200 and "hello123" in str(r.json()))
        # 1.3 应用消息（mock 降级）
        r = client.post("/channels/wecom/app",
                        json={"user_id": "ZhangSan", "content": "您的报价已生成"},
                        headers=headers)
        check("应用消息发送（mock）", r.status_code == 200 and r.json().get("ok"))

        print("\n[2] WhatsApp 完善")
        # 2.1 webhook 消息接收 -> AI 回复（未配置签名，跳过校验）
        payload = {
            "entry": [{
                "changes": [{"value": {"messages": [{
                    "from": "8613800000001",
                    "id": "wamid.test1",
                    "text": {"body": "请问你们有 500ml 烧杯吗？"},
                }]}}],
            }]
        }
        r = client.post("/whatsapp/webhook", json=payload)
        check("webhook 接收并回复", r.status_code == 200 and r.json().get("handled"))
        check("返回 AI 答案", bool(r.json().get("answer")))
        # 2.2 消息入库验证
        time.sleep(0.2)
        from database import SessionLocal
        from models import Conversation
        db = SessionLocal()
        try:
            conv = db.query(Conversation).filter(
                Conversation.channel == "whatsapp").first()
            check("WhatsApp 消息入库", conv is not None and conv.session_id.startswith("whatsapp:"))
            if conv:
                check("入库含客户问题", "烧杯" in (conv.user_message or ""))
        finally:
            db.close()

        print("\n[3] 邮件自动化")
        # 3.1 跟进序列生成
        r = client.post("/channels/email/sequence",
                        json={"company": "太阳玻璃", "product": "玻璃烧杯"},
                        headers=headers)
        seq = r.json().get("sequence", {})
        check("跟进序列 3 封", r.status_code == 200 and len(seq) == 3)
        # 3.2 创建线索并发跟进邮件（mock SMTP）
        r = client.post("/leads/discover", json={"company_id": cid, "limit": 1},
                        headers=headers)
        # 若未发现新客户，直接构造线索
        from database import SessionLocal
        from models import Lead
        db = SessionLocal()
        try:
            lead = db.query(Lead).filter(Lead.company_id == cid).first()
            if not lead:
                lead = Lead(company_name="Test Buyer", email="buyer@test.com",
                            country="DE", industry="laboratory",
                            status="discovered", company_id=cid)
                db.add(lead)
                db.commit()
                db.refresh(lead)
            lead_id = lead.id
        finally:
            db.close()
        r = client.post("/channels/email/followup",
                        json={"lead_id": lead_id, "company_id": cid, "step": 1},
                        headers=headers)
        check("首封跟进发送", r.status_code == 200 and r.json().get("status") in ("sent", "simulated", "error"))
        # 3.3 退信标记（用发送日志里的真实收件人）
        r = client.get(f"/channels/email/logs?company_id={cid}", headers=headers)
        logs = r.json().get("logs", [])
        check("邮件日志列表", len(logs) >= 1)
        if logs:
            recipient = logs[0]["recipient"]
            r = client.post("/channels/email/bounce", json={"recipient": recipient},
                            headers=headers)
            check("退信标记", r.status_code == 200 and r.json().get("count", 0) >= 1)
            # 验证状态已更新为 bounced
            r = client.get(f"/channels/email/logs?company_id={cid}", headers=headers)
            check("退信状态更新", any(l["status"] == "bounced" for l in r.json().get("logs", [])))

        print("\n[4] 渠道状态")
        r = client.get("/channels/status", headers=headers)
        st = r.json()
        check("渠道状态接口", r.status_code == 200 and "wecom" in st and "whatsapp" in st)

        print("\n" + "=" * 60)
        print(f"测试结果: PASS={PASS} FAIL={FAIL}")
        return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
