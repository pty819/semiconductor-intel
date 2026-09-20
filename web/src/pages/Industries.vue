<script setup lang="ts">
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
</script>

<template>
  <main class="page">
    <h1>我的领域</h1>
    <p>无其他用户列表。全局仅采集来源 / 运行中心 / 账号设置。</p>
    <ul class="cards">
      <li v-for="item in session.industries" :key="item.id">
        <router-link :to="{ name: 'workspace', params: { industryId: item.id } }" @click="session.setIndustry(item.id)">
          {{ item.name }}
        </router-link>
        <span>{{ item.status }}</span>
      </li>
    </ul>
    <p v-if="!session.industries.length">无领域时提供五种可编辑模板（Etching / PVD / 陶瓷 / 算法 / 电镜）。</p>
  </main>
</template>
