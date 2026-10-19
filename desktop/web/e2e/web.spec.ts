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
    if (path.endsWith('/execution-history') && method === 'GET') return ok({ tools: [] })
    if (path === '/api/sandbox/terminal-capabilities' && method === 'GET') return ok({enabled:false,backend:'docker',interactive:true})
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

test('compiler upgrade preserves control styles and hidden-sibling spacing', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await expect(page.getByText('Hi, 我是 Ridge')).toBeVisible()
  // These expectations were measured against the generated Tailwind 3 stylesheet.
  // Use classes already present in the UI, so this also detects missing utilities.
  const styles = await page.evaluate(() => {
    const fixture = document.createElement('section')
    fixture.innerHTML = '<button id="style-control" class="border rounded-sm shadow-xs bg-gray-50 text-gray-700">control</button>' +
      '<button id="style-disabled" disabled>disabled</button><input id="style-placeholder" placeholder="text"><div id="style-spacing" class="space-y-2"><p>A</p><p hidden>B</p><p>C</p></div>' +
      '<span id="style-font" class="font-mono"></span>'
    document.body.append(fixture)
    const control = getComputedStyle(fixture.querySelector('#style-control')!)
    const result = {
      background: control.backgroundColor, color: control.color,
      border: control.borderColor, borderWidth: control.borderWidth,
      radius: control.borderRadius, shadow: control.boxShadow, cursor: control.cursor,
      disabledCursor: getComputedStyle(fixture.querySelector('#style-disabled')!).cursor,
      placeholder: getComputedStyle(fixture.querySelector('#style-placeholder')!, '::placeholder').color,
      font: getComputedStyle(fixture.querySelector('#style-font')!).fontFamily,
      spacing: [...fixture.querySelector('#style-spacing')!.children].map(child => {
        const style = getComputedStyle(child)
        return [style.marginTop, style.marginBottom, style.display]
      }),
    }
    fixture.remove()
    return result
  })
  expect(styles).toMatchObject({
    background: 'rgb(249, 250, 251)', color: 'rgb(55, 65, 81)',
    border: 'rgb(229, 231, 235)', borderWidth: '1px', radius: '4px', cursor: 'pointer', disabledCursor: 'default',
    placeholder: 'rgb(156, 163, 175)',
    font: '"JetBrains Mono", "Fira Code", Consolas, monospace',
    spacing: [['0px', '0px', 'block'], ['0px', '0px', 'none'], ['8px', '0px', 'block']],
  })
  expect(styles.shadow).toContain('rgba(0, 0, 0, 0.05) 0px 1px 2px 0px')
})

test('compiler upgrade preserves ToolCard and modal code utility styles', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByPlaceholder('问任何问题').fill('检查工具样式')
  await page.getByPlaceholder('问任何问题').press('Enter')
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await emit(page, 'tool.started', { tool_call_id: 'style-tool', tool: 'Read', input: { path: 'hello.py' } })
  await emit(page, 'tool.completed', {
    tool_call_id: 'style-tool', tool: 'Read', status: 'success', output: 'style-tool-output',
  })
  await page.getByRole('button', { name: 'Read 工具详情' }).click()
  const toolBlocks = page.locator('pre').filter({ hasText: /hello.py|style-tool-output/ })
  await expect(toolBlocks).toHaveCount(2)
  // Frozen v3 measurements for the actual ConversationWidgets class combinations.
  for (const block of await toolBlocks.all()) {
    await expect.soft(block).toHaveCSS('font-size', '11px', { timeout: 500 })
    await expect.soft(block).toHaveCSS('padding', '8px', { timeout: 500 })
    await expect.soft(block).toHaveCSS('border-radius', '8px', { timeout: 500 })
    // v4 serializes this color as oklab; compare rendered sRGB against v3.
    const background = await block.evaluate(element => {
      const canvas = document.createElement('canvas')
      canvas.width = canvas.height = 1
      const context = canvas.getContext('2d')!
      context.fillStyle = getComputedStyle(element).backgroundColor
      context.fillRect(0, 0, 1, 1)
      return [...context.getImageData(0, 0, 1, 1).data]
    })
    expect.soft(background).toEqual([255, 255, 255, 179])
  }
  await emit(page, 'ask_user.requested', { request_id: 'style-ask', prompt: 'style-ask-prompt' })
  const prompt = page.locator('pre').filter({ hasText: 'style-ask-prompt' })
  await expect.soft(prompt).toHaveCSS('font-size', '14px', { timeout: 500 })
  await expect.soft(prompt).toHaveCSS('padding', '12px', { timeout: 500 })
  await expect.soft(prompt).toHaveCSS('border-radius', '12px', { timeout: 500 })
  await expect.soft(prompt).toHaveCSS('background-color', 'rgb(249, 250, 251)', { timeout: 500 })
  await page.getByPlaceholder('输入你的回答...').fill('answer')
  await page.getByRole('button', { name: '回答', exact: true }).click()
  await emit(page, 'permission.requested', {
    request_id: 'style-permission', tool: 'Read', path: 'style-path.py', action: 'read',
  })
  await expect.soft(page.locator('code').filter({ hasText: 'style-path.py' })).toHaveCSS('font-size', '12px', { timeout: 500 })
  await page.getByRole('button', { name: '拒绝', exact: true }).click()
  await emit(page, 'llm.delta', { kind: 'content', text: '```python\nprint("component styles")\n```' })
  // Deliberate Markdown component rules must still override the element defaults.
  const markdown = page.locator('.answer-code-block pre')
  await expect(markdown).toHaveCSS('padding', '13px 16px')
  await expect(markdown).toHaveCSS('border-radius', '0px')
  await expect(markdown).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await expect(markdown.locator('code')).toHaveCSS('font-size', '12px')
})

