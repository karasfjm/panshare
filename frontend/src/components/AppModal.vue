<script setup lang="ts">
defineProps<{ title: string; wide?: boolean }>()
const emit = defineEmits<{ close: [] }>()
</script>

<template>
  <div class="modal" @click.self="emit('close')">
    <div class="box" :class="{ wide }">
      <div class="head">
        <span>{{ title }}</span>
        <span class="spacer"></span>
        <button class="btn ghost sm" @click="emit('close')">✕</button>
      </div>
      <div class="body">
        <slot />
      </div>
      <div class="foot">
        <slot name="foot">
          <button class="btn" @click="emit('close')">关闭</button>
        </slot>
      </div>
    </div>
  </div>
</template>
