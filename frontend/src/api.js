import axios from 'axios'

// 所有请求统一走 /api 前缀（vite proxy 转发到后端 8000）
const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

// 自动附加 JWT
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 401 时跳回登录
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

// 鉴权文件打开（财务凭证/发票 PDF 经鉴权下载端点；带 JWT fetch → blob → 新窗口）
export async function openAuthedFile(url) {
  try {
    const res = await api.get(url, { responseType: 'blob' })
    const blobUrl = URL.createObjectURL(res.data)
    window.open(blobUrl, '_blank')
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60000)
  } catch (e) {
    // eslint-disable-next-line no-alert
    alert('文件打开失败：可能无权限或文件不存在')
  }
}

export default api
