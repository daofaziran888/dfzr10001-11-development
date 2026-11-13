<template>
  <section class="page" data-module="task">
    <header class="page-head">
      <div>
        <h2>采集任务管理</h2>
        <p class="page-desc">维护采集任务的调度周期、增量策略与执行参数，实现按需或定时采集调度。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集任务</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集任务数据，可先登记采集任务</td>
        </tr>
      </tbody>
    </table>

    <section class="linkage-box">
      <header class="linkage-head">
        <h3>去重待入库工作台</h3>
        <p class="page-desc">去重预检确认后立即列出的待入库条目，每条来自一个候选组的胜出记录；同时汇总剔除与留档口径。</p>
      </header>
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
            <th>目标字段</th><th>目标字段值</th><th>采集时间</th><th>来源标识</th><th>生成时间</th><th>状态</th>
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
            <td>{{ row['来源标识'] }}</td>
            <td>{{ row['生成时间'] }}</td>
            <td><span class="status-tag">{{ row['状态'] }}</span></td>
          </tr>
          <tr v-if="!pendingRows.length">
            <td :colspan="10" class="empty-state">暂无待入库条目，去重预检确认后立即列出</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/task'
const columns = ["任务编号", "任务名称", "关联源端", "采集频率", "增量字段", "起始水位", "批次大小", "执行窗口"]
const actions = ["提交执行", "暂停任务", "恢复任务"]
const statuses = ["草稿", "待执行", "执行中", "已完成"]
const stats = [{"label": "采集任务总数", "value": 0}, {"label": "待执行任务", "value": 0}, {"label": "执行中任务", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const pendingRows = ref<Row[]>([])
const workbench = ref<Record<string, any>>({})

async function loadWorkbench() {
  try {
    const response = await request('/api/task/dedup-workbench')
    if (response.ok) {
      const payload = await response.json()
      workbench.value = payload
      pendingRows.value = payload['待入库条目'] ?? []
    }
  } catch {
    // 联动数据读取失败不影响任务主表
    pendingRows.value = []
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集任务动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadWorkbench()
})
</script>

<style scoped>
.linkage-box { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 16px; }
.linkage-head h3 { margin: 0 0 4px; font-size: 15px; }
.status-tag { padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #ecfdf3; color: #067647; border: 1px solid #abefc6; }
</style>
