import { test, expect, type Page } from '@playwright/test'

type MockState = { modelChanges: string[]; interrupts: number; messages: string[]; uploads: number }

async function mockBackend(page: Page): Promise<MockState> {
  const state: MockState = { modelChanges: [], interrupts: 0, messages: [], uploads: 0 }
  // Simulate a transport connection independent of the real LLM provider.
  await page.addInitScript(() => {
    const target = window as any
    target.__mockSockets = []
    class MockSocket {
      onopen: ((event?: unknown) => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onclose: ((event?: unknown) => void) | null = null
      onerror: ((event?: unknown) => void) | null = null
      readyState = 0
      constructor(public url: string) {
        target.__mockSockets.push(this)
        setTimeout(() => {
          if (this.readyState !== 0) return
          this.readyState = 1
          this.onopen?.()
        }, 0)
      }
      close() {
        if (this.readyState === 3) return
        this.readyState = 3
        this.onclose?.()
      }
      send() { /* server-to-client protocol */ }
    }
    target.WebSocket = MockSocket as unknown as typeof WebSocket
    target.__emitAgent = (message: unknown) => {
      const active = [...target.__mockSockets].reverse().find((s: MockSocket) => s.readyState === 1)
      if (!active) throw new Error('No connected mock socket')
      active.onmessage?.({ data: JSON.stringify(message) })
    }
    target.__dropSocket = () => {
      const active = [...target.__mockSockets].reverse().find((s: MockSocket) => s.readyState === 1)
      active?.close()
    }
  })

  await page.route('**/api/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const ok = (json: unknown, status = 200) => route.fulfill({
      status, contentType: 'application/json', body: JSON.stringify(json),
    })

    if (method === 'GET' && path === '/api/sessions') return ok([])
    if (method === 'POST' && path === '/api/sessions') return ok({
      id: 'e2e-session', title: '新会话', busy: false, pinned: false,
    }, 201)
    if (path === '/api/models' && method === 'GET') return ok([
      { name: 'primary', model: 'test-model', provider: 'openai', base_url: '' },
      { name: 'alternate', model: 'alternate-model', provider: 'openai', base_url: '' },
    ])
    if (method === 'GET' && (path === '/api/skills' || path === '/api/mcp/servers')) return ok([])
    if (path.endsWith('/activate') && method === 'POST') return ok({ status: 'active' })
    if (path.endsWith('/history') && method === 'GET') return ok({ messages: [] })
    if (path.endsWith('/config') && method === 'GET') return ok({
      model: 'test-model', provider: 'openai',
      enable_agent_teams: false, plan_mode: false, thinking_level: 'off',
    })
    if (path.endsWith('/config') && method === 'PUT') {
      const payload = request.postDataJSON()
      if (payload.model_profile) state.modelChanges.push(payload.model_profile)
      return ok({ status: 'updated' })
    }
    if (path.endsWith('/teams') && method === 'GET') return ok([])
    if (path === '/api/sessions/e2e-session' && method === 'GET') return ok({
      id: 'e2e-session', title: '新会话', busy: false, pinned: false,
    })
    if (path.endsWith('/messages') && method === 'POST') {
      state.messages.push(request.postDataJSON().content)
      return ok({ status: 'accepted' })
    }
    if (path.endsWith('/interrupt') && method === 'POST') {
      state.interrupts++
      return ok({ status: 'interrupted' })
    }
    if (path.endsWith('/upload') && method === 'POST') {
      state.uploads++
      return ok({ name: 'uploaded.txt', path: 'uploaded.txt', size: 10 })
    }
    if (path.endsWith('/files') && method === 'GET') {
      return ok({ path: url.searchParams.get('path') || '.',
        entries: [
          { name: 'hello.py', path: 'hello.py', type: 'file' },
          { name: 'notes', path: 'notes', type: 'directory' },
        ], truncated: false })
    }
    if (path.endsWith('/files/content') && method === 'GET') {
      return ok({ path: 'hello.py', content: '1\\tprint("hello world")', truncated: false })
    }
    if (path === '/api/hooks' && method === 'GET') return ok({ hooks: {} })
    return ok({ status: 'ok' })
  })
  return state
}

async function emit(page: Page, type: string, payload: Record<string, unknown>, step = 1) {
  await page.evaluate(event => (window as any).__emitAgent(event), { type, payload, step })
}

test('first message, live tokens, tool log and interruption', async ({ page }) => {
  const state = await mockBackend(page)
  await page.goto('/')
  await expect(page.getByText('Hi, 我是 MyCodeAgent')).toBeVisible()
  await page.getByPlaceholder('问任何问题').fill('请检查当前文件')
  await page.getByPlaceholder('问任何问题').press('Enter')
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()

  await emit(page, 'llm.delta', { kind: 'content', text: '正在检查' })
  await expect(page.getByText('正在检查')).toBeVisible()
  await emit(page, 'tool.started', {
    tool_call_id: 'call-1', tool: 'Read', input: { path: 'hello.py' },
  })
  await expect(page.getByText('Read', { exact: true })).toBeVisible()
  await emit(page, 'tool.completed', {
    tool_call_id: 'call-1', tool: 'Read', input: { path: 'hello.py' },
    status: 'success', output: '{"status":"success","data":{"content":"hello"}}',
  })
  await page.getByRole('button', { name: '停止生成' }).click()
  await expect.poll(() => state.interrupts).toBe(1)
  await emit(page, 'turn.completed', { response: '已停止生成（用户中断）' })
  await expect(page.getByText('已停止生成（用户中断）')).toBeVisible()
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0)
  expect(state.messages).toEqual(['请检查当前文件'])
})

