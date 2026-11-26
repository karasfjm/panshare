const base = '/api'

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {}
  if (init.body && typeof init.body === 'string') headers['Content-Type'] = 'application/json'
  const res = await fetch(base + path, {
    credentials: 'same-origin',
    ...init,
    headers: { ...headers, ...(init.headers as Record<string, string> | undefined) },
  })
  const text = await res.text()
  let data: any = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    const msg = (data && data.error) || `请求失败（HTTP ${res.status}）`
    throw new Error(msg)
  }
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

export interface MeInfo {
  logged_in: boolean
  username: string
  site_name: string
  no_auth: boolean
  version: string
}

let meCache: MeInfo | null = null

export async function fetchMe(force = false): Promise<MeInfo> {
  if (meCache && !force) return meCache
  meCache = await api.get<MeInfo>('/me')
  return meCache
}

export function clearMe() {
  meCache = null
}

export function fmtTime(ts?: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function fmtSize(n?: number): string {
  if (!n || n <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

export function ago(ts?: number): string {
  if (!ts) return '从未'
  const s = Math.max(1, Math.floor(Date.now() / 1000 - ts))
  if (s < 60) return `${s} 秒前`
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return `${Math.floor(s / 86400)} 天前`
}
