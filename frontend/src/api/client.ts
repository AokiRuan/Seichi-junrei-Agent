// 调后端的接口。讲解见 docs/10-frontend.md；访问码见 docs/12-going-public.md
// 路径都是相对的 /api/...：开发时由 Vite 代理到 :8000，部署时页面和接口同源，不需要配置后端地址
import { readSSE } from './sse'
import type { ChatEvent, SessionDetail, SessionOut } from './types'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function ensureOk(res: Response): Promise<Response> {
  if (res.ok) return res
  // FastAPI 的错误体是 {"detail": "..."}（422 时 detail 是数组）
  let detail = res.statusText
  try {
    const body = await res.json()
    detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
  } catch {
    // 不是 JSON（比如代理返回的 502 页面），用状态文本
  }
  throw new ApiError(res.status, detail)
}

/** 后端设了访问码、而浏览器还没有访问 cookie（或访问码换了）时，所有 /api 请求都是 401 */
export function needsAccessCode(e: unknown): boolean {
  return e instanceof ApiError && e.status === 401
}

/**
 * 提交访问码。输对了服务端会种一个 HttpOnly 的访问 cookie，之后的请求浏览器自动带上——
 * 前端不保存访问码，也读不到那个 cookie
 */
export async function grantAccess(code: string): Promise<void> {
  await ensureOk(
    await fetch('/api/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code }),
    }),
  )
}

export async function listSessions(): Promise<SessionOut[]> {
  return (await ensureOk(await fetch('/api/sessions'))).json()
}

export async function getSession(id: string): Promise<SessionDetail> {
  return (await ensureOk(await fetch(`/api/sessions/${encodeURIComponent(id)}`))).json()
}

/**
 * 发一条消息，逐个产出 SSE 事件。
 *
 * 4xx（会话不存在、消息为空）在流开始之前就会返回——后端把校验放在了依赖里——
 * 所以这里抛 ApiError；流开始之后的失败则以 error 事件的形式到达。
 */
export async function* streamChat(
  message: string,
  sessionId: string | null,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const res = await ensureOk(
    await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sessionId ? { message, session_id: sessionId } : { message }),
      signal,
    }),
  )
  if (!res.body) throw new ApiError(res.status, '响应没有内容')

  for await (const msg of readSSE(res.body)) {
    yield { type: msg.event, ...JSON.parse(msg.data) } as ChatEvent
  }
}
