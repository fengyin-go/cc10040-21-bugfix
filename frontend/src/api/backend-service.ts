import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 口径重算引擎专用链路（登记 -> 资格 -> 启动 -> 差异/历史） ──

export type RecalcEligibility = {
  task_id: number
  application_id?: number
  metric: string
  target_version: string
  time_range?: string
  application_status?: string
  task_status?: string
  attempt_count: number
  completed_attempt_count: number
  eligible: boolean
  reason: string
  available_diff_count: number
}

export type RecalcDetail = {
  id: number
  task_id: number
  attempt_id: number
  attempt_no: number
  application_id: number
  指标: string
  记录日期: string
  旧值: number
  新值: number
  差额: number
  旧口径: string
  新口径: string
  在窗内: boolean
  已完成: boolean
}

export type RecalcStartResult = {
  task_id: number
  application_id?: number
  attempt_id?: number
  attempt_no?: number
  metric?: string
  status: string
  message?: string
  error?: string
  idempotent?: boolean
  diff_count?: number
  diff_total?: number
  details?: RecalcDetail[]
}

export type AvailableDiffs = {
  items: RecalcDetail[]
  total: number
  diff_total?: number
  reason: string
}

export type RecalcHistoryRow = {
  id: number
  任务ID: number
  尝试序号: number
  申请ID: number
  关联指标: string
  旧口径: string
  新口径: string
  历史时间范围: string
  状态: string
  原因: string
  产出差异: string
  差异条数: number
  重算时间: string
}

async function getJson(path: string): Promise<unknown> {
  const response = await request(path)
  if (!response.ok) throw new Error('重算链路读取失败')
  return await response.json()
}

export function fetchRecalcEligibility(taskId: number): Promise<RecalcEligibility> {
  return getJson(`/api/cross/recalc/${taskId}/eligibility`) as Promise<RecalcEligibility>
}

export function startRecalc(taskId: number): Promise<RecalcStartResult> {
  return request(`/api/cross/recalc/${taskId}/start`, { method: 'POST' }).then((r) => r.json())
}

export function fetchAvailableDiffs(metricName?: string): Promise<AvailableDiffs> {
  const query = new URLSearchParams()
  if (metricName) query.set('metric_name', metricName)
  return getJson(`/api/cross/recalc/available-diffs?${query.toString()}`) as Promise<AvailableDiffs>
}

export function fetchRecalcHistory(metricName?: string): Promise<RecalcHistoryRow[]> {
  const query = new URLSearchParams()
  if (metricName) query.set('metric_name', metricName)
  return getJson(`/api/cross/recalc/history?${query.toString()}`) as Promise<RecalcHistoryRow[]>
}
