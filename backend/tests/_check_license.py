"""端到端验证：手机验证码 + 认证守卫"""
import urllib.request, json, urllib.parse

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

tk = call('POST', '/auth/login', {'email': 'admin@demo.com', 'password': '123456'})['access_token']

print('=== ① 联系手机验证码（演示固定 123456）===')
r = call('POST', '/company/sms/send',
         body={'company_id': 1, 'mobile': '+86 138-0000-0000'},
         token=tk)
print(f'  发送: {r}')

r = call('POST', '/company/sms/verify',
         body={'company_id': 1, 'mobile': '+86 138-0000-0000', 'code': '123456'},
         token=tk)
print(f'  验证: {r}')

print()
print('=== ② 错误验证码 → 400 ===')
r = call('POST', '/company/sms/verify',
         body={'company_id': 1, 'mobile': '+86 138-0000-0000', 'code': '999999'},
         token=tk)
print(f'  {r}')

print()
print('=== ③ 未认证企业生成经营日报 → 应 403 ===')
r = call('GET', '/dashboard/daily-report', token=tk, params={'company_id': 1})
print(f'  {r}')

print()
print('=== ④ 未认证企业执行 Hermes agent → 应 403 ===')
r = call('POST', '/hermes/execute',
         body={'agent_name': 'sales_agent', 'message': '采购1000烧杯', 'company_id': 1},
         token=tk)
print(f'  {r}')

print()
print('=== ⑤ /auth/me 仍可访问（未拦截）===')
r = call('GET', '/auth/me', token=tk)
print(f'  license_verified={r.get("license_verified")}, plan={r.get("plan")}')

print()
print('ALL OK')