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

export default api
