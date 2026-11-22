<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <template v-if="isRecalc">
              <button
                class="link"
                type="button"
                data-testid="recalc-start"
                :disabled="startingId === Number(row.id)"
                @click="startRecalcTask(Number(row.id))"
              >
                启动重算
              </button>
              <button class="link" type="button" data-testid="recalc-eligibility" @click="inspectEligibility(Number(row.id))">
                启动资格
              </button>
            </template>
            <button
              v-else
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <section v-if="isRecalc" class="recalc-panel" data-testid="recalc-panel">
      <div class="recalc-panel-head">
        <h3>重算资格与窗口差异</h3>
        <button class="btn ghost" type="button" data-testid="recalc-refresh" @click="loadRecalcPanels">刷新</button>
      </div>

      <div v-if="eligibility" class="recalc-card" data-testid="recalc-eligibility-card">
        <p>
          任务 #{{ eligibility.task_id }}：
          <strong :class="eligibility.eligible ? 'ok-text' : 'warn-text'">
            {{ eligibility.eligible ? '具备启动资格' : '暂不可启动' }}
          </strong>
        </p>
        <p class="muted">{{ eligibility.reason }}</p>
        <p class="muted">
          申请 #{{ eligibility.application_id ?? '—' }} · 目标版本 {{ eligibility.target_version }} ·
          已尝试 {{ eligibility.attempt_count }} 次（成功 {{ eligibility.completed_attempt_count }} 次）·
          可用差异 {{ eligibility.available_diff_count }} 笔
        </p>
      </div>
      <p v-else class="muted">点击任意任务的「启动资格」查看该任务能否启动及原因。</p>

      <h4>获准且实际完成的窗口差异（报告消费面）</h4>
      <table v-if="availableDiffs.items.length" class="data-table diff-table">
        <thead>
          <tr>
            <th>任务</th><th>尝试</th><th>指标</th><th>记录日期</th>
            <th>旧值</th><th>新值</th><th>差额</th><th>口径</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="diff in availableDiffs.items" :key="String(diff.id)" :data-testid="`diff-${diff.id}`">
            <td>#{{ diff.task_id }}</td>
            <td>第{{ diff.attempt_no }}次</td>
            <td>{{ diff.指标 }}</td>
            <td>{{ diff.记录日期 }}</td>
            <td>{{ diff.旧值 }}</td>
            <td>{{ diff.新值 }}</td>
            <td>{{ diff.差额 }}</td>
            <td>{{ diff.旧口径 }} → {{ diff.新口径 }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-state" data-testid="diff-empty">{{ availableDiffs.reason || '暂无已完成的重算窗口差异' }}</p>

      <h4>重算历史（按任务与尝试回写，含首次失败）</h4>
      <table v-if="recalcHistory.length" class="data-table diff-table">
        <thead>
          <tr>
            <th>任务</th><th>尝试</th><th>指标</th><th>目标版本</th>
            <th>时间范围</th><th>状态</th><th>原因/差异</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="hist in recalcHistory" :key="String(hist.id)" :data-testid="`history-${hist.id}`">
            <td>#{{ hist.任务ID }}</td>
            <td>第{{ hist.尝试序号 }}次</td>
            <td>{{ hist.关联指标 }}</td>
            <td>{{ hist.新口径 }}</td>
            <td>{{ hist.历史时间范围 }}</td>
            <td :class="hist.状态 === '已完成' ? 'ok-text' : 'warn-text'">{{ hist.状态 }}</td>
            <td>{{ hist.原因 || hist.产出差异 }}</td>
            <td>{{ hist.重算时间 }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-state">暂无重算尝试记录。</p>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  fetchAvailableDiffs,
  fetchRecalcEligibility,
  fetchRecalcHistory,
  listEntries,
  moduleMeta,
  runAction as applyAction,
  startRecalc,
} from '@/api/backend-service'
import type {
  AvailableDiffs,
  RecalcEligibility,
  RecalcHistoryRow,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isRecalc = props.moduleKey === 'recalc_engine'
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const startingId = ref<number | null>(null)
const eligibility = ref<RecalcEligibility | null>(null)
const availableDiffs = ref<AvailableDiffs>({ items: [], total: 0, reason: '' })
const recalcHistory = ref<RecalcHistoryRow[]>([])
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
    if (isRecalc) await loadRecalcPanels()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function inspectEligibility(taskId: number) {
  errorMessage.value = ''
  try {
    eligibility.value = await fetchRecalcEligibility(taskId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '资格查询失败'
  }
}

async function startRecalcTask(taskId: number) {
  errorMessage.value = ''
  startingId.value = taskId
  try {
    const result = await startRecalc(taskId)
    if (result.status === '重算失败') {
      errorMessage.value = result.error || '重算启动失败，已保留失败记录，可在发布后从原任务入口重试'
    } else {
      errorMessage.value = result.idempotent
        ? `任务已完成，未重复签发差异（差异 ${result.diff_count ?? 0} 笔）`
        : (result.message || '重算已完成')
    }
    await reload()
    await inspectEligibility(taskId)
    await loadRecalcPanels()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算启动失败'
  } finally {
    startingId.value = null
  }
}

async function loadRecalcPanels() {
  try {
    const [diffs, history] = await Promise.all([
      fetchAvailableDiffs(),
      fetchRecalcHistory(),
    ])
    availableDiffs.value = diffs
    recalcHistory.value = history
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算面板读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(() => {
  void reload()
  if (isRecalc) void loadRecalcPanels()
})
</script>
