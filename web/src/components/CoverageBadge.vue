<script setup lang="ts">
defineProps<{ label: string; tone?: 'ok' | 'warn' | 'bad' | 'info' | 'late' }>()
</script>

<template>
  <span class="badge" :class="tone === 'warn' ? 'partial' : tone === 'bad' ? 'failed' : tone === 'late' ? 'late' : tone === 'ok' ? 'ok' : 'pending'">
    {{ label }}
  </span>
</template>
