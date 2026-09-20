<script setup lang="ts">
import { useSessionStore } from '@/stores/session'
const session = useSessionStore()
</script>

<template>
  <section class="page">
    <h1>设置</h1>
    <p>账号 {{ session.user?.login }} · session generation {{ session.sessionGeneration }}</p>
    <p>退出登录会清空客户端缓存（Query key 含 session generation / industry / as_of）。</p>
  </section>
</template>
