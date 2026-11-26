import { reactive } from 'vue'

export interface ToastItem {
  id: number
  text: string
  kind: 'ok' | 'err'
}

let seq = 1

export const toasts = reactive<ToastItem[]>([])

export function toast(text: string) {
  push(text, 'ok')
}

export function toastError(err: unknown) {
  push(err instanceof Error ? err.message : String(err), 'err')
}

function push(text: string, kind: 'ok' | 'err') {
  const item: ToastItem = { id: seq++, text, kind }
  toasts.push(item)
  window.setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === item.id)
    if (i >= 0) toasts.splice(i, 1)
  }, kind === 'err' ? 5200 : 2600)
}
