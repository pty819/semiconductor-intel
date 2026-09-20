<script setup lang="ts">
import { ref } from 'vue'
import { FEED_OUTCOME_LABEL } from '@/utils/labels'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const feeds = ref([
  { id: 'f1', title: '厂商 RSS', cadence: '每天', running: true, outcome: 'success' as const },
  { id: 'f2', title: '专利 API', cadence: '每周', running: true, outcome: 'no_change' as const },
  { id: 'f3', title: '付费数据库', cadence: '每天', running: false, outcome: 'failed' as const, paywall: true },
])

function poll(id: string) {
  const feed = feeds.value.find((item) => item.id === id)
  if (!feed) return
  void session.submitWrite({ poll: id })
  if (feed.paywall) feed.outcome = 'failed'
}

function toggle(id: string) {
  const feed = feeds.value.find((item) => item.id === id)
  if (feed) feed.running = !feed.running
}
</script>

<template>
  <main class="page">
    <h1>采集来源</h1>
    <p>付费/访问受限单独标明，不伪装成「无文章」。</p>
    <article v-for="feed in feeds" :key="feed.id" class="card">
      <h3>{{ feed.title }}</h3>
      <p class="meta">周期 {{ feed.cadence }} · {{ feed.running ? '运行中' : '已暂停' }}</p>
      <p>
        最近：{{ FEED_OUTCOME_LABEL[feed.outcome] }}
        <span v-if="feed.paywall" class="badge failed">访问受限/付费墙</span>
      </p>
      <button type="button" @click="toggle(feed.id)">启停</button>
      <button type="button" @click="poll(feed.id)">立即 poll</button>
    </article>
  </main>
</template>
