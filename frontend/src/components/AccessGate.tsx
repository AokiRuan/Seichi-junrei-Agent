// 输入访问码的页面。讲解见 docs/12-going-public.md
import { type FormEvent, useState } from 'react'
import { needsAccessCode } from '../api/client'

interface Props {
  onSubmit: (code: string) => Promise<void>
}

export function AccessGate({ onSubmit }: Props) {
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const value = code.trim()
    if (!value || pending) return
    setPending(true)
    setError(null)
    try {
      await onSubmit(value) // 成功后父组件不再渲染本页
    } catch (err) {
      setError(needsAccessCode(err) ? '访问码不对' : `出错了：${String(err)}`)
      setPending(false)
    }
  }

  return (
    <div className="gate">
      <form onSubmit={submit}>
        <h1>聖地巡礼</h1>
        <p className="muted small">这是一个演示站点，需要访问码才能使用。</p>
        <input
          type="password"
          autoComplete="current-password"
          placeholder="访问码"
          aria-label="访问码"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          autoFocus
        />
        {error && <div className="gate-error">{error}</div>}
        <button type="submit" disabled={pending || !code.trim()}>
          {pending ? '验证中…' : '进入'}
        </button>
      </form>
    </div>
  )
}
