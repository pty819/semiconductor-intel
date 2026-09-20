import type { TimeValue } from '@/schemas'

const PRECISION_LABEL: Record<TimeValue['precision'], string> = {
  instant: '时刻',
  day: '精确到日',
  month: '精确到月',
  year: '精确到年',
  range: '区间',
  unknown: '日期未知',
}

export function formatTimeValue(value: TimeValue): string {
  const precision = PRECISION_LABEL[value.precision]
  if (value.precision === 'unknown' || !value.start) {
    const original = value.original_text ? `（原文「${value.original_text}」）` : ''
    return `日期未知 · ${precision}${original}`
  }
  const start = formatIso(value.start, value.precision)
  if (value.precision === 'range' && value.end) {
    return `${start} – ${formatIso(value.end, value.precision)} · ${precision}`
  }
  const basis = value.basis === 'inferred' ? ' · 推断' : value.basis === 'unknown' ? ' · 依据不明' : ''
  const original = value.original_text ? ` · 原文「${value.original_text}」` : ''
  return `${start} · ${precision}${basis}${original}`
}

export function formatIso(iso: string, precision: TimeValue['precision'] = 'day'): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const y = d.getUTCFullYear()
  const m = String(d.getUTCMonth() + 1).padStart(2, '0')
  const day = String(d.getUTCDate()).padStart(2, '0')
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  if (precision === 'year') return `${y}年`
  if (precision === 'month') return `${y}年${m}月`
  if (precision === 'instant') return `${y}-${m}-${day} ${hh}:${mm} UTC`
  return `${y}年${m}月${day}日`
}

export function formatClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
}

export function todayIso(): string {
  return '2026-09-20T12:00:00Z'
}
