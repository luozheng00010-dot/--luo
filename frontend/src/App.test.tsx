import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import App, { apiFetch } from './App'

// P1-01：首页渲染与健康检查展示
describe('App', () => {
  it('渲染标题与健康检查卡片', async () => {
    global.fetch = new Proxy(global.fetch, {
      apply(_target, _this, args) {
        const url = String(args[0])
        if (url.includes('/healthz')) {
          return Promise.resolve(
            new Response(JSON.stringify({ status: 'ok', version: '0.1.0' }), { status: 200 }),
          )
        }
        return Promise.reject(new Error(`unexpected fetch ${url}`))
      },
    })

    render(<App />)
    expect(screen.getByText('自动剪辑软件')).toBeTruthy()
    await waitFor(() => {
      expect(screen.getByText(/健康检查 正常/)).toBeTruthy()
    })
  })
})

describe('apiFetch', () => {
  it('附会会话令牌并在错误时抛出 detail', async () => {
    localStorage.setItem('autoeditor.session_token', 'tok-1')
    const calls: RequestInit[] = []
    global.fetch = ((url: string, init?: RequestInit) => {
      calls.push(init ?? {})
      if (url.includes('/boom')) {
        return Promise.resolve(
          new Response(JSON.stringify({ error: 'x', detail: '出错了' }), { status: 400 }),
        )
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    }) as unknown as typeof fetch

    await expect(apiFetch('/api/boom')).rejects.toThrow('出错了')
    expect((calls[0].headers as Headers).get('X-Session-Token')).toBe('tok-1')
  })
})
