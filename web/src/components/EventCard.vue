<script setup lang="ts">
import type { MockEvent } from '@/stores/mock'
import CoverageBadge from '@/components/CoverageBadge.vue'

defineProps<{ event: MockEvent }>()
const emit = defineEmits<{ evidence: []; generation: [] }>()
</script>

<template>
  <article class="card">
    <header>
      <h3>{{ event.title }}</h3>
      <CoverageBadge v-if="event.late_discovery" label="新收录历史事件" tone="late" />
      <CoverageBadge :label="event.occurred_time.precision === 'unknown' ? '日期未知' : event.occurred_time.precision" />
    </header>
    <p>{{ event.summary }}</p>
    <p class="meta">发生精度以文字标明，不只靠颜色。类型 {{ event.event_type }} · 文档 {{ event.document_count }}</p>
    <footer>
      <button type="button" @click="emit('evidence')">依据</button>
      <button type="button" @click="emit('generation')">生成过程</button>
      <router-link :to="{ name: 'event', params: { industryId: $route.params.industryId, eventId: event.id } }">详情</router-link>
    </footer>
  </article>
</template>
