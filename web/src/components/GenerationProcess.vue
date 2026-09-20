<script setup lang="ts">
import { useMockStore } from '@/stores/mock'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const mock = useMockStore()
</script>

<template>
  <aside v-if="open" class="drawer" role="dialog" aria-label="生成过程">
    <header class="drawer-head">
      <h2>生成过程</h2>
      <button type="button" @click="emit('close')">关闭</button>
    </header>
    <p>步骤 {{ mock.generation.step_key }} · 路由 {{ mock.generation.model_route_display }}</p>
    <p>trace_state={{ mock.generation.trace_state }} · viewer_available={{ mock.generation.viewer_available }}</p>
    <p class="hint">普通用户看不到不可用的 NOOA viewer 跳转。过期/失败状态不给死链。</p>
  </aside>
</template>
