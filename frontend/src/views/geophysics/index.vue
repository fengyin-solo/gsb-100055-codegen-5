<template>
  <section class="page" data-module="geophysics">
    <header class="page-head">
      <div>
        <h2>地球物理管理</h2>
        <p class="page-desc">
          物探测线沿「待施测→施测中→已采集→数据处理→已归档」单向推进；每次推进带修订号，
          采集台账、成果图清单、待处理列表三处同号。撤回归档开新版本，历史长度与数据质量按原始口径留存。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记物探测线</button>
        <button class="btn" type="button" @click="migrateLegacy">迁移老测线状态</button>
        <button class="btn" type="button" @click="exportRows">导出地球物理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <form class="filter-bar" @submit.prevent="reloadActive">
      <label class="filter-item">
        <span>测线编号</span>
        <input v-model="keyword" placeholder="按测线编号检索" />
      </label>
      <label v-if="activeTab === 'lines'" class="filter-item">
        <span>流程站位</span>
        <input v-model="statusFilter" placeholder="如：待施测" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <!-- 测线主表：每行只给当前站位允许的唯一下一步，跳级/回退由后端兜底拦下 -->
    <table v-if="activeTab === 'lines'" class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>修订号/版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['测线编号'] ?? '—' }}</td>
          <td>{{ row['勘探区'] ?? '—' }}</td>
          <td>{{ row['物探方法'] ?? '—' }}</td>
          <td>{{ row['测线长度'] ?? '—' }}</td>
          <td>{{ row['点距'] ?? '—' }}</td>
          <td>{{ row['施测日期'] ?? '—' }}</td>
          <td>{{ row['数据质量'] ?? '—' }}</td>
          <td>
            <span class="badge" :class="badgeClass(row.status)">{{ row.status }}</span>
          </td>
          <td>
            <span class="badge revision">R{{ row.revision ?? 0 }} · V{{ row.version ?? 1 }}</span>
          </td>
          <td class="row-actions">
            <button
              v-if="nextAction(row.status)"
              class="link"
              type="button"
              @click="submitAction(nextAction(row.status), row)"
            >
              {{ nextAction(row.status) }}
            </button>
            <button
              v-if="row.status === '已归档'"
              class="link"
              type="button"
              @click="submitAction('撤回归档', row)"
            >
              撤回归档
            </button>
            <span v-if="!nextAction(row.status) && row.status !== '已归档'" class="empty-state">—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无物探测线，可先登记一条</td>
        </tr>
      </tbody>
    </table>

    <!-- 三处同步口径 + 修订历史，全部按修订号摆放 -->
    <table v-else class="data-table">
      <thead>
        <tr>
          <th v-for="column in outletColumns[activeTab]" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(item, index) in outletRows" :key="`${activeTab}-${index}`">
          <td v-for="column in outletColumns[activeTab]" :key="column">{{ item[column] ?? '—' }}</td>
        </tr>
        <tr v-if="!outletRows.length">
          <td :colspan="outletColumns[activeTab].length" class="empty-state">暂无数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ activeTabLabel }}记录</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>

    <!-- 登记弹窗 -->
    <div v-if="showCreate" class="modal-mask" @click.self="showCreate = false">
      <form class="modal" @submit.prevent="confirmCreate">
        <h3>登记物探测线</h3>
        <label v-for="field in createFields" :key="field" class="form-item">
          <span>{{ field }}<template v-if="requiredFields.includes(field)">（必填）</template></span>
          <input v-model="createForm[field]" :placeholder="`请输入${field}`" />
        </label>
        <p v-if="createError" class="error-text">{{ createError }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" type="submit">提交登记</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type OutletItem = Record<string, string | number | null>

const ENDPOINT = '/api/geophysics'
const STATUSES = ['待施测', '施测中', '已采集', '数据处理', '已归档']
const NEXT_ACTIONS: Record<string, string> = {
  待施测: '开始施测',
  施测中: '完成采集',
  已采集: '提交处理',
  数据处理: '归档',
}

const columns = ['测线编号', '勘探区', '物探方法', '测线长度', '点距', '施测日期', '数据质量', '测线状态']
const tabs = [
  { key: 'lines', label: '测线台账', path: '' },
  { key: 'ledger', label: '勘探区采集台账', path: '/ledger' },
  { key: 'maps', label: '成果图清单', path: '/maps' },
  { key: 'pending', label: '待处理列表', path: '/pending' },
  { key: 'revisions', label: '修订历史', path: '/revisions' },
] as const
type TabKey = (typeof tabs)[number]['key']

const outletColumns: Record<Exclude<TabKey, 'lines'>, string[]> = {
  ledger: ['revision', '测线编号', 'version', '勘探区', '动作', '前态', '后态', '测线长度', '数据质量', '备注', '时间'],
  maps: ['revision', '测线编号', 'version', '勘探区', '物探方法', '成图状态', '测线状态', '更新时间'],
  pending: ['revision', '测线编号', 'version', '勘探区', '当前状态', '下一步动作', '挂起时间'],
  revisions: ['revision', '测线编号', 'version', '动作', '前态', '后态', '测线长度', '数据质量', '备注', '时间'],
}

const rows = ref<Row[]>([])
const outletRows = ref<OutletItem[]>([])
const total = ref(0)
const counts = ref<Record<string, number>>({})
const activeTab = ref<TabKey>('lines')
const keyword = ref('')
const statusFilter = ref('')
const message = ref('')
const messageOk = ref(false)

// 每条测线各自的操作序号：只在本页会话内单调递增，重发同号即断点回放，绝不推进两次
const seqByLine = reactive<Record<string, number>>({})
// 防止用户连点
const busy = reactive<Record<string, boolean>>({})

const requiredFields = ['测线编号', '勘探区', '物探方法']
const createFields = [...requiredFields, '测线长度', '点距', '施测日期', '数据质量']
const showCreate = ref(false)
const createError = ref('')
const createForm = reactive<Record<string, string>>({})

const stats = computed(() => [
  { label: '待施测测线', value: counts.value['待施测'] ?? 0 },
  { label: '施测中/已采集/处理中', value: (counts.value['施测中'] ?? 0) + (counts.value['已采集'] ?? 0) + (counts.value['数据处理'] ?? 0) },
  { label: '已归档测线', value: counts.value['已归档'] ?? 0 },
])
const activeTabLabel = computed(() => tabs.find((tab) => tab.key === activeTab.value)?.label ?? '')

function nextAction(status: unknown): string {
  return NEXT_ACTIONS[String(status)] ?? ''
}

function badgeClass(status: unknown): string {
  if (status === '已归档') return 'done'
  if (status === '待施测') return ''
  return 'busy'
}

function notify(text: string, ok = false) {
  message.value = text
  messageOk.value = ok
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reloadActive()
}

async function loadCounts() {
  const response = await request(`${ENDPOINT}/status-summary`)
  if (response.status === 200) {
    const payload = await response.json()
    counts.value = payload.counts ?? {}
  }
}

async function reloadLines() {
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  const response = await request(`${ENDPOINT}?${params.toString()}`)
  if (response.status !== 200) {
    notify('物探测线列表读取失败')
    return
  }
  const payload = await response.json()
  rows.value = payload.items ?? []
  total.value = payload.total ?? rows.value.length
  await loadCounts()
}

async function reloadOutlet() {
  const tab = tabs.find((item) => item.key === activeTab.value)
  if (!tab || !tab.path) return
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  const response = await request(`${ENDPOINT}${tab.path}?${params.toString()}`)
  if (response.status !== 200) {
    notify('同步口径读取失败')
    return
  }
  const payload = await response.json()
  outletRows.value = payload.items ?? []
  total.value = payload.total ?? outletRows.value.length
}

async function reloadActive() {
  message.value = ''
  if (activeTab.value === 'lines') {
    await reloadLines()
  } else {
    await reloadOutlet()
  }
}

function switchTab(key: TabKey) {
  activeTab.value = key
  void reloadActive()
}

async function submitAction(action: string, row: Row) {
  const lineKey = String(row.id)
  if (busy[lineKey]) {
    notify('该测线已有提交在处理，请勿重复点击，结果会按操作序号续上', false)
    return
  }
  seqByLine[lineKey] = (seqByLine[lineKey] ?? Number(row.last_seq ?? 0)) + 1
  busy[lineKey] = true
  try {
    const response = await request(`${ENDPOINT}/${lineKey}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        action,
        seq: seqByLine[lineKey],
        expected_revision: Number(row.revision ?? 0),
      }),
    })
    const payload = await response.json().catch(() => ({ message: '响应解析失败' }))
    if (response.status === 409) {
      // 并发核对落败：撤销本地占用的序号，刷新后从断点重新走
      seqByLine[lineKey] = Number(row.revision ?? 0)
      notify(payload.detail ?? '并发核对失败，该测线已被其他提交推进，请刷新后重试', false)
    } else if (payload.ok) {
      notify(payload.message, true)
    } else {
      // 跳级/回退等拦截，序号已被服务端忽略，回退本地计数，修正后仍从断点走
      seqByLine[lineKey] -= 1
      notify(payload.message ?? '动作未生效', false)
    }
  } catch (error) {
    // 网络中断：保留序号，用户重试即按断点回放，不会推进两次
    notify(error instanceof Error ? error.message : '提交未送达，可原样重试（操作序号保留）', false)
  } finally {
    busy[lineKey] = false
    await reloadLines()
  }
}

function openCreate() {
  createFields.forEach((field) => {
    createForm[field] = ''
  })
  createError.value = ''
  showCreate.value = true
}

async function confirmCreate() {
  createError.value = ''
  const values: Record<string, string> = {}
  createFields.forEach((field) => {
    const text = createForm[field]?.trim()
    if (text) values[field] = text
  })
  const response = await request(ENDPOINT, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json().catch(() => ({ ok: false, message: '响应解析失败' }))
  if (!payload.ok) {
    createError.value = payload.message
    return
  }
  showCreate.value = false
  notify(payload.message ?? '物探测线已登记', true)
  await reloadLines()
}

async function migrateLegacy() {
  try {
    const response = await request(`${ENDPOINT}/migrate-legacy`, { method: 'POST' })
    const payload = await response.json()
    notify(payload.message ?? '老测线迁移完成', true)
    await reloadActive()
  } catch (error) {
    notify(error instanceof Error ? error.message : '老测线迁移失败', false)
  }
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

onMounted(reloadActive)
</script>
