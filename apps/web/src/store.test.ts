import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useAppStore } from './store'

/**
 * store.ts 单测 — 基于 useAppStore (Zustand create) 的真实 API。
 *
 * 测试策略：Zustand 的 create() 返回单例 store，所有 test 共享同一份 state。
 * beforeEach 通过 useAppStore.setState(...) 把数据字段重置回初始默认值；
 * action 函数引用本身是稳定的不需要重置。
 */
describe('useAppStore', () => {
  beforeEach(() => {
    // 仅重置数据字段；action 引用保持不变（Zustand setState 是浅合并）。
    useAppStore.setState({
      selectedAgentId: null,
      activeView: 'tree',
      rightPanelTab: 'chat',
      chatSessions: {},
      orgTreeVersion: 0,
      goalsVersion: 0,
      goalsUpdatedProjectId: null,
      socketReconnectVersion: 0,
      questionVersion: 0,
      activeCommunications: [],
      projects: [],
      selectedProjectId: null,
      apiKey: null,
      pendingApprovals: {},
      showAddAgent: false,
      addAgentParentId: null,
      processingAgents: [],
      agentDispositions: {},
      userPingAgentIds: [],
      agentAlarms: {},
      agentHealth: {},
      pendingInitialMessage: null,
      activityFeed: [],
      _activityFeedInternal: [],
      _activityRafPending: false,
      toasts: [],
      debugLogs: [],
    })
  })

  it('初始状态符合默认值', () => {
    const s = useAppStore.getState()
    expect(s.selectedAgentId).toBeNull()
    expect(s.activeView).toBe('tree')
    expect(s.rightPanelTab).toBe('chat')
    expect(s.chatSessions).toEqual({})
    expect(s.orgTreeVersion).toBe(0)
    expect(s.goalsVersion).toBe(0)
    expect(s.goalsUpdatedProjectId).toBeNull()
    expect(s.socketReconnectVersion).toBe(0)
    expect(s.questionVersion).toBe(0)
    expect(s.projects).toEqual([])
    expect(s.selectedProjectId).toBeNull()
    expect(s.apiKey).toBeNull()
    expect(s.pendingApprovals).toEqual({})
    expect(s.processingAgents).toEqual([])
    expect(s.agentHealth).toEqual({})
    expect(s.activityFeed).toEqual([])
    expect(s.toasts).toEqual([])
    expect(s.debugLogs).toEqual([])
  })

  it('setSelectedAgent / setActiveView / setRightPanelTab 更新对应字段', () => {
    const s = useAppStore.getState()
    s.setSelectedAgent('agent-1')
    s.setActiveView('office')
    s.setRightPanelTab('logs')

    const next = useAppStore.getState()
    expect(next.selectedAgentId).toBe('agent-1')
    expect(next.activeView).toBe('office')
    expect(next.rightPanelTab).toBe('logs')
  })

  it('addMessage 追加消息；replaceMessage 替换；removeMessage 删除；setChatMessages 覆盖', () => {
    const { addMessage, replaceMessage, removeMessage, setChatMessages } =
      useAppStore.getState()

    addMessage('a1', { id: 'm1', role: 'user', content: 'hello', timestamp: 1 })
    addMessage('a1', { id: 'm2', role: 'assistant', content: 'hi', timestamp: 2 })
    expect(useAppStore.getState().chatSessions['a1']).toHaveLength(2)

    // replace by oldId
    replaceMessage('a1', 'm1', {
      id: 'm1',
      role: 'user',
      content: 'hello-edited',
      timestamp: 1,
    })
    expect(useAppStore.getState().chatSessions['a1'][0].content).toBe('hello-edited')

    // remove by msgId
    removeMessage('a1', 'm2')
    expect(useAppStore.getState().chatSessions['a1']).toHaveLength(1)
    expect(useAppStore.getState().chatSessions['a1'][0].id).toBe('m1')

    // setChatMessages overwrite (not merge)
    setChatMessages('a1', [
      { id: 'x', role: 'system', content: 'reset', timestamp: 9 },
    ])
    expect(useAppStore.getState().chatSessions['a1']).toHaveLength(1)
    expect(useAppStore.getState().chatSessions['a1'][0].id).toBe('x')
  })

  it('clearChatSessions 清空所有 agent 会话', () => {
    useAppStore.getState().addMessage('a1', {
      id: 'm1',
      role: 'user',
      content: 'hi',
      timestamp: 1,
    })
    useAppStore.getState().addMessage('a2', {
      id: 'm2',
      role: 'user',
      content: 'yo',
      timestamp: 2,
    })
    expect(Object.keys(useAppStore.getState().chatSessions)).toHaveLength(2)

    useAppStore.getState().clearChatSessions()
    expect(useAppStore.getState().chatSessions).toEqual({})
  })

  it('refreshOrgTree / bumpGoalsVersion / bumpSocketReconnect / bumpQuestionVersion 自增版本号', () => {
    const s = useAppStore.getState()
    s.refreshOrgTree()
    s.bumpGoalsVersion('proj-1')
    s.bumpSocketReconnect()
    s.bumpQuestionVersion()

    const next = useAppStore.getState()
    expect(next.orgTreeVersion).toBe(1)
    expect(next.goalsVersion).toBe(1)
    expect(next.goalsUpdatedProjectId).toBe('proj-1')
    expect(next.socketReconnectVersion).toBe(1)
    expect(next.questionVersion).toBe(1)
  })

  it('setPendingApprovals / setAllPendingApprovals / removeApproval 维护审批映射', () => {
    const { setPendingApprovals, setAllPendingApprovals, removeApproval } =
      useAppStore.getState()

    setPendingApprovals('a1', [
      {
        id: 'r1',
        agentId: 'a1',
        toolName: 'bash',
        toolArguments: '{}',
        description: 'run',
        status: 'pending',
        createdAt: 1,
      },
    ])
    expect(useAppStore.getState().pendingApprovals['a1']).toHaveLength(1)

    // setAllPendingApprovals 按 agentId 重新分组，整体替换（不合并旧值）
    setAllPendingApprovals([
      {
        id: 'r2',
        agentId: 'a1',
        toolName: 'write',
        toolArguments: '{}',
        description: 'w',
        status: 'pending',
        createdAt: 2,
      },
      {
        id: 'r3',
        agentId: 'a2',
        toolName: 'read',
        toolArguments: '{}',
        description: 'r',
        status: 'pending',
        createdAt: 3,
      },
    ])
    expect(useAppStore.getState().pendingApprovals['a1']).toHaveLength(1)
    expect(useAppStore.getState().pendingApprovals['a1'][0].id).toBe('r2')
    expect(useAppStore.getState().pendingApprovals['a2']).toHaveLength(1)

    // removeApproval 跨所有 agent 过滤 id
    removeApproval('r2')
    expect(useAppStore.getState().pendingApprovals['a1']).toHaveLength(0)
    expect(useAppStore.getState().pendingApprovals['a2']).toHaveLength(1)
  })

  it('openAddAgent / closeAddAgent 控制对话框状态', () => {
    useAppStore.getState().openAddAgent('parent-1')
    let s = useAppStore.getState()
    expect(s.showAddAgent).toBe(true)
    expect(s.addAgentParentId).toBe('parent-1')

    // 无参数调用 openAddAgent → parentId 为 null
    useAppStore.getState().openAddAgent()
    s = useAppStore.getState()
    expect(s.showAddAgent).toBe(true)
    expect(s.addAgentParentId).toBeNull()

    useAppStore.getState().closeAddAgent()
    s = useAppStore.getState()
    expect(s.showAddAgent).toBe(false)
    expect(s.addAgentParentId).toBeNull()
  })

  it('setProcessingAgents 全量替换；updateProcessingAgent 增删单个 agent', () => {
    const { setProcessingAgents, updateProcessingAgent } = useAppStore.getState()
    setProcessingAgents(['a1', 'a2'])
    expect(useAppStore.getState().processingAgents).toEqual(['a1', 'a2'])

    // 加入新 agent
    updateProcessingAgent('a3', true)
    expect(useAppStore.getState().processingAgents).toContain('a3')

    // 移除已有 agent
    updateProcessingAgent('a1', false)
    expect(useAppStore.getState().processingAgents).not.toContain('a1')

    // 重复设置相同状态应无变化（短路返回 state）
    const before = useAppStore.getState().processingAgents
    updateProcessingAgent('a2', true)
    expect(useAppStore.getState().processingAgents).toBe(before)
  })

  it('setAgentHealth 在 error 时写入，ok / null 时清除；clearAgentHealth 全清', () => {
    const { setAgentHealth, clearAgentHealth } = useAppStore.getState()

    setAgentHealth('a1', { health: 'error', message: 'fail', at: 1, projectId: 'p1' })
    expect(useAppStore.getState().agentHealth['a1']).toBeDefined()
    expect(useAppStore.getState().agentHealth['a1'].health).toBe('error')

    // ok 清除已有 entry
    setAgentHealth('a1', { health: 'ok', message: '', at: 2 })
    expect(useAppStore.getState().agentHealth['a1']).toBeUndefined()

    // null 也清除
    setAgentHealth('a1', { health: 'error', message: 'fail2', at: 3 })
    setAgentHealth('a1', null)
    expect(useAppStore.getState().agentHealth['a1']).toBeUndefined()

    // clearAgentHealth 全清
    setAgentHealth('a1', { health: 'error', message: 'fail3', at: 4 })
    setAgentHealth('a2', { health: 'error', message: 'fail4', at: 5 })
    clearAgentHealth()
    expect(useAppStore.getState().agentHealth).toEqual({})
  })

  it('showToast 添加 toast 并在 dismissToast 后移除', () => {
    // showToast 内部用 setTimeout 排程自动消失；用 fake timers 避免污染后续测试
    vi.useFakeTimers()
    try {
      useAppStore.getState().showToast('hello', 'info')
      useAppStore.getState().showToast('boom', 'error')
      expect(useAppStore.getState().toasts).toHaveLength(2)
      expect(useAppStore.getState().toasts[0].message).toBe('hello')
      expect(useAppStore.getState().toasts[1].type).toBe('error')

      const firstId = useAppStore.getState().toasts[0].id
      useAppStore.getState().dismissToast(firstId)
      expect(useAppStore.getState().toasts).toHaveLength(1)
      expect(
        useAppStore.getState().toasts.find((t) => t.id === firstId)
      ).toBeUndefined()
    } finally {
      vi.useRealTimers()
    }
  })

  it('addDebugLog 追加日志；clearDebugLogs 清空', () => {
    const { addDebugLog, clearDebugLogs } = useAppStore.getState()
    addDebugLog({ category: 'api', message: 'GET /api/projects' })
    addDebugLog({ category: 'ws', message: 'streamChat called' })
    expect(useAppStore.getState().debugLogs).toHaveLength(2)
    expect(useAppStore.getState().debugLogs[0].category).toBe('api')
    expect(useAppStore.getState().debugLogs[1].category).toBe('ws')

    clearDebugLogs()
    expect(useAppStore.getState().debugLogs).toHaveLength(0)
  })

  it('addActivity 拦截 agent_health 事件：写入 agentHealth 而非 activityFeed', () => {
    useAppStore.getState().addActivity({
      type: 'agent_health',
      agentId: 'a1',
      health: 'error',
      message: 'LLM down',
      at: 12345,
      projectId: 'p1',
    } as any)

    expect(useAppStore.getState().agentHealth['a1']).toBeDefined()
    expect(useAppStore.getState().agentHealth['a1'].health).toBe('error')
    expect(useAppStore.getState().agentHealth['a1'].message).toBe('LLM down')
    // agent_health 事件不应进入 activityFeed
    expect(useAppStore.getState().activityFeed).toHaveLength(0)
  })

  it('addActivity 非 delta 事件直接同步写入 activityFeed', () => {
    useAppStore.getState().addActivity({
      agentId: 'a1',
      agentName: 'Foo',
      type: 'text',
      content: 'hello',
      timestamp: 1,
    })
    expect(useAppStore.getState().activityFeed).toHaveLength(1)
    expect(useAppStore.getState().activityFeed[0].content).toBe('hello')
    expect(useAppStore.getState().activityFeed[0].agentName).toBe('Foo')
  })
})
