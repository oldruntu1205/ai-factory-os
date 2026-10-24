import { useCallback, useEffect, useState } from 'react'
import { describeAuthError, getJson } from './v3'

/**
 * V3.0 页面的数据读取小钩子。
 *
 * ## 为什么要抽出来
 *
 * 四个 V3.0 页面（结论 / 员工 / 发布台账 / 对账）各自都要：
 * 读数据、显示"读取中"、把失败**说清楚**、支持重查。
 * 各写一遍的话，最先分叉的一定是**错误处理**——
 * 而这里错误恰好分几种**必须区分**的情况：
 *
 * - `401 AUTH_REQUIRED/AUTH_INVALID`：没登录或登录失效 → 引导去登录；
 * - `403 TENANT_NOT_MAPPED`：你的公司还没映射到 V3.0 租户 → 说明去哪登记；
 * - `403 NO_TENANT_SCOPE`：这个账号不属于任何公司 → 说明该去管理台；
 * - 其它：如实显示 HTTP 状态与后端消息。
 *
 * 全都显示成"读取失败"是**最省事也最没用**的做法：使用者不知道该做什么。
 */
export function useV3Data(url, { skip = false } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(
    async (target = url) => {
      if (!target) return
      setBusy(true)
      setError(null)
      try {
        setData(await getJson(target, { force: true }))
      } catch (e) {
        setError(e)
        setData(null)
      } finally {
        setBusy(false)
      }
    },
    [url],
  )

  useEffect(() => {
    if (skip || !url) return
    let alive = true
    ;(async () => {
      setBusy(true)
      setError(null)
      try {
        const body = await getJson(url)
        if (alive) setData(body)
      } catch (e) {
        if (alive) {
          setError(e)
          setData(null)
        }
      } finally {
        if (alive) setBusy(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [url, skip])

  return {
    data,
    error,
    busy,
    reload: load,
    /** 可直接渲染的错误说明；无错误时返回 null。 */
    errorText: describeAuthError(error),
    /** 需要引导登录（登录态问题）。 */
    needsLogin: error?.status === 401,
  }
}
