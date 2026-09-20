<script setup lang="ts">
import { useMockStore } from '@/stores/mock'
import { useSessionStore } from '@/stores/session'

const mock = useMockStore()
const session = useSessionStore()
</script>

<template>
  <main class="page">
    <h1>运行中心</h1>
    <p>{{ mock.sseConnected ? 'SSE 进度' : 'SSE 断开，轮询降级，任务仍保留' }} · {{ mock.ssePhase }}</p>
    <ul>
      <li v-for="job in mock.jobs" :key="job.id">
        {{ job.kind }} · {{ job.state }}
        <button type="button" @click="session.submitWrite({ cancel: job.id })">取消</button>
      </li>
    </ul>
  </main>
</template>