test('compiler upgrade preserves AuthGate focus outline and ring', async ({ page }) => {
  await mockBackend(page)
  await page.route('**/api/auth/status', route => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ required: true, authenticated: false }),
  }))
  await page.goto('/')
  const input = page.getByRole('textbox', { name: '本地访问令牌' })
  await input.focus()
  expect(await input.evaluate(element => element.matches(':focus-visible'))).toBe(true)
  await expect(input).toHaveCSS('outline-color', 'rgba(0, 0, 0, 0)', { timeout: 500 })
  await expect(input).toHaveCSS('outline-style', 'solid', { timeout: 500 })
  await expect(input).toHaveCSS('outline-width', '2px', { timeout: 500 })
  await expect(input).toHaveCSS('outline-offset', '2px', { timeout: 500 })
  expect(await input.evaluate(element => getComputedStyle(element).boxShadow))
    .toContain('rgb(191, 219, 254) 0px 0px 0px 2px')
  await input.fill('focus-test')
  const login = page.getByRole('button', { name: '登录', exact: true })
  await login.focus()
  await expect(login).toHaveCSS('outline-color', 'rgb(70, 114, 186)')
  await expect(login).toHaveCSS('outline-style', 'solid')
})

test('compiler upgrade preserves the session rename input focus indicator', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await page.getByRole('button', { name: '管理会话 新会话', exact: true }).click()
  await page.getByRole('button', { name: '重命名', exact: true }).click()
  const input = page.getByRole('textbox', { name: '重命名会话' })
  await expect(input).toBeFocused()
  expect(await input.evaluate(element => element.matches(':focus-visible'))).toBe(true)
  await expect(input).toHaveCSS('outline-color', 'rgb(70, 114, 186)', { timeout: 500 })
  await expect(input).toHaveCSS('outline-style', 'solid')
  await expect(input).toHaveCSS('outline-width', '2px')
  await expect(input).toHaveCSS('outline-offset', '2px')
  await expect(input).toHaveCSS('box-shadow', 'none')
})

test('compiler upgrade preserves each Plugins panel spacing level', async ({ page }) => {
  await mockBackend(page)
  await page.route('**/api/plugins', route => route.fulfill({
    contentType: 'application/json', body: JSON.stringify([{
      name: 'spacing-plugin', version: '1', description: 'Spacing fixture',
      enabled: false, trusted: false, skills: 1, has_code: false,
    }]),
  }))
  await page.goto('/')
  await page.getByRole('button', { name: '插件', exact: true }).click()
  const install = page.locator('div.space-y-3').filter({ has: page.locator('#plugin-upload') })
  const article = page.locator('article').filter({ hasText: 'spacing-plugin' })
  await expect(article).toBeVisible()
  await expect.soft(install).toHaveCSS('margin-top', '20px', { timeout: 500 })
  await expect.soft(article).toHaveCSS('margin-top', '20px', { timeout: 500 })
  await expect.soft(page.locator('#plugin-upload')).toHaveCSS('margin-top', '12px', { timeout: 500 })
  await expect.soft(install.locator('p')).toHaveCSS('margin-top', '12px', { timeout: 500 })
  await expect.soft(article.locator(':scope > div').last()).toHaveCSS('margin-top', '12px', { timeout: 500 })
})

