<script setup lang="ts">
// 本地 STRM 目录选择器：只在映射根下面逛，选完回填「相对映射根」的路径。
import { ref } from 'vue'
import { api } from '../lib/api'
import { toastError } from '../lib/toast'

const props = defineProps<{
  modelValue: string
  sm?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [string]
}>()

interface Entry {
  name: string
  rel: string
  abs: string
}

const open = ref(false)
const loading = ref(false)
const dirs = ref<Entry[]>([])
const rel = ref('')
const abs = ref('')
const root = ref('')
const parent = ref('')

async function load(path: string) {
  loading.value = true
  try {
    const res = await api.get<{
      root: string
      rel: string
      abs: string
      parent: string
      dirs: Entry[]
    }>(`/locals?path=${encodeURIComponent(path)}`)
    root.value = res.root
    rel.value = res.rel
    abs.value = res.abs
    parent.value = res.parent
    dirs.value = res.dirs || []
  } catch (e) {
    toastError(e)
  } finally {
    loading.value = false
  }
}

async function show() {
  open.value = true
  // 从当前值开始逛；值不合法就退回映射根
  await load(props.modelValue || '')
}

function enter(d: Entry) {
  load(d.rel)
}

function up() {
  load(parent.value)
}

function use() {
  // 回填「映射根的绝对路径」形式（/strm/panshare-go/LTX/VR），
  // 和页面上展示的一致；保存时后端会再归一化成相对路径
  const base = root.value.replace(/\/+$/, '')
  emit('update:modelValue', rel.value ? `${base}/${rel.value}` : base)
  open.value = false
}
</script>

<template>
  <button class="btn" :class="{ sm: props.sm }" type="button" @click="show">选</button>

  <div v-if="open" class="modal" @click.self="open = false">
    <div class="box">
      <div class="head">
        <span>选择本地目录</span>
        <span class="spacer"></span>
        <button class="btn ghost sm" @click="open = false">✕</button>
      </div>
      <div class="body">
        <div class="dir-crumb">
          <a href="javascript:void(0)" @click="load('')">{{ root }}</a>
          <span v-if="rel"> / {{ rel }}</span>
        </div>
        <div class="muted small mono" style="margin: 6px 0 10px">{{ abs }}</div>
        <div v-if="loading" class="empty">加载中…</div>
        <template v-else>
          <div v-if="rel" class="dir-row" @click="up()"><span class="ico">↩</span><span>上一级</span></div>
          <div v-for="d in dirs" :key="d.rel" class="dir-row" @click="enter(d)">
            <span class="ico">📁</span>
            <span>{{ d.name }}</span>
          </div>
          <div v-if="!dirs.length" class="empty">这个目录下没有子目录</div>
        </template>
      </div>
      <div class="foot">
        <button class="btn" @click="open = false">取消</button>
        <button class="btn primary" @click="use">用这个目录</button>
      </div>
    </div>
  </div>
</template>
