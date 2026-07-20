import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

/**
 * api.ts 单测 — 覆盖 REST 路径 (fetchJSON)。
 *
 * api.ts 顶部 import { Socket, Channel } from "phoenix" 用于 WebSocket 流式；
 * 本测试只测 REST 函数，所以先 vi.mock("phoenix") 防止真实 WS 连接。
 * 所有 REST 请求最终走全局 fetch，用 vi.stubGlobal('fetch', mockFetch) 拦截。
 */

// 必须在 import api 之前 mock phoenix（vitest 会把 vi.mock 提升到文件顶部）
vi.mock('phoenix', () => ({
  Socket: vi.fn().mockImplementation(() => ({
    connect: vi.fn(),
    channel: vi.fn().mockReturnValue({
      on: vi.fn(),
      join: vi.fn().mockReturnValue({ receive: vi.fn() }),
      push: vi.fn(),
      leave: vi.fn(),
      state: 'closed',
    }),
    isConnected: () => false,
  })),
  Channel: vi.fn(),
}))

import {
  getProjects,
  createProject,
  getAgent,
  getPendingApprovals,
  getModels,
  respondToApproval,
  setApiKey,
} from './api'

// ---- fetch mock 基础设施 --------------------------------------------------

const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

/** 构造一个最小可用的 Response-like 对象给 fetchJSON 用。 */
function jsonResponse(body: unknown, status = 200, statusText = 'OK'): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    text: () =>
      Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  } as Response
}

beforeEach(() => {
  mockFetch.mockReset()
  setApiKey(null) // 清掉模块级 _apiKey
})

afterEach(() => {
  setApiKey(null)
  // 清掉 401 测试可能注入的 __hwStore
  if ((window as any).__hwStore && (window as any).__hwStore.__testOnly) {
    delete (window as any).__hwStore
  }
})

// ---- 测试用例 --------------------------------------------------------------

describe('api.ts REST helpers', () => {
  it('getProjects: 成功返回 data.projects 数组，请求 GET /api/projects', async () => {
    const projects = [
      { id: 'p1', name: 'Demo', createdAt: 1 },
    ]
    mockFetch.mockResolvedValueOnce(jsonResponse({ projects }))

    const result = await getProjects()

    expect(result).toEqual(projects)
    expect(mockFetch).toHaveBeenCalledOnce()
    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('/api/projects')
    // GET 请求不显式带 method
    expect(init?.method).toBeUndefined()
  })

  it('getProjects: HTTP 500 抛异常且错误信息含状态码', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: 'boom' }, 500, 'Internal Server Error')
    )
    await expect(getProjects()).rejects.toThrow(/HTTP 500/)
  })

  it('createProject: POST /api/projects 带正确 body 与 Content-Type', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ id: 'p2', name: 'New' }))

    await createProject('New', '/tmp/x', 'desc', 'hierarchy', 'zh')

    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('/api/projects')
    expect(init?.method).toBe('POST')
    const headers = init?.headers as Headers
    expect(headers.get('Content-Type')).toBe('application/json')
    const body = JSON.parse(init?.body as string)
    expect(body).toMatchObject({
      name: 'New',
      workspacePath: '/tmp/x',
      description: 'desc',
      orgParadigm: 'hierarchy',
      language: 'zh',
    })
  })

  it('getAgent: 解包 { agent: ... } envelope', async () => {
    const agent = { id: 'a1', name: 'Foo', role: 'executor' }
    mockFetch.mockResolvedValueOnce(jsonResponse({ agent }))

    const result = await getAgent('a1')

    expect(result).toEqual(agent)
    expect(mockFetch.mock.calls[0][0]).toBe('/api/org/agents/a1')
  })

  it('getAgent: 无 envelope 时直接返回原始数据', async () => {
    const raw = { id: 'a1', name: 'Foo', role: 'executor' }
    mockFetch.mockResolvedValueOnce(jsonResponse(raw))

    const result = await getAgent('a1')

    expect(result).toEqual(raw)
  })

  it('getPendingApprovals: 解包 data.requests 数组，请求 GET /api/permissions/pending/{agentId}', async () => {
    const requests = [
      {
        id: 'r1',
        agentId: 'a1',
        toolName: 'bash',
        toolArguments: '{}',
        description: 'run',
        status: 'pending',
        createdAt: 1,
      },
    ]
    mockFetch.mockResolvedValueOnce(jsonResponse({ requests }))

    const result = await getPendingApprovals('a1')

    expect(result).toEqual(requests)
    expect(mockFetch.mock.calls[0][0]).toBe('/api/permissions/pending/a1')
  })

  it('getModels: 解包 data.models 数组，兼容裸数组响应', async () => {
    const models = [{ id: 'm1', name: 'gpt' }]

    // 后端标准包装 { models: [...] }
    mockFetch.mockResolvedValueOnce(jsonResponse({ models }))
    expect(await getModels()).toEqual(models)

    // 兼容裸数组
    mockFetch.mockResolvedValueOnce(jsonResponse(models))
    expect(await getModels()).toEqual(models)
  })

  it('respondToApproval: POST /api/permissions/respond 带正确 body', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }))

    await respondToApproval('r1', true, false, 'note', 'p1')

    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('/api/permissions/respond')
    expect(init?.method).toBe('POST')
    const body = JSON.parse(init?.body as string)
    expect(body).toMatchObject({
      requestId: 'r1',
      approved: true,
      remember: false,
      userNote: 'note',
      projectId: 'p1',
    })
  })

  it('setApiKey: 注入 x-api-key 请求头到后续请求', async () => {
    setApiKey('secret-key')
    mockFetch.mockResolvedValueOnce(jsonResponse({ projects: [] }))

    await getProjects()

    const [, init] = mockFetch.mock.calls[0]
    const headers = init?.headers as Headers
    expect(headers.get('x-api-key')).toBe('secret-key')
  })

  it('401 响应抛异常并触发 store.showToast 提示用户设置 API Key', async () => {
    const showToast = vi.fn()
    ;(window as any).__hwStore = {
      __testOnly: true,
      getState: () => ({ showToast }),
    }

    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: 'unauthorized' }, 401, 'Unauthorized')
    )

    await expect(getProjects()).rejects.toThrow(/HTTP 401/)
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('API Key'),
      'error'
    )

    delete (window as any).__hwStore
  })
})
