import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useUiStore = defineStore('ui', () => {
  const evidenceOpen = ref(false)
  const generationOpen = ref(false)
  return { evidenceOpen, generationOpen }
})
