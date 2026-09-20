import type { components } from '@/api/openapi'

export type IndustryView = components['schemas']['IndustryView']
export type IndustryCreate = components['schemas']['IndustryCreate']
export type IndustryPatch = components['schemas']['IndustryPatch']
export type LifecycleCommand = components['schemas']['LifecycleCommand']
export type TopicView = components['schemas']['TopicView']
export type TopicCreate = components['schemas']['TopicCreate']
export type TopicPatch = components['schemas']['TopicPatch']
export type EventCard = components['schemas']['EventCard']
export type Citation = components['schemas']['Citation']
export type EvidenceView = components['schemas']['EvidenceView']
export type TimeValue = components['schemas']['TimeValue']
export type Coverage = components['schemas']['Coverage']
export type GenerationRef = components['schemas']['GenerationRef']
export type GenerationRunView = components['schemas']['GenerationRunView']
export type GenerationViewerLink = components['schemas']['GenerationViewerLink']
export type EvolutionView = components['schemas']['EvolutionView']
export type EvolutionEdge = components['schemas']['EvolutionEdge']
export type EvolutionNode = components['schemas']['EvolutionNode']
export type ConversationView = components['schemas']['ConversationView']
export type ConversationCreate = components['schemas']['ConversationCreate']
export type MessageView = components['schemas']['MessageView']
export type MessageCreate = components['schemas']['MessageCreate']
export type AnswerBlock = components['schemas']['AnswerBlock']
export type ReportView = components['schemas']['ReportView']
export type ReviewView = components['schemas']['ReviewView']
export type ReviewDecision = components['schemas']['ReviewDecision']
export type JobView = components['schemas']['JobView']
export type JobAccepted = components['schemas']['JobAccepted']
export type DocumentView = components['schemas']['DocumentView']
export type DocumentRevisionView = components['schemas']['DocumentRevisionView']
export type DiffView = components['schemas']['DiffView']
export type DocumentTopicCommand = components['schemas']['DocumentTopicCommand']
export type ImportURL = components['schemas']['ImportURL']
export type FeedView = components['schemas']['FeedView']
export type FeedCreate = components['schemas']['FeedCreate']
export type SourceSubscriptionView = components['schemas']['SourceSubscriptionView']
export type SourceTemplate = components['schemas']['SourceTemplate']
export type SourceRunView = components['schemas']['SourceRunView']
export type UserView = components['schemas']['UserView']
export type WatchView = components['schemas']['WatchView']
export type WatchCreate = components['schemas']['WatchCreate']
export type EntityView = components['schemas']['EntityView']
export type SearchHit = components['schemas']['SearchHit']
export type CorrectionCommand = components['schemas']['CorrectionCommand']
export type VersionCommand = components['schemas']['VersionCommand']
export type SourceBlock = components['schemas']['SourceBlock']
export type ClaimProposal = components['schemas']['ClaimProposal']
export type ErrorEnvelope = components['schemas']['ErrorEnvelope']
export type ErrorDetail = components['schemas']['ErrorDetail']

/** Claim as shown in the evidence drawer (not a generation proposal). */
export type ClaimView = {
  revision_id: string
  text: string
  kind: 'source_statement' | 'inference'
  attribution: string | null
  conditions: string[]
  evidence_ids: string[]
}

/** One parse snapshot. Citations must load this by parse_id, never the latest body. */
export type ParseSnapshot = {
  parse_id: string
  document_id: string
  document_title: string
  revision_label: string
  captured_at: string
  is_latest: boolean
  text: string
  blocks: SourceBlock[]
  media: 'html' | 'pdf'
  page: number | null
}

export type LocateResult =
  | {
      status: 'verified'
      parse_id: string
      highlighted: string
      before: string
      after: string
    }
  | {
      status: 'invalid'
      parse_id: string
      reason: string
    }

export type TimelineQuery = {
  from?: string | null
  to?: string | null
  as_of?: string | null
  topic_ids?: string[]
  topic_op?: 'or' | 'and'
  entity_ids?: string[]
  event_type?: string | null
  sort?: 'occurred' | 'discovered'
}

export type JobSseEvent = {
  job_id: string
  state: JobView['state']
  stage: string | null
  done: number
  total: number | null
  documents?: number
  citations_verified?: number
  cursor?: string
}

export type DrawerMode = 'evidence' | 'generation'

export type DrawerLevel = 1 | 2 | 3 | 4

export type Page<T> = {
  items: T[]
  next_cursor: string | null
}

export type TopicInterpretation = components['schemas']['TopicInterpretation']
export type LoginRequest = components['schemas']['LoginRequest']
export type IndustrySettings = components['schemas']['IndustrySettings']
export type Ack = components['schemas']['Ack']
