<script setup lang="ts">
import { useMockStore } from '@/stores/mock'
import CoverageBadge from '@/components/CoverageBadge.vue'
const mock = useMockStore()
</script>

<template>
  <section>
    <h1>主题工作台</h1>
    <p>优先试运行：RF / 材料选择 / OES。新主题回填进度，不能永久空白。</p>
    <ul>
      <li v-for="topic in mock.topics" :key="topic.id">
        {{ topic.name }}
        <CoverageBadge label="回填 60%" tone="info" />
      </li>
    </ul>
  </section>
</template>
