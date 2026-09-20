<script setup lang="ts">
import { useMockStore } from '@/stores/mock'
import { useUiStore } from '@/stores/ui'
import TimelineControls from '@/components/TimelineControls.vue'

const mock = useMockStore()
const ui = useUiStore()
</script>

<template>
  <section class="page">
    <h1>演进</h1>
    <TimelineControls />
    <p>状态 {{ mock.evolution.status }}（资料不足时只保留时间轴）。</p>
    <div class="split">
      <ol>
        <li v-for="stage in mock.evolution.stages" :key="stage.title">
          <strong>{{ stage.title }}</strong>
          <p>{{ stage.summary }}</p>
        </li>
      </ol>
      <ul>
        <li v-for="edge in mock.evolution.edges" :key="edge.from_event_id + edge.to_event_id">
          {{ edge.from_event_id }} → {{ edge.to_event_id }} · {{ edge.relation }} · {{ edge.basis }}
          <p>{{ edge.rationale }}</p>
          <button type="button" @click="ui.evidenceOpen = true">边的依据</button>
        </li>
      </ul>
    </div>
  </section>
</template>
