<script setup lang="ts">
import { computed } from 'vue'
import { useMockStore } from '@/stores/mock'
import { useUiStore } from '@/stores/ui'
import TimelineControls from '@/components/TimelineControls.vue'
import EventCard from '@/components/EventCard.vue'

const mock = useMockStore()
const ui = useUiStore()
const known = computed(() => mock.events.filter((e) => e.occurred_time.precision !== 'unknown'))
const unknown = computed(() => mock.events.filter((e) => e.occurred_time.precision === 'unknown'))
</script>

<template>
  <section class="page">
    <h1>时间轴</h1>
    <TimelineControls />
    <h2>已知日期</h2>
    <EventCard
      v-for="event in known"
      :key="event.id"
      :event="event"
      @evidence="ui.evidenceOpen = true"
      @generation="ui.generationOpen = true"
    />
    <h2>日期未知（独立组，可隐藏）</h2>
    <EventCard
      v-for="event in unknown"
      :key="event.id"
      :event="event"
      @evidence="ui.evidenceOpen = true"
      @generation="ui.generationOpen = true"
    />
  </section>
</template>
