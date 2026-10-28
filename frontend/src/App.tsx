// 三栏布局：会话侧栏 | 对话 | 地图。讲解见 docs/10-frontend.md
import { useMemo } from 'react'
import { useChat } from './chat/useChat'
import { AccessGate } from './components/AccessGate'
import { ChatPanel } from './components/ChatPanel'
import { MapPanel } from './components/MapPanel'
import { Sidebar } from './components/Sidebar'
import { extractMapData } from './map/extract'

export default function App() {
  const chat = useChat()
  // 地图数据不单独存状态，而是每次从对话里推导出来：只有一份数据源，
  // 实时对话、刷新恢复、切换会话三种情况下地图自动一致，不会出现「对话换了地图没换」
  const mapData = useMemo(() => extractMapData(chat.items), [chat.items])

  // 后端设了访问码而浏览器还没通过：整个界面换成输入页（讲解见 docs/12-going-public.md）
  if (chat.locked) return <AccessGate onSubmit={chat.unlock} />

  return (
    <div className="app">
      <Sidebar
        sessions={chat.sessions}
        currentId={chat.sessionId}
        onNew={chat.newChat}
        onOpen={chat.openSession}
      />
      <ChatPanel
        items={chat.items}
        busy={chat.busy}
        notice={chat.notice}
        onSend={chat.send}
        onStop={chat.stop}
      />
      <MapPanel data={mapData} />
    </div>
  )
}
