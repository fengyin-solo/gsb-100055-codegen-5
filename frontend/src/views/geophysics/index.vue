<template>
  <section class="page" data-module="geophysics">
    <header class="page-head">
      <div>
        <h2>地球物理管理</h2>
        <p class="page-desc">
          物探测线沿待施测 → 施测中 → 已采集 → 数据处理 → 已归档单向推进；跳级、回退一律拦下。
          每次推进按同一修订号同步到勘探区采集台账、成果图清单与待处理列表。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记物探测线</button>
        <button class="btn" type="button" @click="runMigrate">补齐老测线状态</button>
        <button class="btn" type="button" @click="exportRows">导出地球物理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>测线编号</span>
        <input v-model="keyword" placeholder="按测线编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>修订号</th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>
            R{{ row.revision ?? 1 }}
            <span v-if="row.history?.length" class="rev-history" :title="historyTitle(row)">
              （含 {{ row.history.length }} 个历史版本）
            </span>
          </td>
          <td v-for="column in columns" :key="column">{{ row[column] || '—' }}</td>
          <td>
            <span class="status-tag" :data-status="row.status">{{ row.status }}</span>
          </td>
          <td class="row-actions">
            <button
              v-if="nextAction(row.status)"
              class="link"
              type="button"
              :disabled="busyId === row.id"
              @click="runAction(nextAction(row.status) as string, row)"
            >
              {{ nextAction(row.status) }}
            </button>
            <button
              v-if="row.status === '已归档'"
              class="link danger"
              type="button"
              :disabled="busyId === row.id"
              @click="withdraw(row)"
            >
              撤回改版
            </button>
            <span v-if="!nextAction(row.status) && row.status !== '已归档'" class="muted-text">—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无符合条件的物探测线</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条物探测线</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="ledger-block">
      <header class="ledger-head">
        <h3>三处台账（当前修订水位 R{{ ledgers.revision_head }}）</h3>
        <button class="btn ghost" type="button" @click="loadLedgers">刷新台账</button>
      </header>
      <div class="ledger-grid">
        <article class="ledger-card">
          <h4>勘探区采集台账</h4>
          <table class="mini-table">
            <thead>
              <tr><th>修订号</th><th>测线编号</th><th>勘探区</th><th>状态</th><th>长度</th><th>质量</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in ledgers.acquisition_ledger" :key="`a${item.测线编号}-${item.line_revision}`">
                <td>R{{ item.line_revision }}<span class="muted-text">#{{ item.revision }}</span></td>
                <td>{{ item.测线编号 }}</td>
                <td>{{ item.勘探区 }}</td>
                <td>{{ item.status }}</td>
                <td>{{ item.测线长度 || '—' }}</td>
                <td>{{ item.数据质量 || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </article>
        <article class="ledger-card">
          <h4>成果图清单</h4>
          <table class="mini-table">
            <thead>
              <tr><th>修订号</th><th>测线编号</th><th>状态</th><th>成果图</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in ledgers.map_list" :key="`m${item.测线编号}-${item.line_revision}`">
                <td>R{{ item.line_revision }}<span class="muted-text">#{{ item.revision }}</span></td>
                <td>{{ item.测线编号 }}</td>
                <td>{{ item.status }}</td>
                <td>{{ item.成果图状态 }}</td>
              </tr>
              <tr v-if="!ledgers.map_list.length"><td colspan="4" class="empty-state">暂无可挂出的成果</td></tr>
            </tbody>
          </table>
        </article>
        <article class="ledger-card">
          <h4>待处理列表</h4>
          <table class="mini-table">
            <thead>
              <tr><th>修订号</th><th>测线编号</th><th>状态</th><th>下一动作</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in ledgers.pending_list" :key="`p${item.测线编号}-${item.line_revision}`">
                <td>R{{ item.line_revision }}<span class="muted-text">#{{ item.revision }}</span></td>
                <td>{{ item.测线编号 }}</td>
                <td>{{ item.status }}</td>
                <td>{{ item.next_action }}</td>
              </tr>
              <tr v-if="!ledgers.pending_list.length"><td colspan="4" class="empty-state">没有待处理测线</td></tr>
            </tbody>
          </table>
        </article>
      </div>
    </section>

    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <form class="modal-card" @submit.prevent="submitCreate">
        <h3>登记物探测线</h3>
        <label v-for="field in createFields" :key="field.key" class="modal-item">
          <span>{{ field.label }}{{ field.required ? ' *' : '' }}</span>
          <input v-model="createForm[field.key]" :placeholder="`请输入${field.label}`" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" type="submit">提交登记</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Status = '待施测' | '施测中' | '已采集' | '数据处理' | '已归档'
interface HistorySnapshot {
  revision: number
  status: string
  测线长度?: string
  数据质量?: string
  施测日期?: string
}
interface Row {
  id: number
  status: Status
  revision: number
  history?: HistorySnapshot[]
  测线编号: string
  勘探区: string
  物探方法: string
  测线长度?: string
  点距?: string
  施测日期?: string
  数据质量?: string
  [key: string]: unknown
}
interface LedgerRow {
  revision: number
  line_revision: number
  测线编号: string
  勘探区?: string
  status: string
  测线长度?: string
  数据质量?: string
  成果图状态?: string
  next_action?: string
}
interface Ledgers {
  revision_head: number
  acquisition_ledger: LedgerRow[]
  map_list: LedgerRow[]
  pending_list: LedgerRow[]
}
interface ActionResponse {
  ok: boolean
  message: string
  entry?: Row
  resumed?: boolean
  seq_head?: number | null
}

const ENDPOINT = '/api/geophysics'
const columns = ['测线编号', '勘探区', '物探方法', '测线长度', '点距', '施测日期', '数据质量']
const statuses: Status[] = ['待施测', '施测中', '已采集', '数据处理', '已归档']
const nextActionMap: Record<Status, string | null> = {
  待施测: '开始施测',
  施测中: '完成采集',
  已采集: '提交处理',
  数据处理: '归档',
  已归档: null,
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const busyId = ref<number | null>(null)
// 每条测线在前端本地维护的操作序号水位：被打断的提交凭原序号从断点接着走
const seqWatermark = ref<Record<string, number>>({})

const ledgers = ref<Ledgers>({
  revision_head: 0,
  acquisition_ledger: [],
  map_list: [],
  pending_list: [],
})

const stats = computed(() => [
  { label: '待处理测线', value: rows.value.filter((row) => row.status !== '已归档').length },
  { label: '已归档测线', value: rows.value.filter((row) => row.status === '已归档').length },
  { label: '含历史版本', value: rows.value.filter((row) => (row.history?.length ?? 0) > 0).length },
  { label: '台账修订水位', value: `R${ledgers.value.revision_head}` },
])

const createFields = [
  { key: '测线编号', label: '测线编号', required: true },
  { key: '勘探区', label: '勘探区', required: true },
  { key: '物探方法', label: '物探方法', required: true },
  { key: '测线长度', label: '测线长度', required: false },
  { key: '点距', label: '点距', required: false },
] as const

const showCreate = ref(false)
const createForm = ref<Record<string, string>>({})

function nextAction(status: string): string | null {
  return nextActionMap[status as Status] ?? null
}

function historyTitle(row: Row): string {
  return (row.history ?? [])
    .map((item) => `R${item.revision} ${item.status}｜长度 ${item.测线长度 || '—'}｜质量 ${item.数据质量 || '—'}`)
    .join('\n')
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  createForm.value = {}
  showCreate.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  const response = await request(ENDPOINT, {
    method: 'POST',
    body: JSON.stringify({ values: createForm.value }),
  })
  const payload = (await response.json()) as ActionResponse
  if (!payload.ok) {
    errorMessage.value = payload.message
    return
  }
  showCreate.value = false
  await Promise.all([reload(), loadLedgers()])
}

async function postMutation(
  path: string,
  body: Record<string, unknown>,
  row: Row,
  okPrefix: string,
): Promise<void> {
  busyId.value = row.id
  errorMessage.value = ''
  try {
    const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
    const payload = (await response.json()) as ActionResponse
    // 服务端回带该测线的序号水位，本地对齐，避免被打断后续跑错号
    if (payload.seq_head != null) {
      seqWatermark.value[row.测线编号] = Math.max(
        seqWatermark.value[row.测线编号] ?? 0,
        payload.seq_head,
      )
    }
    if (response.status === 409) {
      errorMessage.value = `并发冲突：${payload.message}（已对齐序号水位，可重新提交）`
      await reload()
      return
    }
    if (!payload.ok) {
      errorMessage.value = payload.message
      return
    }
    errorMessage.value = payload.resumed
      ? `${okPrefix}已按原操作序号断点续跑，未重复推进`
      : payload.message
    await Promise.all([reload(), loadLedgers()])
  } catch (error) {
    // 请求未送达：不推进本地水位，重试沿用同一序号，服务端按断点接着走
    errorMessage.value =
      error instanceof Error
        ? `${error.message}；提交可能已到达，可直接重试，不会重复推进`
        : '操作请求失败，可按原序号重试'
  } finally {
    busyId.value = null
  }
}

async function runAction(action: string, row: Row) {
  const lineNo = row.测线编号
  const seq = (seqWatermark.value[lineNo] ?? 0) + 1
  seqWatermark.value[lineNo] = seq
  await postMutation(
    `${ENDPOINT}/${row.id}/actions`,
    { action, seq, revision: row.revision },
    row,
    action,
  )
}

async function withdraw(row: Row) {
  const lineNo = row.测线编号
  const seq = (seqWatermark.value[lineNo] ?? 0) + 1
  seqWatermark.value[lineNo] = seq
  await postMutation(
    `${ENDPOINT}/${row.id}/withdraw`,
    { seq, revision: row.revision },
    row,
    '撤回改版',
  )
}

async function runMigrate() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/migrate`, { method: 'POST' })
    const payload = (await response.json()) as ActionResponse
    errorMessage.value = payload.message
    await Promise.all([reload(), loadLedgers()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '老测线迁移失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  query.set('size', '200')
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('物探测线列表读取失败')
    }
    const payload = (await response.json()) as { items: Row[]; total: number }
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '地球物理列表读取失败'
  }
}

async function loadLedgers() {
  try {
    const response = await request(`${ENDPOINT}/ledgers`)
    if (response.ok) {
      ledgers.value = (await response.json()) as Ledgers
    }
  } catch {
    // 台账读取失败不阻断主表操作
  }
}

onMounted(() => {
  void reload()
  void loadLedgers()
})
</script>

<style scoped>
.page-actions {
  display: flex;
  gap: 8px;
}
.filter-item select,
.filter-item input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 13px;
}
.rev-history {
  color: var(--muted);
  font-size: 12px;
}
.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #eef2ff;
  color: #3730a3;
}
.status-tag[data-status='已归档'] {
  background: #ecfdf3;
  color: #027a48;
}
.status-tag[data-status='施测中'] {
  background: #fffaeb;
  color: #b54708;
}
.link.danger {
  color: #b42318;
}
.link:disabled {
  color: #9aa4b2;
  cursor: not-allowed;
}
.muted-text {
  color: var(--muted);
  font-size: 12px;
}
.ledger-block {
  margin-top: 20px;
}
.ledger-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.ledger-head h3 {
  margin: 0 0 8px;
  font-size: 15px;
}
.ledger-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.ledger-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
}
.ledger-card h4 {
  margin: 0 0 8px;
  font-size: 13px;
}
.mini-table {
  width: 100%;
  border-collapse: collapse;
}
.mini-table th,
.mini-table td {
  border: 1px solid var(--border);
  padding: 5px 7px;
  font-size: 12px;
  text-align: left;
  white-space: nowrap;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(16, 24, 40, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
}
.modal-card {
  width: 380px;
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
}
.modal-card h3 {
  margin: 0 0 12px;
}
.modal-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
  font-size: 13px;
}
.modal-item input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 9px;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 6px;
}
</style>
