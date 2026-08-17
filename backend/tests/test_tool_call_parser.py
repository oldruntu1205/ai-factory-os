import sys, os
os.environ["USE_SQLITE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hermes.registry.prompt_agent import _extract_json_calls

# 1. 纯 JSON
r1 = _extract_json_calls('[{"tool": "crm.get_customer", "params": {"name": "Alpha"}}]')
assert r1 == [{"tool": "crm.get_customer", "params": {"name": "Alpha"}}], r1
print("[OK] 纯 JSON:", r1)

# 2. markdown 包裹 + 前后说明
r2 = _extract_json_calls('我决定调用工具：\n```json\n[{"tool": "erp.get_stock", "params": {}}]\n```')
assert r2 == [{"tool": "erp.get_stock", "params": {}}], r2
print("[OK] markdown 包裹:", r2)

# 3. 无工具调用（直接回答）
r3 = _extract_json_calls('直接回答，不需要工具')
assert r3 == [], r3
print("[OK] 无工具调用返回空")

# 4. 含非法工具（仍解析，由白名单在 ToolCenter 拦截）
r4 = _extract_json_calls('[{"tool": "hack.tool", "params": {}}]')
assert len(r4) == 1 and r4[0]["tool"] == "hack.tool", r4
print("[OK] 非法工具可解析（执行时被拦截）:", r4)

# 5. 两个工具调用
r5 = _extract_json_calls('[{"tool": "a", "params": {}}, {"tool": "b", "params": {"x": 1}}]')
assert len(r5) == 2, r5
print("[OK] 多工具调用:", r5)

print("\n===== 工具调用解析全部通过 =====")
