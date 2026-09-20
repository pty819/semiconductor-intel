<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { useUiStore } from '@/stores/ui'
import EvidenceDrawer from '@/components/EvidenceDrawer.vue'
import GenerationProcess from '@/components/GenerationProcess.vue'

const session = useSessionStore()
const ui = useUiStore()
const route = useRoute()
const industryId = computed(() => String(route.params.industryId || session.industryId))

const nav = [
  { name: 'workspace', label: '概览' },
  { name: 'documents', label: '资料' },
  { name: 'timeline', label: '时间轴' },
  { name: 'topics', label: '主题' },
  { name: 'evolution', label: '演进' },
  { name: 'qa', label: '问答' },
  { name: 'reports', label: '报告' },
  { name: 'reviews', label: '待处理' },
  { name: 'industry-settings', label: '设置' },
]
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="plate">
        <span class="kicker">当前领域</span>
        <span class="name">{{ session.currentIndustry.name }}</span>
      </div>
      <nav class="top-actions">
        <router-link to="/sources">采集来源</router-link>
        <router-link to="/runs">运行中心</router-link>
        <router-link to="/account">账号设置</router-link>
        <button type="button" class="linkish" @click="session.logout(); $router.push('/login')">退出</button>
      </nav>
    </header>
    <aside class="bay">
      <h2>领域内</h2>
      <p class="slot-meta" style="padding:0 14px">无跨领域知识搜索</p>
      <nav class="bay-list">
        <router-link
          v-for="item in nav"
          :key="item.name"
          :to="{ name: item.name, params: { industryId } }"
        >
          <span class="slot-dot active" />
          {{ item.label }}
        </router-link>
      </nav>
    </aside>
    <main class="stage">
      <p v-if="session.writePending" class="banner info">写入等待服务器确认，未做乐观成功。</p>
      <p v-if="session.writeError" class="banner warn">{{ session.writeError }}</p>
      <router-view />
    </main>
    <EvidenceDrawer :open="ui.evidenceOpen" @close="ui.evidenceOpen = false" />
    <GenerationProcess :open="ui.generationOpen" @close="ui.generationOpen = false" />
  </div>
</template>
