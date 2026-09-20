import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { INDUSTRY_TEMPLATES } from '@/utils/labels'

export type IndustryRow = {
  id: string
  name: string
  status: 'active' | 'draft' | 'paused' | 'archived'
}

export const useSessionStore = defineStore('session', () => {
  const user = ref<{ login: string } | null>({ login: 'liyifan' })
  const industryId = ref('ind-etch')
  const sessionGeneration = ref(1)
  const writePending = ref(false)
  const writeError = ref<string | null>(null)
  const lastDraft = ref<Record<string, unknown> | null>(null)
  const industries = ref<IndustryRow[]>([
    { id: 'ind-etch', name: 'Etching', status: 'active' },
    { id: 'ind-pvd', name: 'PVD', status: 'draft' },
    { id: 'ind-ceramic', name: '陶瓷材料', status: 'paused' },
    { id: 'ind-algo', name: '智能化算法', status: 'draft' },
    { id: 'ind-em', name: '电子显微镜', status: 'draft' },
  ])

  const currentIndustry = computed(
    () => industries.value.find((item) => item.id === industryId.value) ?? industries.value[0],
  )

  function login(loginName: string) {
    user.value = { login: loginName }
    sessionGeneration.value += 1
  }

  function logout() {
    user.value = null
    sessionGeneration.value += 1
    writePending.value = false
    writeError.value = null
    lastDraft.value = null
  }

  function setIndustry(id: string) {
    industryId.value = id
  }

  function createFromTemplate(name: string) {
    const id = `ind-${Date.now()}`
    industries.value.push({ id, name, status: 'draft' })
    lastDraft.value = { created: name, id }
    return id
  }

  function setStatus(id: string, status: IndustryRow['status']) {
    const row = industries.value.find((item) => item.id === id)
    if (row) row.status = status
  }

  async function submitWrite(draft: Record<string, unknown>, succeed = true) {
    lastDraft.value = { ...draft }
    writePending.value = true
    writeError.value = null
    await new Promise((r) => setTimeout(r, 180))
    writePending.value = false
    if (!succeed) {
      writeError.value = 'version_conflict：服务器版本已变，输入已保留供比较'
      return false
    }
    return true
  }

  return {
    user,
    industryId,
    sessionGeneration,
    writePending,
    writeError,
    lastDraft,
    industries,
    templates: INDUSTRY_TEMPLATES,
    currentIndustry,
    login,
    logout,
    setIndustry,
    createFromTemplate,
    setStatus,
    submitWrite,
  }
})
