"""端到端验证：安全设置 + 成员管理 + 认证标识 + 计费"""
import urllib.request, json, time

BASE = 'http://127.0.0.1:8000'
def call(method, path, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = f'Bearer {token}'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {'HTTP_ERROR': e.code, 'detail': json.loads(e.read()).get('detail', '')}

token = call('POST', '/auth/login', {'email':'admin@demo.com','password':'123456'})['access_token']

print('=== ① 安全设置：修改密码 ===')
r = call('PUT', '/company/password', {'old_password':'123456','new_password':'newpass666'}, token)
print('  ✅ 修改密码:', r.get('message', r))
# 改回来
call('PUT', '/company/password', {'old_password':'newpass666','new_password':'123456'}, token)
r = call('PUT', '/company/password', {'old_password':'wrong','new_password':'xxxxxx'}, token)
print('  ✅ 错误旧密码被拒绝:', 'HTTP_ERROR' in r and r['HTTP_ERROR'] == 400)

print('\n=== ② 安全设置：操作日志 ===')
r = call('GET', '/company/audit-logs?company_id=1', token=token)
logs = r.get('logs', [])
print(f'  ✅ 日志数: {len(logs)} | 最近: {logs[0]["action"] if logs else "-"} ({logs[0]["detail"] if logs else ""})')
print(f'     前3条: {[l["action"] for l in logs[:3]]}')

print('\n=== ③ 认证标识 ===')
r = call('GET', '/auth/me', token=token)
print(f'  ✅ me 返回 license_verified: {r.get("license_verified")} | plan: {r.get("plan")}')

print('\n=== ④ 成员管理 ===')
r = call('POST', '/members/invite', {'company_id':1,'email':'staff@demo.com','role':'staff'}, token)
print('  ✅ 邀请:', r.get('message'), '| code:', r.get('code','')[:12]+'...')
code = r.get('code')
r = call('GET', '/members/list?company_id=1', token=token)
print(f'  ✅ 成员列表: {len(r["members"])} 人 | roles: {[m["role"] for m in r["members"]]}')
# 接受邀请（新注册）
r = call('POST', '/members/invite/accept', {'code':code,'email':'staff@demo.com','password':'staff123','name':'员工小王'})
print('  ✅ 接受邀请:', r.get('message'), '| company_id:', r.get('company_id'))
# 找成员id
r = call('GET', '/members/list?company_id=1', token=token)
staff = [m for m in r['members'] if m['email']=='staff@demo.com'][0]
r = call('PUT', f"/members/{staff['id']}/role", {'role':'admin','company_id':1}, token)
print('  ✅ 分配角色:', r.get('message'))
r = call('PUT', f"/members/{staff['id']}/status", {'status':'disabled','company_id':1}, token)
print('  ✅ 停用成员:', r.get('message'))
r = call('PUT', f"/members/{staff['id']}/status", {'status':'active','company_id':1}, token)
print('  ✅ 重新启用:', r.get('message'))

print('\n=== ⑤ 计费系统 ===')
r = call('GET', '/billing/plans', token=token)
print(f'  ✅ 套餐: {list(r["plans"].keys())} | Professional ¥{r["plans"]["professional"]["price"]}/月')
r = call('GET', '/billing/usage?company_id=1', token=token)
print(f'  ✅ 用量: 对话 {r["conversations"]["used"]}/{r["conversations"]["limit"]} | Agent {r["agents"]["used"]}/{r["agents"]["limit"]} | 占比 {r["percent"]}%')
r = call('POST', '/billing/upgrade', {'company_id':1,'plan':'professional'}, token)
print('  ✅ 升级:', r.get('message'), '| 账单:', r.get('bill_no'))
r = call('POST', f"/billing/pay?bill_no={r.get('bill_no')}", token=token)
print('  ✅ 支付:', r.get('message'))
r = call('GET', '/billing/invoices?company_id=1', token=token)
print(f'  ✅ 账单历史: {len(r["invoices"])} 条 | 状态: {[b["status"] for b in r["invoices"]]}')

print('\n=== ALL OK ===')