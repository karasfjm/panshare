import { api } from './api'
import type { Settings } from './types'

export async function loadSettings(): Promise<Settings> {
  const res = await api.get<{ settings: Settings }>('/settings')
  return res.settings
}

/** 后端只接受整份 Settings，这里读出来合并后整体保存，避免把别的开关覆盖成默认值。 */
export async function patchSettings(patch: Partial<Settings>): Promise<Settings> {
  const current = await loadSettings()
  const next = { ...current, ...patch }
  await api.post('/settings', next)
  return next
}
