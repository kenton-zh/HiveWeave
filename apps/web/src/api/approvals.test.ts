import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * FE-08 / SR-05 —— 审批归一层单测（rest.ts）。
 *
 * 关键纪律：mock fetch 一律使用**真实后端载荷形状（snake_case）**，
 * 不用理想化 camelCase mock（那正是本工作包要修的「测试全绿但真实载荷
 * 读到 undefined」的根因）。样例形状抄自后端代码与后端契约测试：
 * - SELECT 列：apps/hiveweave-py/src/hiveweave/services/approval.py
 *   （get_pending_requests / get_project_pending）
 * - 表结构：apps/hiveweave-py/src/hiveweave/db/schema.py::permission_requests
 * - 插入样例：apps/hiveweave-py/tests/test_approval_pending_dto_contract.py
 *
 * 契约锁：本文件 PENDING_APPROVAL_FIELDS 与后端契约测试的
 * PENDING_APPROVAL_FIELDS、rest.ts 的 PENDING_APPROVAL_SOURCE_FIELDS
 * 三处逐字一致 —— 后端改字段名（SELECT 列 / 表结构）后端测试红；
 * 归一层期望漂移本文件红。
 */

import {
  PENDING_APPROVAL_SOURCE_FIELDS,
  normalizePendingApproval,
  normalizePendingApprovals,
  getPendingApprovals,
  getProjectPendingApprovals,
  respondToApproval,
  setApiKey,
} from './rest'

// ── 冻结契约：后端待审批行字段名（snake_case，与后端测试逐字一致）────

const PENDING_APPROVAL_FIELDS = [
  'id',
  'agent_id',
  'tool_name',
  'tool_arguments',
  'description',
  'status',
  'created_at',
] as const

// ── 真实后端载荷样例（snake_case，抄自后端契约测试的插入行）──────────

const REAL_BACKEND_ROW = {
  id: '0d3f1c2e-4a5b-6c7d-8e9f-0a1b2c3d4e5f',
  agent_id: 'agent-93d33bb76df6',
  tool_name: 'bash',
  tool_arguments: '{"command": "Remove-Item -Recurse ./build"}',
  description: '清理构建产物（契约测试样例）',
  status: 'pending',
  created_at: 1727200000000,
}

// ---- fetch mock 基础设施 ---------------------------------------------------

const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    text: () =>
      Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  } as Response
}

beforeEach(() => {
  mockFetch.mockReset()
  setApiKey(null)
})

// ── 契约锁 ───────────────────────────────────────────────────────────────

describe('审批 DTO 契约锁（FE-08）', () => {
  it('PENDING_APPROVAL_SOURCE_FIELDS 与后端冻结字段清单逐字一致', () => {
    expect([...PENDING_APPROVAL_SOURCE_FIELDS].sort()).toEqual([
      ...PENDING_APPROVAL_FIELDS,
    ].sort())
  })

  it('归一层只消费清单内字段：真实载荷行不出现在归一输出的多余键里', () => {
    const out = normalizePendingApproval(REAL_BACKEND_ROW)
    expect(Object.keys(out).sort()).toEqual(
      [
        'id',
        'agentId',
        'toolName',
        'toolArguments',
        'description',
        'status',
        'createdAt',
        'malformed',
        'missingFields',
      ].sort(),
    )
  })
})

// ── 归一：happy path ─────────────────────────────────────────────────────

describe('normalizePendingApproval：真实 snake_case 载荷 → camelCase DTO', () => {
  it('真实后端行归一为 camelCase，且非畸形', () => {
    const out = normalizePendingApproval(REAL_BACKEND_ROW)
    expect(out).toEqual({
      id: '0d3f1c2e-4a5b-6c7d-8e9f-0a1b2c3d4e5f',
      agentId: 'agent-93d33bb76df6',
      toolName: 'bash',
      toolArguments: '{"command": "Remove-Item -Recurse ./build"}',
      description: '清理构建产物（契约测试样例）',
      status: 'pending',
      createdAt: 1727200000000,
      malformed: false,
      missingFields: [],
    })
  })

  it('snake_case 键不会被误读成 undefined（回归：SR-05 原始病灶）', () => {
    const out = normalizePendingApproval(REAL_BACKEND_ROW)
    // 旧代码直接读 agentId/toolName ⇒ undefined。归一后必须有值。
    expect(out.agentId).toBe('agent-93d33bb76df6')
    expect(out.toolName).toBe('bash')
    expect(out.createdAt).toBe(1727200000000)
  })

  it('非关键字段缺失/类型漂移 ⇒ 兜底默认值，不算畸形', () => {
    const out = normalizePendingApproval({
      id: 'r1',
      agent_id: 'a1',
      tool_name: 'bash',
      // description / status / created_at / tool_arguments 全缺
    })
    expect(out.malformed).toBe(false)
    expect(out.missingFields).toEqual([])
    expect(out.toolArguments).toBe('{}')
    expect(out.description).toBe('')
    expect(out.status).toBe('unknown')
    expect(out.createdAt).toBe(0)
  })

  it('created_at 类型漂移（字符串时间戳）归一为 0 而不是 NaN', () => {
    const out = normalizePendingApproval({
      ...REAL_BACKEND_ROW,
      created_at: '1727200000000',
    })
    expect(out.createdAt).toBe(0)
    expect(out.malformed).toBe(false)
  })
})