test('model picker changes backend config and file browser previews content', async ({ page }) => {
  const state = await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await page.getByRole('button', { name: /test-model/ }).click()
  await page.getByRole('button', { name: 'alternate-model' }).click()
  await expect.poll(() => state.modelChanges).toEqual(['alternate'])
  await expect(page.getByRole('button', { name: /alternate-model/ })).toBeVisible()

  await page.getByRole('button', { name: '文件', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作区文件' })).toBeVisible()
  await page.getByRole('button', { name: /hello.py/ }).click()
  await expect(page.getByText('print("hello world")', { exact: false })).toBeVisible()
  await expect(page.getByRole('link', { name: '下载', exact: true })).toHaveAttribute('href', /files\/download/)
})

test('a lost socket reconnects without discarding the session', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await page.evaluate(() => (window as any).__dropSocket())
  await expect(page.getByText('连接中 / 自动重连…')).toBeVisible()
  await expect(page.getByText('实时连接正常')).toBeVisible({ timeout: 10000 })
})

test('new session reconnects with cursor and ignores duplicate frames', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect(page.getByText('实时连接正常')).toBeVisible()
  const original = await page.evaluate(() => (window as any).__mockSockets.at(-1).url as string)
  expect(original).toContain('after=0')

  await page.evaluate(() => (window as any).__emitAgent({
    type: 'llm.delta', seq: 1, step: 1, payload: { kind: 'content', text: '甲' },
  }))
  await expect(page.getByText('甲', { exact: true })).toBeVisible()
  await page.evaluate(() => (window as any).__dropSocket())
  await expect(page.getByText('实时连接正常')).toBeVisible({ timeout: 10000 })
  const reconnected = await page.evaluate(() => (window as any).__mockSockets.at(-1).url as string)
  expect(reconnected).toContain('after=1')

  await page.evaluate(() => {
    (window as any).__emitAgent({
      type: 'llm.delta', seq: 1, step: 1, payload: { kind: 'content', text: '重复' },
    })
    ;(window as any).__emitAgent({
      type: 'llm.delta', seq: 2, step: 1, payload: { kind: 'content', text: '乙' },
    })
  })
  await expect(page.getByText('甲乙', { exact: true })).toBeVisible()
})

test('journal reset rebases cursor before the next reconnect', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await page.evaluate(() => {
    (window as any).__emitAgent({
      type: 'step.started', seq: 9, step: 1, payload: { step: 1 },
    })
    ;(window as any).__emitAgent({
      type: 'stream.reset', step: 0,
      payload: { reason: 'cursor_ahead', oldest_seq: 1, latest_seq: 0, resume_from: 0 },
    })
    ;(window as any).__dropSocket()
  })
  await expect(page.getByText('实时连接正常')).toBeVisible({ timeout: 10000 })
  const current = await page.evaluate(() => (window as any).__mockSockets.at(-1).url as string)
  expect(current).toContain('after=0')
})

test('failed turn is reported as failure, not a successful answer', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByPlaceholder('问任何问题').fill('运行一个失败的请求')
  await page.getByPlaceholder('问任何问题').press('Enter')
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await emit(page, 'llm.delta', { kind: 'content', text: '部分输出' })
  await expect(page.getByText('部分输出')).toBeVisible()
  await emit(page, 'run.finished', { status: 'failed', final: '' })
  // The inner agent run finishing must not prematurely complete the turn.
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()
  await emit(page, 'turn.finished', {
    status: 'failed', error_kind: 'rate_limited', retryable: true, response: '',
  })
  await expect(page.getByText('执行失败：模型服务限流，重试次数已用尽')).toBeVisible()
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0)
})

test('cancelled turn remains cancelled after legacy completion event', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByPlaceholder('问任何问题').fill('停止一个复杂任务')
  await page.getByPlaceholder('问任何问题').press('Enter')
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await emit(page, 'turn.finished', { status: 'cancelled', response: '已停止生成（用户中断）' })
  await emit(page, 'turn.completed', { status: 'cancelled', response: '已停止生成（用户中断）' })
  await expect(page.getByText('已停止生成（用户中断）')).toHaveCount(1)
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0)
})

test('local token login gates the chat before session API calls', async ({ page }) => {
  const backend = await mockBackend(page)
  await page.route('**/api/auth/status', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ required: true, authenticated: false }),
  }))
  await page.route('**/api/auth/login', route => {
    const ok = route.request().postDataJSON().token === 'secret-for-test'
    return route.fulfill({
      status: ok ? 200 : 401, contentType: 'application/json',
      body: JSON.stringify(ok ? { status: 'ok' } : { detail: 'Invalid login token' }),
    })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '解锁 WodeAgent' })).toBeVisible()
  await expect(page.getByPlaceholder('问任何问题')).toHaveCount(0)
  await page.getByRole('textbox', { name: '本地访问令牌' }).fill('wrong')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('textbox', { name: '本地访问令牌' }).fill('secret-for-test')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByText('Hi, 我是 MyCodeAgent')).toBeVisible()
  await expect(page.getByPlaceholder('问任何问题')).toBeVisible()
  const saved = await page.evaluate(() => {
    return JSON.stringify(Object.keys(localStorage).map(x => [x, localStorage.getItem(x)]))
  })
  expect(saved).not.toContain('secret-for-test')
})
