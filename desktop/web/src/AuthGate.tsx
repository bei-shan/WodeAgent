import { useEffect, useState, type FormEvent, type ReactNode } from 'react'

type AuthStatus = { required: boolean; authenticated: boolean }
type AuthMode = 'checking' | 'authorized' | 'login' | 'error'

export default function AuthGate({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<AuthMode>('checking')
  const [required, setRequired] = useState(false)
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const checkStatus = async () => {
    setMode('checking')
    setError('')
    try {
      const response = await fetch('/api/auth/status', { credentials: 'same-origin' })
      if (!response.ok) throw new Error('服务不可用')
      const status = await response.json() as AuthStatus
      setRequired(Boolean(status.required))
      setMode(status.required && !status.authenticated ? 'login' : 'authorized')
    } catch {
      setError('无法连接到本地 Agent 服务，请确认后端已启动。')
      setMode('error')
    }
  }

  useEffect(() => {
    void checkStatus()
    const reset = () => { setToken(''); setError('会话已失效，请重新登录。'); setMode('login') }
    window.addEventListener('wodeagent-auth-expired', reset)
    return () => window.removeEventListener('wodeagent-auth-expired', reset)
  }, [])

  const login = async (event: FormEvent) => {
    event.preventDefault()
    if (!token.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      const result = await fetch('/api/auth/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      })
      if (!result.ok) {
        setError(result.status === 429 ? '尝试次数过多，请稍后重试。' : '访问令牌不正确，或请求来源不受信任。')
        return
      }
      setToken('')
      setMode('authorized')
    } catch {
      setError('登录失败，无法连接后端服务。')
    } finally {
      setBusy(false)
    }
  }

  const logout = async () => {
    try {
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
    } finally {
      setToken('')
      setMode('login')
    }
  }

  if (mode === 'checking') {
    return <div className="min-h-screen flex items-center justify-center text-gray-500 bg-gray-50">正在检查本地会话…</div>
  }

  if (mode === 'error') {
    return <div className="min-h-screen flex flex-col gap-4 items-center justify-center bg-gray-50 text-gray-700">
      <p role="alert">{error}</p>
      <button type="button" className="rounded-xl bg-blue-600 text-white px-5 py-2" onClick={() => void checkStatus()}>重新连接</button>
    </div>
  }

  if (mode === 'login') {
    return <main className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <form onSubmit={login} className="w-full max-w-sm bg-white rounded-2xl shadow-md border border-gray-100 p-7 space-y-5">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">解锁 WodeAgent</h1>
          <p className="text-sm text-gray-500 mt-2">请输入服务端配置的 WEB_AUTH_TOKEN。令牌只用于本次登录，不会保存在浏览器存储中。</p>
        </div>
        <label className="block text-sm text-gray-700">
          本地访问令牌
          <input type="password" autoComplete="current-password" aria-label="本地访问令牌"
            value={token} onChange={e => setToken(e.target.value)}
            className="mt-2 w-full border border-gray-200 rounded-xl px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-200"
            required />
        </label>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <button type="submit" disabled={busy || !token}
          className="w-full rounded-xl bg-blue-600 text-white py-2.5 disabled:opacity-40">
          {busy ? '验证中…' : '登录'}
        </button>
      </form>
    </main>
  }

  return <>
    {children}
    {required && <button type="button" onClick={() => void logout()}
      className="fixed bottom-4 right-4 z-40 rounded-full bg-white border border-gray-200 shadow px-3 py-1.5 text-xs text-gray-600 hover:text-red-600">
      退出登录
    </button>}
  </>
}
