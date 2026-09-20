<script setup lang="ts">
import { ref } from 'vue'
import { useMockStore } from '@/stores/mock'
import { useUiStore } from '@/stores/ui'
import { useSessionStore } from '@/stores/session'

const mock = useMockStore()
const ui = useUiStore()
const session = useSessionStore()
const question = ref('')
const insufficient = ref(false)

function ask() {
  if (mock.qaMode === 'archive' && !question.value.includes('E-500')) {
    insufficient.value = true
    return
  }
  insufficient.value = false
  mock.startSseMock()
  void session.submitWrite({ question: question.value, mode: mock.qaMode })
}
</script>

<template>
  <section class="page">
    <h1>问答 · 当前领域 {{ $route.params.industryId }}</h1>
    <fieldset>
      <legend>模式</legend>
      <label><input v-model="mock.qaMode" type="radio" value="archive" /> 仅归档资料</label>
      <label><input v-model="mock.qaMode" type="radio" value="online" /> 在线调查（将访问外部公开资料）</label>
    </fieldset>
    <p>阶段 {{ mock.ssePhase }} · {{ mock.sseConnected ? 'SSE 已连接' : '已降级轮询' }}</p>
    <button type="button" @click="mock.dropSse">模拟断线</button>
    <ol>
      <li v-for="msg in mock.messages" :key="msg.id">
        <strong>{{ msg.role }}</strong> {{ msg.text }}
        <button v-if="msg.role === 'assistant'" type="button" @click="ui.evidenceOpen = true">依据</button>
      </li>
    </ol>
    <p v-if="insufficient" class="banner warn">
      归档模式无足够证据。
      <button type="button" @click="mock.qaMode = 'online'">转为在线调查（新消息，保留原回答）</button>
    </p>
    <form @submit.prevent="ask">
      <input v-model="question" placeholder="提问" />
      <button type="submit">发送</button>
      <button type="button">取消</button>
    </form>
  </section>
</template>
