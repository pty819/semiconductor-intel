export const INDUSTRY_TEMPLATES = [
  { name: 'Etching', description: '刻蚀设备、工艺与监控研究' },
  { name: 'PVD', description: '物理气相沉积设备、薄膜与均匀性' },
  { name: '陶瓷材料', description: '陶瓷基材料、涂层与耐蚀性' },
  { name: '智能化算法', description: '工艺控制、终点检测与监控算法' },
  { name: '电子显微镜', description: '电镜表征、缺陷分析与制样' },
] as const

export const CONFLICT_LABEL: Record<string, string> = {
  none_known: '无已知争议',
  disputed: '待核实',
  unknown: '争议状态未知',
}

export const SUPPORT_LABEL: Record<string, string> = {
  supported: '来源陈述',
  uncertain: '独立验证未知',
  rejected: '存在反驳',
}

export const SCOPE_LABEL: Record<string, string> = {
  metadata: '仅元数据',
  abstract: '仅摘要',
  partial: '部分读取',
  fulltext: '全文',
}

export const PARSE_LABEL: Record<string, string> = {
  ok: '解析完成',
  partial: '部分解析',
  failed: '解析失败',
  pending: '解析中',
}

export const JOB_STATE_LABEL: Record<string, string> = {
  queued: '排队',
  running: '运行中',
  retry_wait: '等待重试',
  waiting_review: '待复核',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
}

export const REVIEW_STATUS_LABEL: Record<string, string> = {
  pending: '待处理',
  accepted: '已接受',
  rejected: '已拒绝',
  obsolete: '已过期',
  applied: '已应用',
  failed: '失败',
}

export const EVO_STATUS_LABEL: Record<string, string> = {
  ready: '可用',
  insufficient_evidence: '证据不足',
  building: '生成中',
  failed: '生成失败',
}

export const TRACE_LABEL: Record<string, string> = {
  recording: '记录中',
  pending: '待导出',
  available: '可查看摘要',
  missing: '缺失',
  expired: '已过期',
  redacted: '已脱敏',
}

export const FEED_OUTCOME_LABEL: Record<string, string> = {
  success: '有更新',
  no_change: '无更新',
  partial: '部分成功',
  failed: '采集失败',
}
