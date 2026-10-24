/**
 * V3.0 用户侧接口客户端。
 *
 * ## 为什么要单独一个模块，而不是复用 `api.js`
 *
 * `api.js` 打的是 **V2.1** 后端（`/api` → 8000）。
 * V3.0 的页面打的是 **V3.0 BFF**（`/v3-user` → 8300）。
 *
 * 两者混用会让"这个请求到底打到哪套后端"变得不可读，所以刻意用不同前缀、不同模块。
 *
 * ## 身份（2026-10-05）
 *
 * BFF **必须**能确定"你是谁"才能回答"你的单子/你的员工"。
 * 所以这里带上 **V2.1 的 JWT**，由 BFF 拿去问 V2.1 的 `/auth/me` 校验，
 * 再映射成 V3.0 身份（company → tenant/factory/employee）。
 *
 * ⚠️ **V3.0 自己的凭证（`Authorization: Bearer <服务身份>`）永远不进浏览器**——
 * 那个是后端之间的身份。浏览器只带自己的登录票据。
 *
 * ## 缓存
 *
 * 同一份数据在演示环境里几乎不变，按 URL 缓存避免切页重复请求。
 * **不缓存失败**：失败要能立刻重试。
 */

const CACHE = new Map()

function authHeaders() {
  const headers = { accept: 'application/json' }
  const token = localStorage.getItem('token')
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

export async function getJson(url, { force = false } = {}) {
  if (!force && CACHE.has(url)) {
    return CACHE.get(url)
  }
  const response = await fetch(url, { headers: authHeaders() })
  const text = await response.text()
  let body
  try {
    body = JSON.parse(text)
  } catch {
    // 非 JSON（例如代理没起来返回 HTML）要**说清楚**，否则界面只会显示"读取失败"
    throw new Error(`接口返回的不是 JSON（HTTP ${response.status}）：${text.slice(0, 120)}`)
  }
  if (!response.ok) {
    const error = new Error(body?.message ?? `HTTP ${response.status}`)
    error.status = response.status
    error.code = body?.code ?? null
    error.body = body
    throw error
  }
  CACHE.set(url, body)
  return body
}

export async function postJson(url, payload) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { ...authHeaders(), 'content-type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(body?.message ?? `HTTP ${response.status}`)
    error.status = response.status
    error.code = body?.code ?? null
    throw error
  }
  return body
}

export function clearCache() {
  CACHE.clear()
}

/** 供页面统一展示"为什么读不到"（登录失效 / 公司未映射 / 后端不可达）。 */
export function describeAuthError(error) {
  if (!error) return null
  if (error.code === 'AUTH_REQUIRED') return '需要登录后才能查看 V3.0 数据。'
  if (error.code === 'AUTH_INVALID') return '登录态已失效，请重新登录。'
  if (error.code === 'TENANT_NOT_MAPPED') return error.message
  if (error.code === 'NO_TENANT_SCOPE') return error.message
  return error.message ?? '读取失败'
}
