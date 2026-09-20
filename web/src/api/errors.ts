export class ApiError extends Error {
  readonly code: string
  readonly request_id: string
  readonly details: Record<string, unknown> | null
  readonly httpStatus: number

  constructor(
    code: string,
    message: string,
    options?: {
      request_id?: string
      details?: Record<string, unknown> | null
      httpStatus?: number
    },
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.request_id = options?.request_id ?? `req_${Date.now().toString(36)}`
    this.details = options?.details ?? null
    this.httpStatus = options?.httpStatus ?? 400
  }
}
