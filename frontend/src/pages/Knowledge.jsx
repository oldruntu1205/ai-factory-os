import React, { useEffect, useState } from 'react'
import api from '../api'

export default function Knowledge() {
  const [docs, setDocs] = useState([])
  const [searchQ, setSearchQ] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const companyId = Number(localStorage.getItem('company_id'))

  const load = async () => {
    const { data } = await api.get('/knowledge/list', { params: { company_id: companyId } })
    setDocs(data.documents)
  }

  useEffect(() => { load() }, [])

  const upload = async (e) => {
    const file = e.target.files[0]
    if (!file) return
    setUploading(true)
    setError('')
    try {
      const fd = new FormData()
      fd.append('company_id', companyId)
      fd.append('file', file)
      const { data } = await api.post('/knowledge/upload', fd)
      if (data.status === 'warning') setError(data.message)
      load()
    } catch (err) {
      setError(err.response?.data?.detail || '上传失败')
    } finally {
      setUploading(false)
      e.target.value = ''
    }
  }

  const search = async () => {
    if (!searchQ.trim()) return
    const { data } = await api.post('/knowledge/search', {
      question: searchQ, company_id: companyId,
    })
    setSearchResults(data.results)
  }

  const removeDoc = async (id) => {
    if (!window.confirm('确认删除该文档？')) return
    await api.delete(`/knowledge/${id}`, { params: { company_id: companyId } })
    load()
  }

  return (
    <div>
      <h1 style={{ fontSize: 22, marginBottom: 24 }}>企业知识库（RAG）</h1>

      {error && <div className="alert alert-error">{error}</div>}

      <div style={{ display: 'grid', gridTemplateColumns: '3fr 2fr', gap: 24 }}>
        {/* 左侧：文档管理 */}
        <div>
          <div className="card" style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <span style={{ fontSize: 14, fontWeight: 600 }}>上传企业知识文档</span>
              <label className="btn btn-primary btn-sm" style={{ cursor: 'pointer' }}>
                {uploading ? '处理中...' : '选择文件上传'}
                <input type="file" style={{ display: 'none' }} onChange={upload}
                  accept=".pdf,.docx,.doc,.xlsx,.xls,.txt,.md,.csv" />
              </label>
              <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>支持 PDF / Word / Excel / TXT</span>
            </div>
          </div>

          <div className="card">
            <table className="table">
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>类型</th>
                  <th>知识片段</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td>{d.filename}</td>
                    <td><span className="badge badge-gray">{d.file_type}</span></td>
                    <td>{d.chunk_count}</td>
                    <td>
                      <span className={`badge ${d.status === 'vectorized' ? 'badge-green' : d.status === 'failed' ? 'badge-red' : 'badge-yellow'}`}>
                        {d.status === 'vectorized' ? '已入库' : d.status === 'failed' ? '解析失败' : '处理中'}
                      </span>
                    </td>
                    <td>
                      <button className="btn btn-danger btn-sm" onClick={() => removeDoc(d.id)}>删除</button>
                    </td>
                  </tr>
                ))}
                {docs.length === 0 && (
                  <tr><td colSpan={5} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>暂无文档</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* 右侧：RAG 检索 */}
        <div className="card">
          <h3 style={{ marginBottom: 12 }}>RAG 智能检索</h3>
          <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
            <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && search()}
              placeholder="输入问题，如：你们的烧杯规格是什么？" />
            <button className="btn btn-primary" onClick={search}>检索</button>
          </div>
          {searchResults.map((r, i) => (
            <div key={i} style={{
              background: 'var(--bg-subtle)', borderRadius: 8, padding: 12, marginBottom: 8, fontSize: 13,
            }}>
              <div style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 4 }}>
                📎 {r.source || '企业资料'} · 片段 {i + 1}
              </div>
              <div style={{ lineHeight: 1.6 }}>{r.text}</div>
            </div>
          ))}
          {searchResults.length === 0 && (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 13, padding: 24 }}>
              上传文档后，可在这里测试企业知识检索
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
