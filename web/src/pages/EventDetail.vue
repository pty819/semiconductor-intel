<script setup lang="ts">
import { useRoute } from 'vue-router'
import { useMockStore } from '@/stores/mock'
import { useUiStore } from '@/stores/ui'
import EventCard from '@/components/EventCard.vue'

const route = useRoute()
const mock = useMockStore()
const ui = useUiStore()
const event = mock.events.find((item) => item.id === route.params.eventId) ?? mock.events[0]
</script>

<template>
  <section>
    <h1>事件详情</h1>
    <EventCard :event="event" @evidence="ui.evidenceOpen = true" @generation="ui.generationOpen = true" />
    <p>旧 ID 合并提示：无。历史 revision 仍可打开。</p>
  </section>
</template>
