<script setup lang="ts">
import { ref } from 'vue'
import { useMockStore } from '@/stores/mock'
import { useSessionStore } from '@/stores/session'
import CoverageBadge from '@/components/CoverageBadge.vue'

const mock = useMockStore()
const session = useSessionStore()
const draft = ref('https://example.com/import')
</script>

<template>
  <section>
    <h1>资料</h1>
    <ul>
      <li v-for="doc in mock.documents" :key="doc.id">
        {{ doc.title }}
        <CoverageBadge :label="String(doc.retrieval_scope)" :tone="doc.retrieval_scope === 'fulltext' ? 'ok' : 'warn'" />
        <CoverageBadge :label="String(doc.parse_status)" />
      </li>
    </ul>
    <form @submit.prevent="session.submitWrite({ url: draft })">
      <label>URL 导入 <input v-model="draft" /></label>
      <button type="submit" :disabled="session.writePending">导入（等服务器）</button>
      <button type="button" @click="session.submitWrite({ url: draft }, false)">模拟版本冲突</button>
    </form>
    <p v-if="session.lastDraft">保留输入：{{ JSON.stringify(session.lastDraft) }}</p>
  </section>
</template>