test('compiler upgrade preserves explicit first-child margin in spacing containers', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await expect(page.getByText('Hi, 我是 Ridge')).toBeVisible()
  const margins = await page.evaluate(() => {
    const fixture = document.createElement('div')
    fixture.className = 'space-y-2'
    fixture.innerHTML = '<p class="mb-4">First</p><p hidden>Hidden</p><p>Last</p>'
    document.body.append(fixture)
    const result = [...fixture.children].map(child => {
      const style = getComputedStyle(child)
      return [style.marginTop, style.marginBottom]
    })
    fixture.remove()
    return result
  })
  expect(margins).toEqual([['0px', '16px'], ['0px', '0px'], ['8px', '0px']])
})

test('first message, live tokens, tool log and interruption', async ({ page }) => {
  const state = await mockBackend(page)
  await page.goto('/')
  await expect(page.getByText('Hi, 我是 Ridge')).toBeVisible()
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

test('new selection ignores frames from a stale prior WebSocket', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect(page.getByText('实时连接正常')).toBeVisible()
  const firstSocketCount = await page.evaluate(() => (window as any).__mockSockets.length)
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect.poll(() => page.evaluate(() => (window as any).__mockSockets.length))
    .toBe(firstSocketCount + 1)
  await expect(page.getByText('实时连接正常')).toBeVisible()

  await page.evaluate(() => {
    const sockets = (window as any).__mockSockets
    // The prior socket's queued event must not mutate the new selection,
    // even when both selections happen to reuse the same session id.
    sockets[sockets.length - 2].onmessage?.({ data: JSON.stringify({
      type: 'llm.delta', seq: 1, step: 2,
      payload: { kind: 'content', text: 'STALE-WEBSOCKET-FRAME' },
    }) })
    ;(window as any).__emitAgent({
      type: 'llm.delta', seq: 1, step: 2,
      payload: { kind: 'content', text: 'CURRENT-SESSION-FRAME' },
    })
  })
  await expect(page.getByText('CURRENT-SESSION-FRAME')).toBeVisible()
  await expect(page.getByText('STALE-WEBSOCKET-FRAME')).toHaveCount(0)
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

for (const [kind, message] of [
  ['output_truncated', '模型输出已达到长度限制，本轮任务未完成'],
  ['empty_response', '模型连续返回空回复，本轮任务失败'],
]) {
  test(`incomplete model response explains ${kind}`, async ({ page }) => {
    await mockBackend(page)
    await page.goto('/')
    await page.getByPlaceholder('问任何问题').fill('检查模型输出')
    await page.getByPlaceholder('问任何问题').press('Enter')
    await expect(page.getByText('实时连接正常')).toBeVisible()
    await emit(page, 'turn.finished', { status: 'failed', error_kind: kind, response: '' })
    await expect(page.getByText(`执行失败：${message}`)).toBeVisible()
    await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0)
  })
}

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
  await expect(page.getByRole('heading', { name: '解锁 Ridge' })).toBeVisible()
  await expect(page.getByPlaceholder('问任何问题')).toHaveCount(0)
  await page.getByRole('textbox', { name: '本地访问令牌' }).fill('wrong')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByRole('textbox', { name: '本地访问令牌' }).fill('secret-for-test')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByText('Hi, 我是 Ridge')).toBeVisible()
  await expect(page.getByPlaceholder('问任何问题')).toBeVisible()
  const saved = await page.evaluate(() => {
    return JSON.stringify(Object.keys(localStorage).map(x => [x, localStorage.getItem(x)]))
  })
  expect(saved).not.toContain('secret-for-test')
})