// ── 归一：关键字段校验（缺字段可见，不静默装空）─────────────────────────

describe('normalizePendingApproval：关键字段存在性校验', () => {
  const KEY_FIELDS = ['id', 'agent_id', 'tool_name'] as const

  for (const field of KEY_FIELDS) {
    it(`缺 ${field} ⇒ 条目标记畸形且仍返回（不丢行）`, () => {
      const broken: Record<string, unknown> = { ...REAL_BACKEND_ROW }
      delete broken[field]

      const out = normalizePendingApproval(broken)
      expect(out.malformed).toBe(true)
      expect(out.missingFields).toEqual([field])
    })
  }

  it('缺 id ⇒ 合成稳定占位 key（malformed-<index>，仅作列表 key）', () => {
    const out = normalizePendingApprovals({ requests: [{ ...REAL_BACKEND_ROW, id: undefined }] })
    expect(out[0].id).toBe('malformed-0')
    expect(out[0].malformed).toBe(true)
  })

  it('空串字段与缺失同样对待（isBlank 判据）', () => {
    const out = normalizePendingApproval({ ...REAL_BACKEND_ROW, tool_name: '  ' })
    expect(out.malformed).toBe(true)
    expect(out.missingFields).toEqual(['tool_name'])
  })

  it('非对象条目（null / 字符串 / 数字）⇒ 畸形条目可见，不抛异常', () => {
    for (const raw of [null, 'garbage', 42]) {
      const out = normalizePendingApproval(raw, 3)
      expect(out.malformed).toBe(true)
      expect(out.missingFields).toEqual(['id', 'agent_id', 'tool_name'])
      expect(out.id).toBe('malformed-3')
    }
  })

  it('混合列表：好行与畸形行都保留（length 不缩水）', () => {
    const out = normalizePendingApprovals({
      requests: [REAL_BACKEND_ROW, { id: 'x', agent_id: 'a1' }],
    })
    expect(out).toHaveLength(2)
    expect(out[0].malformed).toBe(false)
    expect(out[1].malformed).toBe(true)
    expect(out[1].missingFields).toEqual(['tool_name'])
  })
})

// ── 包络解包：故障可见，不伪装成「没有待办」─────────────────────────────

describe('normalizePendingApprovals：包络校验', () => {
  it('{requests: [...]} 标准包络解包', () => {
    const out = normalizePendingApprovals({ requests: [REAL_BACKEND_ROW] })
    expect(out).toHaveLength(1)
    expect(out[0].id).toBe(REAL_BACKEND_ROW.id)
  })

  it('裸数组包络防御性接受', () => {
    const out = normalizePendingApprovals([REAL_BACKEND_ROW])
    expect(out).toHaveLength(1)
  })

  it('包络不合法（undefined / {} / requests 非数组）⇒ 抛错而非静默返回 []', () => {
    expect(() => normalizePendingApprovals(undefined)).toThrow(/envelope/)
    expect(() => normalizePendingApprovals({})).toThrow(/envelope/)
    expect(() => normalizePendingApprovals({ requests: 'oops' })).toThrow(/envelope/)
  })
})

// ── API 函数端到端（mock fetch 用真实 snake_case 载荷）──────────────────

describe('审批 API 函数（真实载荷形状）', () => {
  it('getPendingApprovals: GET /permissions/pending/{agentId}，返回归一后 DTO', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ requests: [REAL_BACKEND_ROW] }),
    )

    const result = await getPendingApprovals('a1')

    expect(mockFetch.mock.calls[0][0]).toBe('/api/permissions/pending/a1')
    expect(result).toHaveLength(1)
    expect(result[0].agentId).toBe('agent-93d33bb76df6')
    expect(result[0].toolName).toBe('bash')
    expect(result[0].malformed).toBe(false)
  })

  it('getProjectPendingApprovals: GET /permissions/pending/project/{projectId}', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ requests: [REAL_BACKEND_ROW] }),
    )

    const result = await getProjectPendingApprovals('proj-1')

    expect(mockFetch.mock.calls[0][0]).toBe('/api/permissions/pending/project/proj-1')
    expect(result[0].id).toBe(REAL_BACKEND_ROW.id)
  })

  it('真实载荷里混入缺字段坏行：API 层保留畸形条目，不整个装空', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({
        requests: [REAL_BACKEND_ROW, { id: 'bad', agent_id: 'a1' }],
      }),
    )

    const result = await getPendingApprovals('a1')
    expect(result).toHaveLength(2)
    expect(result[1].malformed).toBe(true)
    expect(result[1].missingFields).toEqual(['tool_name'])
  })

  it('respondToApproval: POST /permissions/respond 带正确 body，透传后端明确状态', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ ok: true, requestId: 'r1', status: 'resolved' }),
    )

    const result = await respondToApproval('r1', true, false, 'note', 'p1')

    const [url, init] = mockFetch.mock.calls[0]
    expect(url).toBe('/api/permissions/respond')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(init?.body as string)).toMatchObject({
      requestId: 'r1',
      approved: true,
      remember: false,
      userNote: 'note',
      projectId: 'p1',
    })
    expect(result).toEqual({ ok: true, requestId: 'r1', status: 'resolved' })
  })
})
