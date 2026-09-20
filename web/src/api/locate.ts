import type { EvidenceView, LocateResult, ParseSnapshot } from '@/schemas'

/**
 * Locate a citation in the parse snapshot named by evidence.parse_id.
 * Never search a different (newer) body. Never fuzzy-match nearby sentences.
 */
export function locateQuoteInParse(
  parse: ParseSnapshot,
  evidence: EvidenceView,
): LocateResult {
  if (parse.parse_id !== evidence.parse_id) {
    throw new Error('parse_id mismatch: refuse to search a different snapshot')
  }
  if (evidence.locator_status === 'invalid') {
    return {
      status: 'invalid',
      parse_id: parse.parse_id,
      reason: '引用失效/待修正',
    }
  }
  const slice = parse.text.slice(evidence.start_char, evidence.end_char)
  if (slice !== evidence.exact_quote) {
    return {
      status: 'invalid',
      parse_id: parse.parse_id,
      reason: '引用失效/待修正：该 parse 中无法按字符区间定位原文，未在最新正文中重搜',
    }
  }
  return {
    status: 'verified',
    parse_id: parse.parse_id,
    highlighted: slice,
    before: parse.text.slice(Math.max(0, evidence.start_char - 120), evidence.start_char),
    after: parse.text.slice(evidence.end_char, evidence.end_char + 120),
  }
}