test('Plugins panel safely installs, enables, and removes a project plugin', async ({ page }) => {
  await mockBackend(page)
  let installed = false
  let enabled = false
  await page.route('**/api/plugins**', async route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()
    let status = 200
    let result: unknown = {}
    if (path === '/api/plugins' && method === 'GET') {
      result = installed ? [{
        name: 'review-kit', version: '1.1', description: 'Review guidance',
        enabled, trusted: false, skills: 1, has_code: false,
      }] : []
    } else if (path === '/api/plugins/install' && method === 'POST') {
      installed = true
      status = 201
      result = { status: 'installed', name: 'review-kit', enabled: false }
    } else if (path === '/api/plugins/review-kit/enabled' && method === 'PUT') {
      enabled = route.request().postDataJSON().enabled
      result = { name: 'review-kit', enabled }
    } else if (path === '/api/plugins/review-kit' && method === 'DELETE') {
      installed = false
      result = { status: 'deleted' }
    }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(result) })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '插件', exact: true }).click()
  await expect(page.getByRole('heading', { name: '插件管理' })).toBeVisible()
  await expect(page.getByText('尚未安装插件')).toBeVisible()
  await page.locator('#plugin-upload').setInputFiles({
    name: 'review-kit.zip', mimeType: 'application/zip',
    buffer: Buffer.from('mock zip bytes'),
  })
  await expect(page.getByRole('heading', { name: /review-kit/ })).toBeVisible()
  await page.getByRole('checkbox', { name: '启用 review-kit' }).check()
  await expect(page.getByRole('checkbox', { name: '启用 review-kit' })).toBeChecked()
  page.once('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '卸载插件' }).click()
  await expect(page.getByText('尚未安装插件')).toBeVisible()
})


test('mobile sidebar opens and switches to workspace tools', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockBackend(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Hi, 我是 Ridge' })).toBeVisible()
  await page.getByRole('button', { name: '打开侧边栏' }).click()
  await expect(page.locator('aside[aria-label="主导航"]')).toHaveClass(/mobile-open/)
  await page.getByRole('button', { name: '文件', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作区文件' })).toBeVisible()
  await expect(page.locator('aside[aria-label="主导航"]')).not.toHaveClass(/mobile-open/)
})

test('file attachment can create a session from an empty chat', async ({ page }) => {
  const state = await mockBackend(page)
  await page.goto('/')
  await page.getByLabel('上传文件').setInputFiles({
    name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello'),
  })
  await expect.poll(() => state.uploads).toBe(1)
  await expect(page.getByLabel('待发送的附件').getByText('uploaded.txt')).toBeVisible()
  await page.getByPlaceholder('问任何问题').fill('检查附件')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect.poll(() => state.messages).toEqual(['检查附件\n@uploaded.txt'])
  await expect(page.locator('.chat-composer')).toHaveCount(1)
  await expect(page.getByPlaceholder('问任何问题')).toHaveCount(1)
})

test('safe code blocks render in the conversation with copy controls', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByPlaceholder('问任何问题').fill('写一个例子')
  await page.getByPlaceholder('问任何问题').press('Enter')
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await emit(page, 'llm.delta', {
    kind: 'content', text: '示例：\n```python\nprint(1 + 1)\n```',
  })
  await expect(page.locator('.answer-code-block code')).toHaveText('print(1 + 1)')
  await expect(page.getByRole('button', { name: '复制代码' })).toBeVisible()
})


test('markdown reply renders headings, lists, safe links and highlighted code', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByPlaceholder('问任何问题').fill('总结')
  await page.getByPlaceholder('问任何问题').press('Enter')
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await emit(page, 'llm.delta', {
    kind: 'content', text: '# 修复报告\n\n- **已完成**\n- `safe`\n\n[安全](https://example.org)\n\n[不安全](javascript:alert(1))\n\n```ts\nconst result = 42\n```',
  })
  await expect(page.getByRole('heading', { name: '修复报告' })).toBeVisible()
  await expect(page.getByText('已完成')).toBeVisible()
  await expect(page.getByRole('link', { name: '安全', exact: true })).toHaveAttribute('href', 'https://example.org')
  await expect(page.getByRole('link', { name: '不安全' })).toHaveCount(0)
  await expect(page.locator('.answer-code-block code')).toContainText('const result = 42')
  await expect(page.locator('.syntax-keyword')).toContainText('const')
})

