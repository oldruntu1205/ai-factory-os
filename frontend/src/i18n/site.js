// 兼容旧缓存：早期 HMR 缓存的 Site.jsx 仍请求 /src/i18n/site.js
// 本文件作为 site.jsx 的 re-export，避免旧缓存出现 404 白屏
export { SiteLanguageProvider, useSiteLang, SITE_LANGS } from './site.jsx'
