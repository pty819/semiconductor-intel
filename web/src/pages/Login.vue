<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'

const loginName = ref('liyifan')
const session = useSessionStore()
const router = useRouter()

function submit() {
  session.login(loginName.value)
  router.push({ name: 'industries' })
}
</script>

<template>
  <main class="page narrow">
    <h1>登录</h1>
    <form @submit.prevent="submit">
      <label>账号 <input v-model="loginName" required /></label>
      <button type="submit">进入</button>
    </form>
  </main>
</template>
