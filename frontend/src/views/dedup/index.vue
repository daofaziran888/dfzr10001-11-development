<template>
  <section class="page" data-module="dedup">
    <header class="page-head">
      <div>
        <h2>采集去重管理</h2>
        <p class="page-desc">入库前预检：按目标字段与时间窗口生成候选组，确认后剔除结果进入存储追溯清单，胜出记录立即列入待入库。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出采集去重清单</button>
      </div>
    </header>

    <div class="tab-row">
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
    </div>

    <p v-if="message" class="message-text" :class="{ 'error-text': !messageOk }">{{ message }}</p>

    <!-- 1. 去重规则 -->
    <div v-show="activeTab === 'rules'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in ruleColumns" :key="column">{{ column }}</th>
            <th>状态</th>
            <th>去重预检</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rules" :key="String(row.id)">
            <td v-for="column in ruleColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>
              <span class="status-tag" :class="row.status === '已启用' ? 'on' : 'off'">{{ row.status }}</span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="startPrecheck(String(row['规则编号']))">生成候选组</button>
            </td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ rules.length }} 条去重规则；已启用规则只参与读取，预检不改写规则配置</span></footer>
    </div>

    <!-- 2. 去重预检 -->
    <div v-show="activeTab === 'precheck'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">预检单</span>
          <strong class="stat-value">{{ prechecks.length }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待确认</span>
          <strong class="stat-value">{{ countByStatus('待确认') }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已中断</span>
          <strong class="stat-value">{{ countByStatus('已中断') }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已确认</span>
          <strong class="stat-value">{{ countByStatus('已确认') }}</strong>
        </article>
      </div>

      <div v-for="pc in prechecks" :key="String(pc.id)" class="precheck-card">
        <div class="precheck-head">
          <strong>{{ pc['预检编号'] }} · {{ pc['规则编号'] }}</strong>
          <span class="status-tag" :class="statusClass(pc['状态'])">{{ pc['状态'] }}</span>
          <span class="muted">锚点 {{ pc['锚点时间'] }} · 窗口 {{ pc['窗口天数'] }} 天 · 候选组 {{ pc['已处理组数'] }}/{{ pc['候选组总数'] }} · 命中 {{ pc['命中记录数'] }} 条 · 拟剔除 {{ pc['待剔除数'] }} 条 · 拟保留 {{ pc['拟保留数'] }} 条</span>
          <span class="row-actions">
            <button v-if="pc['状态'] === '已中断'" class="link" type="button" @click="resume(pc.id)">续跑</button>
            <button
              v-if="pc['状态'] === '进行中' || pc['状态'] === '待确认'"
              class="link" type="button" @click="interrupt(pc.id)"
            >中断</button>
            <button
              v-if="pc['状态'] === '待确认'"
              class="link primary-link" type="button" @click="confirm(pc.id)"
            >确认预检</button>
          </span>
        </div>
        <p v-if="pc['备注']" class="muted note">{{ pc['备注'] }}</p>
        <div v-for="group in pc['候选组']" :key="group['候选组编号']" class="group-box">
          <div class="group-title">
            <span class="group-no">{{ group['候选组编号'] }}</span>
            <span class="muted">
              {{ group['目标字段'] }} = {{ group['目标字段值'] }} ｜ {{ group['去重粒度'] }} ｜ 窗口 {{ group['时间窗口'] }}
              ｜ {{ group['去重策略'] }} ｜ {{ group['保留规则'] }} ｜ {{ group['落地方式'] }} ｜ 启用：{{ group['是否启用'] }}
            </span>
            <span class="muted">闭窗口 [{{ group['窗口起'] }} ~ {{ group['窗口止'] }}]</span>
          </div>
          <table class="data-table inner-table">
            <thead>
              <tr>
                <th>记录编号</th><th>采集时间</th><th>来源顺序</th><th>来源标识</th>
                <th>预检判定</th><th>判定说明</th><th>命中的窗口边界</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="candidate in group['候选记录']" :key="String(candidate['记录编号'])">
                <td>{{ candidate['记录编号'] }}</td>
                <td>{{ candidate['采集时间'] }}</td>
                <td>{{ candidate['来源顺序'] }}</td>
                <td>{{ candidate['来源标识'] }}</td>
                <td>
                  <span class="verdict" :class="verdictClass(candidate['预检判定'])">{{ candidate['预检判定'] }}</span>
                </td>
                <td>{{ candidate['判定说明'] || '—' }}</td>
                <td class="boundary-cell">{{ candidate['命中窗口边界'] }}</td>
              </tr>
            </tbody>
          </table>
          <p class="muted note">
            保留：{{ group['保留记录编号'] || '—' }} ｜ 剔除：{{ (group['剔除记录编号'] || []).join('、') || '—' }}
          </p>
        </div>
      </div>
      <p v-if="!prechecks.length" class="empty-state">暂无预检单，请在「去重规则」页签生成候选组</p>
    </div>

    <!-- 3. 剔除追溯清单（采集数据存储清单的可追溯记录） -->
    <div v-show="activeTab === 'removals'">
      <form class="filter-bar" @submit.prevent="loadRemovals">
        <label class="filter-item">
          <span>规则编号</span>
          <input v-model="removalFilter" placeholder="如 DEDU-0002" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetRemovalFilter">重置</button>
      </form>
      <div v-for="groupNo in removalGroups" :key="groupNo" class="group-box">
        <div class="group-title"><span class="group-no">{{ groupNo }}</span><span class="muted">剔除 {{ groupCount(groupNo) }} 条</span></div>
        <table class="data-table inner-table">
          <thead>
            <tr>
              <th>剔除编号</th><th>规则编号</th><th>记录编号</th><th>目标字段</th><th>目标字段值</th>
              <th>保留规则</th><th>保留记录</th><th>采集时间</th><th>命中窗口边界</th><th>剔除时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rowsOfGroup(groupNo)" :key="String(row.id)">
              <td>{{ row['剔除编号'] }}</td>
              <td>{{ row['规则编号'] }}</td>
              <td>{{ row['记录编号'] }}</td>
              <td>{{ row['目标字段'] }}</td>
              <td>{{ row['目标字段值'] }}</td>
              <td>{{ row['保留规则'] }}</td>
              <td>{{ row['保留记录编号'] }}</td>
              <td>{{ row['采集时间'] }}</td>
              <td class="boundary-cell">{{ row['命中窗口边界'] }}</td>
              <td>{{ row['剔除时间'] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!removals.length" class="empty-state">暂无剔除记录，确认预检后在此生成可追溯清单</p>
      <footer class="page-foot"><span>共 {{ removals.length }} 条剔除记录，按候选组编号分段展示</span></footer>
    </div>

    <!-- 4. 待入库（任务工作台业务面） -->
    <div v-show="activeTab === 'pending'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">待入库总数</span>
          <strong class="stat-value">{{ workbench['待入库总数'] ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">剔除总数</span>
          <strong class="stat-value">{{ workbench['剔除总数'] ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">留档批次</span>
          <strong class="stat-value">{{ workbench['留档批次数'] ?? 0 }}</strong>
        </article>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>待入库编号</th><th>规则编号</th><th>候选组编号</th><th>记录编号</th>
            <th>目标字段</th><th>目标字段值</th><th>采集时间</th><th>来源顺序</th><th>来源标识</th><th>生成时间</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in pendingRows" :key="String(row.id)">
            <td>{{ row['待入库编号'] }}</td>
            <td>{{ row['规则编号'] }}</td>
            <td>{{ row['候选组编号'] }}</td>
            <td>{{ row['记录编号'] }}</td>
            <td>{{ row['目标字段'] }}</td>
            <td>{{ row['目标字段值'] }}</td>
            <td>{{ row['采集时间'] }}</td>
            <td>{{ row['来源顺序'] }}</td>
            <td>{{ row['来源标识'] }}</td>
            <td>{{ row['生成时间'] }}</td>
            <td><span class="status-tag on">{{ row['状态'] }}</span></td>
          </tr>
        </tbody>
      </table>
      <p v-if="!pendingRows.length" class="empty-state">暂无待入库条目，预检确认后立即列出</p>
    </div>

    <!-- 5. 历史留档 -->
    <div v-show="activeTab === 'archives'">
      <div v-for="arc in archives" :key="String(arc.id)" class="precheck-card">
        <div class="precheck-head">
          <strong>{{ arc['留档编号'] }} · {{ arc['规则编号'] }}</strong>
          <span class="muted">来源预检 {{ arc['预检编号'] }} · 确认时间 {{ arc['确认时间'] }}</span>
          <span class="muted">候选组 {{ arc['候选组数'] }} ｜ 本次剔除 {{ arc['本次剔除数'] }} ｜ 本次待入库 {{ arc['本次待入库数'] }}</span>
        </div>
        <p class="muted note">{{ arc['留档说明'] }}</p>
        <table class="data-table inner-table">
          <thead>
            <tr><th>目标字段</th><th>去重粒度</th><th>时间窗口</th><th>去重策略</th><th>保留规则</th><th>落地方式</th><th>是否启用</th></tr>
          </thead>
          <tbody>
            <tr>
              <td>{{ arc['规则快照']['目标字段'] }}</td>
              <td>{{ arc['规则快照']['去重粒度'] }}</td>
              <td>{{ arc['规则快照']['时间窗口'] }}</td>
              <td>{{ arc['规则快照']['去重策略'] }}</td>
              <td>{{ arc['规则快照']['保留规则'] }}</td>
              <td>{{ arc['规则快照']['落地方式'] }}</td>
              <td>{{ arc['规则快照']['是否启用'] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!archives.length" class="empty-state">暂无历史留档；确认预检时按当时去重规则快照留档</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Dict = Record<string, any>
type Precheck = Dict
type Workbench = Dict

const ENDPOINT = '/api/dedup'
const ruleColumns = ['规则编号', '目标字段', '去重粒度', '时间窗口', '去重策略', '保留规则', '落地方式', '是否启用']
const tabs = [
  { key: 'rules', label: '去重规则' },
  { key: 'precheck', label: '去重预检' },
  { key: 'removals', label: '剔除追溯清单' },
  { key: 'pending', label: '待入库（工作台）' },
  { key: 'archives', label: '历史留档' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('rules')
const message = ref('')
const messageOk = ref(true)

const rules = ref<Row[]>([])
const prechecks = ref<Precheck[]>([])
const removals = ref<Row[]>([])
const pendingRows = ref<Row[]>([])
const archives = ref<Dict[]>([])
const workbench = ref<Workbench>({})
const removalFilter = ref('')

const removalGroups = computed(() => [...new Set(removals.value.map((r) => String(r['候选组编号'])))])

function notify(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function countByStatus(status: string) {
  return prechecks.value.filter((pc) => pc['状态'] === status).length
}

function statusClass(status: string) {
  if (status === '已确认' || status === '待确认') return 'on'
  if (status === '已中断') return 'warn'
  return 'off'
}

function verdictClass(verdict: string) {
  if (verdict === '保留') return 'keep'
  if (verdict === '待剔除') return 'drop'
  return 'skip'
}

function rowsOfGroup(groupNo: string) {
  return removals.value.filter((r) => String(r['候选组编号']) === groupNo)
}

function groupCount(groupNo: string) {
  return rowsOfGroup(groupNo).length
}

async function loadRules() {
  const response = await request(`${ENDPOINT}?size=200`)
  const payload = await response.json()
  rules.value = payload.items ?? []
}

async function loadPrechecks() {
  const response = await request(`${ENDPOINT}/prechecks/list`)
  const payload = await response.json()
  prechecks.value = payload.items ?? []
}

async function loadRemovals() {
  const query = new URLSearchParams()
  if (removalFilter.value.trim()) query.set('rule_code', removalFilter.value.trim())
  const response = await request(`${ENDPOINT}/removals/list?${query.toString()}`)
  const payload = await response.json()
  removals.value = payload.items ?? []
}

function resetRemovalFilter() {
  removalFilter.value = ''
  void loadRemovals()
}

async function loadPending() {
  const [pendingResp, wbResp] = await Promise.all([
    request(`${ENDPOINT}/pending/list`),
    request(`${ENDPOINT}/workbench/summary`),
  ])
  pendingRows.value = (await pendingResp.json()).items ?? []
  workbench.value = await wbResp.json()
}

async function loadArchives() {
  const response = await request(`${ENDPOINT}/archives/list`)
  const payload = await response.json()
  archives.value = payload.items ?? []
}

async function refreshAll() {
  await Promise.all([loadRules(), loadPrechecks(), loadRemovals(), loadPending(), loadArchives()])
}

async function switchTab(key: (typeof tabs)[number]['key']) {
  activeTab.value = key
  message.value = ''
  await refreshAll()
}

async function postJson(path: string, body?: unknown) {
  return request(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
}

async function startPrecheck(ruleCode: string) {
  const response = await postJson(`${ENDPOINT}/prechecks`, { values: { 规则编号: ruleCode } })
  const payload = await response.json()
  notify(payload.message, payload.ok)
  if (payload.ok) {
    activeTab.value = 'precheck'
    await refreshAll()
  }
}

async function resume(id: number) {
  const payload = (await (await postJson(`${ENDPOINT}/prechecks/${id}/resume`)).json()) as { ok: boolean; message: string }
  notify(payload.message, payload.ok)
  await refreshAll()
}

async function interrupt(id: number) {
  const payload = (await (await postJson(`${ENDPOINT}/prechecks/${id}/interrupt`)).json()) as { ok: boolean; message: string }
  notify(payload.message, payload.ok)
  await refreshAll()
}

async function confirm(id: number) {
  const payload = (await (await postJson(`${ENDPOINT}/prechecks/${id}/confirm`)).json()) as { ok: boolean; message: string }
  notify(payload.message, payload.ok)
  await refreshAll()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

onMounted(refreshAll)
</script>

<style scoped>
.tab-row { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.tab-btn { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 13px; }
.tab-btn.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.message-text { font-size: 13px; margin: 4px 0 8px; color: #067647; }
.precheck-card, .group-box { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.precheck-head { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 13px; }
.group-box { background: #f8fafc; }
.group-title { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; font-size: 13px; }
.group-no { font-weight: 600; color: var(--brand); }
.inner-table { font-size: 12px; }
.boundary-cell { color: var(--muted); font-size: 12px; min-width: 260px; }
.muted { color: var(--muted); font-size: 12px; }
.note { margin: 6px 0; }
.status-tag { padding: 2px 8px; border-radius: 10px; font-size: 12px; border: 1px solid transparent; }
.status-tag.on { background: #ecfdf3; color: #067647; border-color: #abefc6; }
.status-tag.off { background: #f2f4f7; color: #475467; border-color: #d0d5dd; }
.status-tag.warn { background: #fffaeb; color: #b54708; border-color: #fedf89; }
.verdict { font-weight: 600; }
.verdict.keep { color: #067647; }
.verdict.drop { color: #b42318; }
.verdict.skip { color: #475467; }
.primary-link { font-weight: 600; }
</style>
