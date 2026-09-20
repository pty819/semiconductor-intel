<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMockStore } from '@/stores/mock'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const mock = useMockStore()
const layer = ref<1 | 2 | 3 | 4>(1)

watch(
  () => props.open,
  (open) => {
    if (open) layer.value = 1
  },
)

const locate = computed(() => mock.locate)
</script>

<template>
  <aside v-if="open" class="drawer" role="dialog" aria-label="依据抽屉" tabindex="-1">
    <header>
      <h2>依据</h2>
      <button type="button" @click="emit('close')">关闭</button>
    </header>
    <ol class="layers">
      <li :class="{ on: layer === 1 }"><button type="button" @click="layer = 1">1 引用</button></li>
      <li :class="{ on: layer === 2 }"><button type="button" @click="layer = 2">2 claim 与条件</button></li>
      <li :class="{ on: layer === 3 }"><button type="button" @click="layer = 3">3 原文高亮（parse_id）</button></li>
      <li :class="{ on: layer === 4 }"><button type="button" @click="layer = 4">4 快照版本</button></li>
    </ol>
    <section v-if="layer === 1">
      <p>引用 cit-1 → evidence {{ mock.evidence.id }}</p>
      <p>来源陈述 · 独立验证未知 · 存在反驳：否 · 覆盖 {{ mock.evidence.retrieval_scope }}</p>
    </section>
    <section v-else-if="layer === 2">
      <blockquote>{{ mock.claim.text }}</blockquote>
      <p>限定条件：{{ mock.claim.conditions.join('；') }}</p>
      <p>归因：{{ mock.claim.attribution }}（{{ mock.claim.kind }}）</p>
    </section>
    <section v-else-if="layer === 3">
      <p>定位 parse {{ mock.parse.parse_id }}，禁止在最新正文重搜。</p>
      <p v-if="locate.status === 'verified'">
        <span>{{ locate.before }}</span>
        <mark>{{ locate.highlighted }}</mark>
        <span>{{ locate.after }}</span>
      </p>
      <p v-else class="warn">{{ locate.reason }}</p>
    </section>
    <section v-else>
      <p>快照 {{ mock.parse.revision_label }} · 非最新</p>
      <p>最新正文是 {{ mock.latestParse.revision_label }}，旧引用不得拿到那里模糊匹配。</p>
      <p>原始 URL：{{ mock.evidence.source_url }} · HTML 快照仅清洗文本视图。</p>
    </section>
  </aside>
</template>