test('execution inspector shows real tool events and sandbox output only', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await emit(page, 'tool.started', { tool_call_id: 'shell-1', tool: 'Bash', input: { command: 'echo sandbox-pass' } })
  await emit(page, 'tool.completed', {
    tool_call_id: 'shell-1', tool: 'Bash', status: 'success',
    output: JSON.stringify({ status: 'success', data: { sandbox: 'docker', stdout: 'sandbox-pass', stderr: '', exit_code: 0 } }),
  })
  await page.getByRole('button', { name: /执行详情/ }).click()
  await expect(page.getByText('Agent 执行详情')).toBeVisible()
  await expect(page.getByText('Step 1 · Bash')).toBeVisible()
  await page.getByRole('button', { name: 'Sandbox 输出' }).click()
  await expect(page.getByText('sandbox-pass', { exact: true })).toBeVisible()
  await expect(page.getByText('退出码 0')).toBeVisible()
  await page.getByRole('button', { name: '关闭执行详情' }).click()
  await expect(page.getByText('Agent 执行详情')).toHaveCount(0)
})

test('file snapshot Diff only compares re-read workspace content', async ({ page }) => {
  await mockBackend(page)
  let reads = 0
  await page.route('**/api/sessions/*/files/content?*', route => {
    reads++
    return route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        path: 'hello.py',
        content: reads === 1 ? '1\tprint("before")' : '1\tprint("after")',
        truncated: false,
      }),
    })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await page.getByRole('button', { name: '文件', exact: true }).click()
  await page.getByRole('button', { name: /hello.py/ }).click()
  await expect(page.getByText('print("before")', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '对比最新' }).click()
  await expect(page.getByLabel('文件快照对比')).toBeVisible()
  await expect(page.locator('.diff-added')).toContainText('print("after")')
  await expect(page.locator('.diff-removed')).toContainText('print("before")')
  await page.getByRole('button', { name: '更新基线' }).click()
  await page.getByRole('button', { name: '对比最新' }).click()
  await expect(page.getByText('与记录的基线相同，尚无变化。')).toBeVisible()
})


test('GFM table and task list render with sanitizer stripping scripts and remote images', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByPlaceholder('问任何问题').fill('Markdown 表格')
  await page.getByPlaceholder('问任何问题').press('Enter')
  await expect(page.getByText('实时连接正常')).toBeVisible()
  await emit(page, 'llm.delta', { kind: 'content',
    text: '| 列 | 值 |\n| --- | --- |\n| 状态 | 完成 |\n\n- [x] 完成测试\n\n<script>alert("bad")</script>\n<img src="https://tracker.invalid/pixel" onerror="alert(1)">',
  })
  await expect(page.getByRole('table')).toBeVisible()
  await expect(page.getByText('完成测试')).toBeVisible()
  await expect(page.locator('.markdown-html script')).toHaveCount(0)
  await expect(page.locator('.markdown-html img')).toHaveCount(0)
})

test('Web UI shows explicit Git Diff mode without confusing browser snapshots', async ({ page }) => {
  await mockBackend(page)
  await page.route('**/api/sessions/*/git/diff?*', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({available: true, path:'hello.py',base:'HEAD', patch:'@@ -1 +1 @@\n-before\n+after\n', reason:''}),
  }))
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await page.getByRole('button', { name: '文件', exact: true }).click()
  await page.getByRole('button', { name: /hello.py/ }).click()
  await page.getByRole('button', { name: 'Git Diff' }).click()
  await expect(page.getByLabel('Git Diff 内容')).toContainText('+after')
  await expect(page.getByText('「对比最新」是本地快照比较', { exact: false })).toBeVisible()
})

test('PTY defaults to disabled without authenticated opt-in', async ({ page }) => {
  await mockBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建任务' }).click()
  await page.getByRole('button', { name: /执行详情/ }).click()
  await page.getByRole('button', { name: 'Docker TTY' }).click()
  await expect(page.getByText('Docker TTY 未启用或当前服务器不支持。')).toBeVisible()
  await expect(page.getByRole('button', { name: '连接 Docker TTY' })).toHaveCount(0)
})
