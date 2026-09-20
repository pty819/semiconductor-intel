import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { ClaimView, EvidenceView, ParseSnapshot } from '@/schemas'
import { locateQuoteInParse } from '@/api/locate'

const PARSE_TEXT =
  '合成论文摘要：监督模型根据 OES 时间序列判断介质刻蚀终点，准确率报告为 96%。腔体 RF 匹配网络采用 13.56MHz。材料选择优先考虑氧化铝陶瓷窗。'

export type MockEvent = {
  id: string
  title: string
  summary: string
  event_type: string
  document_count: number
  late_discovery: boolean
  occurred_time: { precision: string }
}

export const useMockStore = defineStore('mock', () => {
  const asOf = ref('2026-09-21T23:59:59+08:00')
  const rangeFrom = ref('2025-01-01')
  const rangeTo = ref('2026-12-31')
  const qaMode = ref<'archive' | 'online'>('archive')
  const ssePhase = ref('空闲')
  const sseConnected = ref(true)

  const topics = [
    { id: 'top-rf', name: 'RF / 射频' },
    { id: 'top-mat', name: '材料选择' },
    { id: 'top-oes', name: 'OES 监控算法' },
  ]

  const events: MockEvent[] = [
    {
      id: 'evt-e500',
      title: '刻蚀机 E-500 正式商用',
      summary: '厂商宣布 E-500 进入 GA，匹配网络 13.56MHz。',
      event_type: 'product_release',
      document_count: 3,
      late_discovery: true,
      occurred_time: { precision: 'month' },
    },
    {
      id: 'evt-unknown',
      title: '某材料窗口传闻（日期未知）',
      summary: '仅摘要来源，发生时间未知。',
      event_type: 'other',
      document_count: 1,
      late_discovery: false,
      occurred_time: { precision: 'unknown' },
    },
  ]

  const evidence = {
    id: 'ev-1',
    claim_revision_id: 'cl-1',
    parse_id: 'parse-1',
    document_id: 'doc-1',
    block_id: 'b001',
    start_char: 7,
    end_char: 41,
    exact_quote: '监督模型根据 OES 时间序列判断介质刻蚀终点，准确率报告为 96%',
    context_before: '合成论文摘要：',
    context_after: '。腔体 RF 匹配网络采用 13.56MHz。',
    locator_status: 'verified' as const,
    semantic_support: 'supported' as const,
    retrieval_scope: 'fulltext' as const,
    source_url: 'https://example.com/oes-paper',
    artifact_url: '/objects/parse-1.html',
    page: null,
  } satisfies EvidenceView

  const claim: ClaimView = {
    revision_id: 'cl-1',
    text: '监督模型根据 OES 时间序列判断介质刻蚀终点，准确率报告为 96%',
    kind: 'source_statement',
    attribution: '合成论文',
    conditions: ['OES 时间序列监督'],
    evidence_ids: ['ev-1'],
  }

  const parse: ParseSnapshot = {
    parse_id: 'parse-1',
    document_id: 'doc-1',
    document_title: 'OES 终点检测论文（快照 v1）',
    revision_label: 'parse-1 · 2026-08-01',
    captured_at: '2026-08-01T00:00:00Z',
    is_latest: false,
    text: PARSE_TEXT,
    blocks: [{ block_id: 'b001', text: PARSE_TEXT, kind: 'paragraph', page: null }],
    media: 'html',
    page: null,
  }

  const latestParse: ParseSnapshot = {
    ...parse,
    parse_id: 'parse-2',
    revision_label: 'parse-2 · 最新正文（禁止用旧引用重搜）',
    is_latest: true,
    text: PARSE_TEXT.replace('96%', '94%（更正）'),
  }

  const documents = [
    { id: 'doc-1', title: 'OES 终点检测论文', retrieval_scope: 'fulltext', parse_status: 'ok' },
    { id: 'doc-2', title: '厂商新闻稿（仅摘要）', retrieval_scope: 'abstract', parse_status: 'partial' },
  ]

  const coverage = { fulltext: 7, total: 12, abstract: 3, failed: 2 }

  const evolution = {
    status: 'ready',
    stages: [
      { title: '光谱终点启发式', summary: '规则阈值' },
      { title: '监督模型', summary: 'OES 时序分类' },
    ],
    edges: [
      {
        from_event_id: 'n1',
        to_event_id: 'n2',
        relation: 'extends',
        basis: 'explicit',
        rationale: '论文引用先前启发式作为基线',
      },
    ],
  }

  const messages = [
    { id: 'msg-u1', role: 'user' as const, text: 'E-500 最新动态？' },
    { id: 'msg-a1', role: 'assistant' as const, text: '正式商用，见引用。' },
  ]

  const reports = [
    { id: 'rep-1', title: 'Etching 日报', stale: true, coverage: 1 },
  ]

  const reviews = [
    { id: 'rv-1', type: 'event_merge', status: 'pending', row_version: 4, summary: '合并重复商用公告' },
  ]

  const jobs = [
    { id: 'job-1', kind: 'archive_answer', state: 'running', error_code: null as string | null },
    { id: 'job-2', kind: 'extract', state: 'failed', error_code: 'parser_unavailable' },
  ]

  const generation = {
    step_key: 'extract_claims',
    trace_state: 'available',
    viewer_available: false,
    model_route_display: 'L2',
  }

  const locate = computed(() => locateQuoteInParse(parse, evidence))

  function startSseMock() {
    sseConnected.value = true
    ssePhase.value = 'retrieving'
    window.setTimeout(() => {
      ssePhase.value = 'extracting'
    }, 400)
    window.setTimeout(() => {
      ssePhase.value = 'completed'
    }, 900)
  }

  function dropSse() {
    sseConnected.value = false
    ssePhase.value = 'SSE 断开，已降级轮询'
  }

  return {
    asOf,
    rangeFrom,
    rangeTo,
    qaMode,
    ssePhase,
    sseConnected,
    topics,
    events,
    evidence,
    claim,
    parse,
    latestParse,
    documents,
    coverage,
    evolution,
    messages,
    reports,
    reviews,
    jobs,
    generation,
    locate,
    startSseMock,
    dropSse,
  }
})
