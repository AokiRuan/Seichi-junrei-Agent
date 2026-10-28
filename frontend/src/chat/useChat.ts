// 对话状态与操作，组件只管渲染。讲解见 docs/10-frontend.md「状态放在哪」
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getSession, grantAccess, listSessions, needsAccessCode, streamChat } from '../api/client'
import type { SessionOut } from '../api/types'
import {
  type AssistantItem,
  type ChatItem,
  applyEvent,
  emptyAssistant,
  historyToItems,
  newId,
  stopItem,
} from './model'

// 当前会话 id 放在网址里（?s=xxx）：刷新页面能回到同一个会话，链接也能直接分享给自己的另一个标签页
function readSessionFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get('s')
}

function writeSessionToUrl(id: string | null) {
  const url = new URL(window.location.href)
  if (id) url.searchParams.set('s', id)
  else url.searchParams.delete('s')
  // replaceState 而不是 pushState：不想让每次切换会话都塞一条浏览器历史
  window.history.replaceState(null, '', url)
}

export function useChat() {
  const [sessions, setSessions] = useState<SessionOut[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [items, setItems] = useState<ChatItem[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  // 需要访问码：任何接口返回 401 时置为 true，App 改为显示输入访问码的页面
  const [locked, setLocked] = useState(false)
  // AbortController 不参与渲染，放 ref：改它不需要触发重渲染
  const abortRef = useRef<AbortController | null>(null)

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await listSessions())
    } catch (e) {
      // 侧栏加载失败不影响聊天，静默；但 401 说明要先输访问码
      if (needsAccessCode(e)) setLocked(true)
    }
  }, [])

  const openSession = useCallback(async (id: string) => {
    abortRef.current?.abort() // 正在生成时切走：先停掉，免得旧会话的事件写进新会话的界面
    // 所有 setState 都放在 await 之后：数据到了再一次性切换，界面不会先闪一下空白
    try {
      const detail = await getSession(id)
      setNotice(null)
      setSessionId(id)
      setItems(historyToItems(detail.messages))
      writeSessionToUrl(id)
    } catch (e) {
      // 401：先输访问码，网址里的会话 id 留着，解锁后再打开
      if (needsAccessCode(e)) {
        setLocked(true)
        return
      }
      // 404：会话不存在（内存存储下后端重启就丢），或者是别的访客的会话（链接是从别的浏览器拷来的）
      const gone = e instanceof ApiError && e.status === 404
      setNotice(gone ? '找不到这个会话（可能已被清除，或属于另一个浏览器），已为你开始新对话' : `加载会话失败：${String(e)}`)
      setSessionId(null)
      setItems([])
      writeSessionToUrl(null)
    }
  }, [])

  const newChat = useCallback(() => {
    abortRef.current?.abort()
    setSessionId(null)
    setItems([])
    setNotice(null)
    writeSessionToUrl(null)
  }, [])

  const send = useCallback(
    async (text: string) => {
      const message = text.trim()
      if (!message || busy) return
      setNotice(null)

      const assistant = emptyAssistant()
      const update = (f: (a: AssistantItem) => AssistantItem) =>
        setItems((prev) => prev.map((it) => (it.id === assistant.id && it.role === 'assistant' ? f(it) : it)))

      setItems((prev) => [...prev, { role: 'user', id: newId('u'), text: message }, assistant])
      setBusy(true)
      const controller = new AbortController()
      abortRef.current = controller

      try {
        for await (const ev of streamChat(message, sessionId, controller.signal)) {
          if (ev.type === 'session') {
            // 新会话：后端在第一个事件里告诉我们 id，下一轮要带上它
            setSessionId(ev.session_id)
            writeSessionToUrl(ev.session_id)
          } else {
            update((a) => applyEvent(a, ev))
          }
        }
        // 正常情况下最后一个事件是 done 或 error；都没有说明连接被意外断开
        update((a) => (a.status === 'streaming' ? applyEvent(a, { type: 'error', message: '连接意外中断' }) : a))
      } catch (e) {
        if (controller.signal.aborted) {
          update(stopItem)
        } else {
          if (needsAccessCode(e)) setLocked(true)
          const msg = e instanceof ApiError ? `${e.status}：${e.message}` : `网络错误：${String(e)}`
          update((a) => applyEvent(a, { type: 'error', message: msg }))
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null
        setBusy(false)
        refreshSessions() // 新会话出现在侧栏、已有会话按更新时间排到最前
      }
    },
    [busy, sessionId, refreshSessions],
  )

  const stop = useCallback(() => abortRef.current?.abort(), [])

  // 输对访问码后：重新拉侧栏，网址里有会话 id 就恢复它。访问码不对时 grantAccess 抛错，交给输入页显示
  const unlock = useCallback(
    async (code: string) => {
      await grantAccess(code)
      setLocked(false)
      void refreshSessions()
      const id = readSessionFromUrl()
      if (id) void openSession(id)
    },
    [refreshSessions, openSession],
  )

  // 首次加载：拉侧栏；网址里带了会话 id 就恢复它
  // effect 里只发起请求，setState 都在请求回来之后的回调里——这是「与外部系统同步」的标准写法
  useEffect(() => {
    listSessions().then(setSessions, (e) => {
      if (needsAccessCode(e)) setLocked(true)
    })
    const id = readSessionFromUrl()
    // openSession 里的 setState 全在 await 之后，并非同步调用；lint 规则看不穿 async 函数，这里是误报
    // oxlint-disable-next-line react/set-state-in-effect
    if (id) void openSession(id)
  }, [openSession])

  return { sessions, sessionId, items, busy, notice, locked, send, stop, newChat, openSession, unlock }
}
