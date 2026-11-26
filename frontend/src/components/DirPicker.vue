<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../lib/api'
import { toastError } from '../lib/toast'
import type { DirEntry } from '../lib/types'

const props = defineProps<{
  accountId: string
  modelValue: string
  placeholder?: string
  allowRoot?: boolean
  label?: string
  sm?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [string]
  pick: [{ id: string; name: string; path: string }]
}>()

interface Crumb {
  id: string
  name: string
}

const open = ref(false)
const loading = ref(false)
const dirs = ref<DirEntry[]>([])
const stack = ref<Crumb[]>([])
const current = ref('0')

async function load(id: string, name: string, push: boolean) {
  if (!props.accountId) {
    toastError(new Error('请先选择 115 账号'))
    return
  }
  loading.value = true
  try {
    const res = await api.get<{ dirs: DirEntry[]; cid: string }>(
      `/dirs?account_id=${encodeURIComponent(props.accountId)}&cid=${encodeURIComponent(id || '0')}`,
    )
    dirs.value = res.dirs || []
    current.value = id || '0'
    if (push) stack.value = [...stack.value, { id: id || '0', name }]
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function show() {
  open.value = true
  stack.value = [{ id: '0', name: '根目录' }]
  await load('0', '根目录', false)
}

function enter(d: DirEntry) {
  load(d.id, d.name, true)
}

function goto(index: number) {
  const target = stack.value[index]
  stack.value = stack.value.slice(0, index + 1)
  load(target.id, target.name, false)
}

function use() {
  const crumb = stack.value[stack.value.length - 1]
  const names = stack.value.slice(1).map((c) => c.name)
  const full = '/' + names.join('/')
  emit('update:modelValue', current.value === '0' ? '0' : current.value)
  emit('pick', { id: current.value, name: crumb?.name || '根目录', path: current.value === '0' ? '/' : full })
  open.value = false
}

/** 直接选中某一层子目录（不用先进去再点「用这个目录」） */
function pickDir(d: DirEntry) {
  const names = stack.value.slice(1).map((c) => c.name)
  names.push(d.name)
  emit('update:modelValue', d.id || '0')
  emit('pick', { id: d.id || '0', name: d.name, path: '/' + names.join('/') })
  open.value = false
}
</script>

<template>
  <button class="btn" :class="{ sm: props.sm }" type="button" @click="show">
    {{ props.label || (props.modelValue && props.modelValue !== '0' ? '换目录' : '选择目录') }}
  </button>

  <div v-if="open" class="modal" @click.self="open = false">
    <div class="box">
      <div class="head">
        <span>选择网盘目录</span>
        <span class="spacer"></span>
        <button class="btn ghost sm" @click="open = false">✕</button>
      </div>
      <div class="body">
        <div class="dir-crumb">
          <a
            v-for="(c, i) in stack"
            :key="c.id + i"
            href="javascript:void(0)"
            @click="goto(i)"
          >{{ c.name }}<span v-if="i < stack.length - 1"> / </span></a>
        </div>
        <div v-if="loading" class="empty">加载中…</div>
        <template v-else>
          <div v-for="d in dirs" :key="d.id" class="dir-row" @click="enter(d)">
            <span class="ico">📁</span>
            <span class="name">{{ d.name }}</span>
            <button class="btn ghost sm" @click.stop="pickDir(d)">选这个</button>
          </div>
          <div v-if="!dirs.length" class="empty">这个目录下没有子目录</div>
        </template>
      </div>
      <div class="foot">
        <span class="dir-current">当前：{{ stack[stack.length - 1]?.name || '根目录' }}</span>
        <button class="btn" @click="open = false">取消</button>
        <button class="btn primary" @click="use">用这个目录</button>
      </div>
    </div>
  </div>
</template>
