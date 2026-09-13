import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

const queryClient = new QueryClient()

// 会话令牌由启动器写入 localStorage；本地恶意网页无法读取（P1-05/T24）
export const SESSION_TOKEN_KEY = 'autoeditor.session_token'

export function getSessionToken(): string {
  return localStorage.getItem(SESSION_TOKEN_KEY) ?? ''
}

export async function apiFetch(path: string, init?: RequestInit): Promise<unknown> {
  const headers = new Headers(init?.headers)
  headers.set('X-Session-Token', getSessionToken())
  if (init?.body) headers.set('Content-Type', 'application/json')
  const resp = await fetch(path, { ...init, headers })
  if (!resp.ok) {
    const body = (await resp.json().catch(() => ({}))) as { detail?: string; error?: string }
    throw new Error(body.detail ?? body.error ?? `HTTP ${resp.status}`)
  }
  return resp.json()
}

function HealthCard() {
  const [status, setStatus] = useState<string>('检查中…')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    apiFetch('/healthz')
      .then((data) => {
        const d = data as { status?: string; version?: string }
        setStatus(`正常（v${d.version ?? '?'}）`)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg font-semibold text-zinc-900">本地服务状态</h2>
      {error ? (
        <p className="mt-2 text-sm text-red-600">连接失败：{error}</p>
      ) : (
        <p className="mt-2 text-sm text-emerald-700">健康检查 {status}</p>
      )}
      <p className="mt-4 text-xs text-zinc-500">
        服务仅监听 127.0.0.1；素材、数据库与成片均保存在本机。
      </p>
    </section>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <main className="mx-auto max-w-3xl space-y-6 p-8">
        <header>
          <h1 className="text-2xl font-bold text-zinc-900">自动剪辑软件</h1>
          <p className="mt-1 text-sm text-zinc-500">素材库 · AI 匹配 · 自动合成（开发中：P1）</p>
        </header>
        <HealthCard />
      </main>
    </QueryClientProvider>
  )
}
