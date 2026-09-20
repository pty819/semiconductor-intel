<script setup lang="ts">
import { useMockStore } from '@/stores/mock'
import { useUiStore } from '@/stores/ui'
import EventCard from '@/components/EventCard.vue'
import CoverageBadge from '@/components/CoverageBadge.vue'

const mock = useMockStore()
const ui = useUiStore()
</script>

<template>
  <section class="page">
    <h1>概览</h1>
    <p class="banner warn">分析积压 · 资料部分覆盖（abstract）单独 banner</p>
    <CoverageBadge :label="`全文 ${mock.coverage.fulltext}/${mock.coverage.total}`" tone="ok" />
    <EventCard
      v-for="event in mock.events"
      :key="event.id"
      :event="event"
      @evidence="ui.evidenceOpen = true"
      @generation="ui.generationOpen = true"
    />
  </section>
</template>
