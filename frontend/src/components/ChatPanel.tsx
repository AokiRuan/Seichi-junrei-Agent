// 左侧对话区：消息列表 + 输入框。讲解见 docs/10-frontend.md
import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { guideOf } from '../chat/guide'
import type { AssistantItem, ChatItem } from '../chat/model'
import { GuideCard } from './GuideCard'
import { ToolStep } from './ToolStep'

// 对应计划里的三个核心场景（docs/00-project-plan.md 第二节）
const EXAMPLES = [
  '《莉可丽丝》的圣地在哪？',
  '这周末去镰仓巡礼，天气怎么样？',
  '我周六上午 10 点从新宿出发，有 4 个小时，想去孤独摇滚的圣地巡礼',
]

function AssistantMessage({ item }: { item: AssistantItem }) {
  const empty = item.parts.length === 0
  return (
    <div className="msg msg-assistant">
      {item.parts.map((p, i) =>
        p.kind === 'text' ? (
          <div className="msg-text" key={i}>
            {/* remark-gfm：表格、删除线等 GitHub 扩展语法。LLM 很爱输出表格，不加的话显示成一堆竖线 */}
            <Markdown remarkPlugins={[remarkGfm]}>{p.text}</Markdown>
          </div>
        ) : (
          <div key={p.id}>
            <ToolStep part={p} />
            {/* 规划结果渲染成图文攻略卡片：数据来自工具，LLM 接着写导语和看点 */}
            {(() => {
              const guide = guideOf(p)
              return guide && <GuideCard guide={guide} />
            })()}
          </div>
        ),
      )}
      {item.status === 'streaming' && empty && <div className="thinking">思考中…</div>}
      {item.status === 'error' && <div className="msg-error">出错了：{item.error}</div>}
      {item.status === 'stopped' && <div className="muted">已停止</div>}
      {item.status === 'done' && item.usage?.total_tokens != null && (
        <div className="msg-meta">
          {item.iterations} 轮 · {item.usage.total_tokens.toLocaleString()} tokens
        </div>
      )}
    </div>
  )
}

interface Props {
  items: ChatItem[]
  busy: boolean
  notice: string | null
  onSend: (text: string) => void
  onStop: () => void
}

export function ChatPanel({ items, busy, notice, onSend, onStop }: Props) {
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true) // 用户是否停在底部

  // 新内容到达时自动滚到底——但只在用户本来就在底部时。
  // 用户往上翻看历史时还强行拉回底部，体验很差
  useEffect(() => {
    const el = listRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [items])

  const onScroll = () => {
    const el = listRef.current
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  const submit = (text: string) => {
    if (!text.trim() || busy) return
    stickRef.current = true
    onSend(text)
    setDraft('')
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    submit(draft)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送，Shift+Enter 换行。isComposing：中文 / 日文输入法选词时按的 Enter 不算发送
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit(draft)
    }
  }

  return (
    <section className="chat-panel">
      <div className="messages" ref={listRef} onScroll={onScroll}>
        {notice && <div className="notice">{notice}</div>}
        {items.length === 0 ? (
          <div className="empty">
            <h2>圣地巡礼助手</h2>
            <p className="muted">查动画取景地、看当地天气、排巡礼路线</p>
            <div className="examples">
              {EXAMPLES.map((q) => (
                <button type="button" key={q} onClick={() => submit(q)} disabled={busy}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          items.map((it) =>
            it.role === 'user' ? (
              <div className="msg msg-user" key={it.id}>
                {it.text}
              </div>
            ) : (
              <AssistantMessage item={it} key={it.id} />
            ),
          )
        )}
      </div>

      <form className="composer" onSubmit={onSubmit}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="问问圣地、天气或路线…（Enter 发送，Shift+Enter 换行）"
          rows={2}
          maxLength={2000}
        />
        {busy ? (
          <button type="button" className="btn-stop" onClick={onStop}>
            停止
          </button>
        ) : (
          <button type="submit" disabled={!draft.trim()}>
            发送
          </button>
        )}
      </form>
    </section>
  )
}
