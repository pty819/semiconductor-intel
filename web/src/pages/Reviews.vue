<script setup lang="ts">
import { useMockStore } from '@/stores/mock'
import { useSessionStore } from '@/stores/session'

const mock = useMockStore()
const session = useSessionStore()
</script>

<template>
  <section class="page">
    <h1>待处理</h1>
    <div class="split">
      <div>
        <h2>复核提案</h2>
        <article v-for="review in mock.reviews" :key="review.id" class="card">
          <p>{{ review.summary }} · row_version={{ review.row_version }}</p>
          <button type="button" @click="session.submitWrite({ id: review.id, expected_version: review.row_version })">接受</button>
          <button type="button" @click="session.submitWrite({ id: review.id, expected_version: 1 }, false)">过期 409 保留输入</button>
        </article>
      </div>
      <div>
        <h2>失败作业</h2>
        <article v-for="job in mock.jobs.filter((j) => j.state === 'failed')" :key="job.id" class="card">
          <p>{{ job.kind }} · {{ job.error_code }}</p>
          <button type="button" @click="session.submitWrite({ retry: job.id })">重试</button>
        </article>
      </div>
    </div>
  </section>
</template>
