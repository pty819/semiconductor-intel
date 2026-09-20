import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

const INDUSTRIES = [
  { id: 'ind-etch', name: 'Etching', status: 'active' as const },
  { id: 'ind-pvd', name: 'PVD', status: 'draft' as const },
  { id: 'ind-ceramic', name: '陶瓷材料', status: 'paused' as const },
  { id: 'ind-algo', name: '智能化算法', status: 'draft' as const },
  { id: 'ind-em', name: '电子显微镜', status: 'draft' as const },
]

export const useSessionStore = defineStore('session', () => {
  const user = ref<{ login: string } | null>({ login: 'liyifan' })
  const industryId = ref('ind-etch')
  const sessionGeneration = ref(1)
  const writePending = ref(false)
  const writeError = ref<string | null>(null)
  const lastDraft = ref<Record<string, unknown> | null>(null)

  const currentIndustry = computed(
    () => INDUSTRIES.find((item) => item.id === industryId.value) ?? INDUSTRIES[0],
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

  /** Writes wait for server confirmation; version conflicts keep the draft. */
  async function submitWrite(draft: Record<string, unknown>, succeed = true) {
    lastDraft.value = { ...draft }
    writePending.value = true
    writeError.value = null
    await new Promise((r) => setTimeout(r, 280))
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
    industries: INDUSTRIES,
    currentIndustry,
    login,
    logout,
    setIndustry,
    submitWrite,
  }
})
