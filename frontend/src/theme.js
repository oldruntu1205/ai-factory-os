/**
 * 主题（日间/夜间 + 主色）——**唯一实现**。
 *
 * ## 为什么要有这个文件
 *
 * 原先主题逻辑散在**三处**，各写一遍：
 * 1. `index.html` 的内联防闪烁脚本；
 * 2. `App.jsx` 的 `useApplyStoredTheme()`；
 * 3. `components/ThemeSwitcher.jsx`。
 *
 * 三者**互不知道**，于是行为不一致：内联脚本与 `App` 那份**直接把 localStorage
 * 的值写进 DOM、不做校验**；`ThemeSwitcher` 那份会校验。
 * 结果是同一份 `localStorage` 在不同时机产生不同结果——这正是本项目反复吃亏的
 * "两套真相"，只是这次发生在主题上。
 *
 * 现在三处都只调用本模块：**校验只有一份，键名只有一份，写入点只有一份。**
 *
 * ## 非法值的处理
 *
 * 旧版本曾提供 `cyan` / `pink` / `red` / `gray` 四个主色，但 `index.css` 里
 * **没有对应规则**（点了没反应）。用户 localStorage 里可能残留这些值。
 * 所以读取时**收敛到合法值**，而不是把非法值写进 DOM——
 * 写进去的后果是 `[data-color="cyan"]` 匹配不到任何规则，主色悄悄回退，
 * 且**没有人知道为什么**。
 */

export const MODES = ['light', 'dark']

/** 主色：**每一项都必须在 `index.css` 里有对应规则**，否则点了没反应。 */
export const COLORS = [
  { key: 'blue', label: '蓝', swatch: '#2563eb' },
  { key: 'green', label: '绿', swatch: '#16a34a' },
  { key: 'purple', label: '紫', swatch: '#9333ea' },
  { key: 'orange', label: '橙', swatch: '#ea580c' },
]

const COLOR_KEYS = new Set(COLORS.map((c) => c.key))

export const DEFAULT_MODE = 'light'
export const DEFAULT_COLOR = 'blue'

/** 把一个任意值收敛为合法的模式。 */
export function normalizeMode(value) {
  return MODES.includes(value) ? value : DEFAULT_MODE
}

/** 把一个任意值收敛为合法的主色。 */
export function normalizeColor(value) {
  return COLOR_KEYS.has(value) ? value : DEFAULT_COLOR
}

/** 从 `localStorage` 读（已收敛）。`storage` 可注入，便于测试。 */
export function readStoredTheme(storage = globalThis.localStorage) {
  let mode = DEFAULT_MODE
  let color = DEFAULT_COLOR
  try {
    mode = normalizeMode(storage?.getItem('theme_mode'))
    color = normalizeColor(storage?.getItem('theme_color'))
  } catch {
    // 隐私模式等场景下 localStorage 可能抛错——用默认值，不让整个应用挂掉
  }
  return { mode, color }
}

/**
 * 把主题写到 DOM。
 *
 * 同时设置 `color-scheme`：让浏览器**原生控件**（滚动条、日期选择器、输入框
 * 的自动填充底色）也跟着明暗走。少了它，夜间模式下滚动条仍是一条亮的白条。
 */
export function applyTheme(mode, color, doc = globalThis.document) {
  const html = doc?.documentElement
  if (!html) return
  html.setAttribute('data-theme', mode)
  html.setAttribute('data-color', color)
  html.style.colorScheme = mode === 'dark' ? 'dark' : 'light'
}

/** 读 + 写一步到位。 */
export function initTheme(storage = globalThis.localStorage, doc = globalThis.document) {
  const theme = readStoredTheme(storage)
  applyTheme(theme.mode, theme.color, doc)
  return theme
}

/** 持久化（写失败不抛——主题是外观，不该让页面崩）。 */
export function persistTheme(mode, color, storage = globalThis.localStorage) {
  try {
    storage?.setItem('theme_mode', mode)
    storage?.setItem('theme_color', color)
  } catch {
    /* 忽略：无法持久化不影响本次会话的外观 */
  }
}
