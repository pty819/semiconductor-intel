<script setup lang="ts">
import { useMockStore } from '@/stores/mock'

const mock = useMockStore()
const emit = defineEmits<{ change: [] }>()
</script>

<template>
  <form class="controls" @submit.prevent="emit('change')">
    <fieldset>
      <legend>时间范围（选取事件）</legend>
      <label>从 <input v-model="mock.rangeFrom" type="date" /></label>
      <label>到 <input v-model="mock.rangeTo" type="date" /></label>
    </fieldset>
    <fieldset>
      <legend>as_of（当时可知版本，与范围分开）</legend>
      <label>截止 <input v-model="mock.asOf" type="datetime-local" /></label>
    </fieldset>
    <p class="hint">范围决定选哪些事件；as_of 决定当时可见的 revision。二者不可合并。</p>
  </form>
</template>
