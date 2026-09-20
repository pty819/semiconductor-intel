<script setup lang="ts">
import { useMockStore } from '@/stores/mock'
import { useUiStore } from '@/stores/ui'
import CoverageBadge from '@/components/CoverageBadge.vue'

const mock = useMockStore()
const ui = useUiStore()
</script>

<template>
  <section class="page">
    <h1>报告</h1>
    <article v-for="report in mock.reports" :key="report.id" class="card">
      <h2>{{ report.title }}</h2>
      <p v-if="report.stale" class="banner warn">旧报告过时 banner，不覆盖历史版本。</p>
      <CoverageBadge :label="`引用覆盖 ${report.coverage}`" tone="ok" />
      <button type="button" @click="ui.evidenceOpen = true">依据</button>
    </article>
  </section>
</template>
