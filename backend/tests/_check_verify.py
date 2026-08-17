"""端到端验证：手机验证码 + 认证守卫（含未认证企业场景）"""
import urllib.request, json, urllib.parse, time

BASE = 'http://127.0.0.1:8000'

def call(method, path, body=None, token=None, params=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    url = BASE + path + (('?' + urllib.parse.urlencode(params)) if params else '')
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            t = r.read().decode()
            return json.loads(t) if t.strip() else {}
    except urllib.error.HTTPError as e:
        try:
            return {'HTTP_ERROR': e.code, 'detail': json.loads(e.read()).get('detail', '')}
        except Exception:
            return {'HTTP_ERROR': e.code}

print('=== ① 验证码发送（演示 123456）===')
ts = int(time.time())
r = call('POST', '/auth/register',
         body={'company': f'验证码测试_{ts}', 'email': f'sms_{ts}@x.com', 'password': '123456'})
new_token = r['access_token']
new_cid = r['company_id']

r = call('POST', '/company/sms/send',
         body={'company_id': new_cid, 'mobile': '+86 138-0000-0000'},
         token=new_token)
print(f'  发送: {r}')

r = call('POST', '/company/sms/verify',
         body={'company_id': new_cid, 'mobile': '+86 138-0000-0000', 'code': '123456'},
         token=new_token)
print(f'  验证: {r}')

print()
print('=== ② 未认证企业生成经营日报 → 应 403 ===')
r = call('GET', '/dashboard/daily-report', token=new_token, params={'company_id': new_cid})
print(f'  {r}')

print()
print('=== ③ 未认证企业 /hermes/execute → 应 403 ===')
r = call('POST', '/hermes/execute',
         body={'agent_name': 'sales_agent', 'message': '采购烧杯', 'company_id': new_cid},
         token=new_token)
print(f'  {r}')

print()
print('=== ④ 未认证企业 /agent/chat → 应 403 ===')
r = call('POST', '/agent/chat',
         body={'message': '你好', 'company_id': new_cid},
         token=new_token)
print(f'  {r}')

print()
print('=== ⑤ 未认证企业 /quotation/create → 应 403 ===')
r = call('POST', '/quotation/create',
         body={'requirement': '采购烧杯', 'company_id': new_cid},
         token=new_token)
print(f'  {r}')

print()
print('=== ⑥ 未认证企业 /leads/discover → 应 403 ===')
r = call('POST', '/leads/discover',
         body={'company_id': new_cid},
         token=new_token)
print(f'  {r}')

print()
print('=== ⑦ 未认证企业 /dashboard/sales → 应 403 ===')
r = call('GET', '/dashboard/sales', token=new_token, params={'company_id': new_cid})
print(f'  {r}')

print()
print('=== ⑧ 未认证企业 /dashboard/overview 仍可读（驾驶舱可见）===')
r = call('GET', '/dashboard/overview', token=new_token, params={'company_id': new_cid})
print(f'  销售额: {r.get("revenue")} | 对话数: {r.get("conversations",{}).get("used")}')

print()
print('=== ⑨ 已认证企业 admin@demo.com 全部可访问（回归）===')
admin_tk = call('POST', '/auth/login', {'email': 'admin@demo.com', 'password': '123456'})['access_token']
r = call('GET', '/dashboard/daily-report', token=admin_tk, params={'company_id': 1})
print(f'  admin 经营日报: {r.get("report", "")[:60]}...')

print()
print('ALL OK')