<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const router = useRouter()

function open(id: string) {
  session.setIndustry(id)
  router.push({ name: 'workspace', params: { industryId: id } })
}

function wizard(name: string) {
  const id = session.createFromTemplate(name)
  void session.submitWrite({ wizard: name, id })
  open(id)
}
</script>

<template>
  <main class="page">
    <h1>我的领域</h1>
    <p v-if="!session.industries.length" class="banner info">无领域：从五种模板创建。</p>
    <ul class="cards">
      <li v-for="item in session.industries" :key="item.id">
        <button type="button" class="linkish" @click="open(item.id)">{{ item.name }}</button>
        <span>{{ item.status }}</span>
        <button type="button" @click="session.setStatus(item.id, 'paused')">暂停</button>
        <button type="button" @click="session.setStatus(item.id, 'active')">恢复</button>
        <button type="button" @click="session.setStatus(item.id, 'archived')">归档</button>
      </li>
    </ul>
    <h2>创建向导（五种可编辑模板）</h2>
    <ul>
      <li v-for="tpl in session.templates" :key="tpl.name">
        <strong>{{ tpl.name }}</strong> — {{ tpl.description }}
        <button type="button" @click="wizard(tpl.name)">试用采集并创建</button>
      </li>
    </ul>
  </main>
</template>
