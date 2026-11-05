<template>
  <div class="maibot-ctx">
    <div class="page-header">
      <div>
        <h1>🧠 MaiBot 上下文健康</h1>
        <p class="subtitle">
          Planner token · 延迟 · 会话轮换
          <span v-if="generatedAt">· 更新于 {{ generatedAt }}</span>
        </p>
      </div>
      <div class="header-actions">
        <button class="btn" :disabled="loading" @click="load(true)">
          {{ loading ? '刷新中…' : '↻ 刷新' }}
        </button>
      </div>
    </div>

    <div v-if="error" class="alert error">{{ error }}</div>
    <div v-if="notice" class="alert ok">{{ notice }}</div>

    <!-- 总览 -->
    <section class="grid-4">
      <div class="card">
        <div class="card-title">输入 Tokens</div>
        <div class="big-number">{{ tokens.latest || '—' }}</div>
        <div class="sub">P50 {{ tokens.p50 }} · P95 {{ tokens.p95 }} · 峰值 {{ tokens.max }}</div>
      </div>
      <div class="card">
        <div class="card-title">Planner 耗时</div>
        <div class="big-number">{{ sec(duration.p50) }}</div>
        <div class="sub">P95 {{ sec(duration.p95) }} · 峰值 {{ sec(duration.max) }}</div>
      </div>
      <div class="card">
        <div class="card-title">超阈值次数</div>
        <div class="big-number">{{ tokens.over_soft }} <span class="muted">/ {{ tokens.over_hard }}</span></div>
        <div class="sub">超软 {{ thresholds.soft_tokens }} / 超硬 {{ thresholds.hard_tokens }}</div>
      </div>
      <div class="card">
        <div class="card-title">会话轮换</div>
        <div class="big-number">{{ guard.rotate_count || 0 }}</div>
        <div class="sub">自动 {{ guard.auto_rotate_count || 0 }} · 手动 {{ guard.manual_rotate_count || 0 }}</div>
      </div>
    </section>

    <!-- 延迟告警 -->
    <section class="grid-2">
      <div class="card">
        <div class="card-title">延迟分布 <span class="badge" :class="duration.over_60s ? 'bad' : 'ok'">
          {{ duration.over_60s ? '有超时风险' : '正常' }}</span></div>
        <div class="sub">采样 {{ duration.count }} 次</div>
        <table class="kv">
          <tr><td>超过 30 秒</td><td>{{ duration.over_30s }} 次</td></tr>
          <tr><td>超过 60 秒（客户端超时线）</td><td :class="{bad: duration.over_60s}">{{ duration.over_60s }} 次</td></tr>
          <tr><td>P50 / P95</td><td>{{ sec(duration.p50) }} / {{ sec(duration.p95) }}</td></tr>
        </table>
      </div>

      <div class="card">
        <div class="card-title">上下文构成</div>
        <div class="sub">按 item 字符数占比（采样 {{ planner.sampled_files }} 次）</div>
        <table class="kv">
          <tr v-for="c in composition" :key="c.item_type">
            <td>{{ label(c.item_type) }}</td>
            <td>
              <div class="bar"><div class="bar-fill" :style="{width: c.percent + '%'}"></div></div>
              {{ c.percent }}%
            </td>
          </tr>
        </table>
      </div>
    </section>

    <!-- 操作区 -->
    <section class="card">
      <div class="card-title">管理员操作 <span class="badge warn">写指令由插件执行</span></div>
      <div class="sub">
        轮换会清空该会话的短期上下文（长期记忆与聊天流身份保留）。
        操作写入指令文件，由 monika-governance 插件在容器内执行，需要二次确认。
      </div>

      <div class="ops">
        <label class="field">
          <span>目标会话</span>
          <select v-model="selectedSession">
            <option value="">— 选择会话 —</option>
            <option v-for="s in sessions" :key="s.session_id" :value="s.session_id">
              {{ s.session_id }}（{{ s.count }} 次请求，最近 {{ s.latest_total }} tokens）
            </option>
          </select>
        </label>

        <button class="btn danger" :disabled="!selectedSession || busy" @click="confirmRotate">
          🔄 安全重置该会话上下文
        </button>

        <label class="field inline">
          <input type="checkbox" :checked="autoRotate" :disabled="busy" @change="toggleAuto">
          <span>自动轮换（超过硬阈值 {{ thresholds.hard_tokens }} tokens 时自动整理）</span>
        </label>
      </div>
    </section>

    <!-- 最近轮换 -->
    <section class="card" v-if="guard.last_rotate">
      <div class="card-title">最近一次轮换</div>
      <table class="kv">
        <tr><td>时间</td><td>{{ guard.last_rotate.ts }}</td></tr>
        <tr><td>会话</td><td>{{ guard.last_rotate.session_id }}</td></tr>
        <tr><td>原因</td><td>{{ guard.last_rotate.reason }}</td></tr>
        <tr><td>运行时已清理</td><td>{{ guard.last_rotate.runtime_cleared ? '是' : '否' }}</td></tr>
        <tr><td>清理边界标记</td><td>{{ guard.last_rotate.marker_written ? '已写入' : '未写入（重启后可能恢复历史）' }}</td></tr>
      </table>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'

const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const planner = ref({ tokens: {}, duration_ms: {}, composition: [], sessions: [] })
const guard = ref({})
const thresholds = ref({ soft_tokens: 8000, hard_tokens: 12000 })
const compactionCount = ref(0)
const selectedSession = ref('')
const autoRotate = ref(false)

const tokens = computed(() => planner.value.tokens || {})
const duration = computed(() => planner.value.duration_ms || {})
const composition = computed(() => planner.value.composition || [])
const sessions = computed(() => planner.value.sessions || [])
const generatedAt = computed(() => '')

const LABELS = {
  UserMessageItem: '聊天消息',
  AssistantMessageItem: '模型输出/推理',
  SystemMessageItem: '系统与人设',
  ReasoningItem: '推理块',
  FunctionCallItem: '工具调用',
  FunctionCallOutputItem: '工具结果',
}
const label = (t) => LABELS[t] || t
const sec = (ms) => (ms ? (ms / 1000).toFixed(1) + 's' : '—')

async function api(path, options) {
  const res = await fetch(path, options)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status))
  return data
}

async function load(showNotice) {
  loading.value = true
  error.value = ''
  try {
    const d = await api('/api/maibot-context/health')
    planner.value = d.planner || {}
    guard.value = d.guard || {}
    thresholds.value = d.thresholds || thresholds.value
    compactionCount.value = d.compaction_count || 0
    autoRotate.value = !!(d.guard && d.guard.last_rotate && d.guard.auto_rotate)
    if (showNotice) notice.value = '已刷新'
    setTimeout(() => (notice.value = ''), 1500)
  } catch (e) {
    error.value = '加载失败：' + e.message
  } finally {
    loading.value = false
  }
}

async function confirmRotate() {
  const sid = selectedSession.value
  if (!sid) return
  const ok = window.confirm(
    '确定要重置会话 ' + sid + ' 的短期上下文吗？\n\n' +
    '• 该会话的近期对话上下文会被清空\n' +
    '• 长期记忆、人物画像、聊天流身份保留\n' +
    '• 此操作会写入审计日志'
  )
  if (!ok) return
  busy.value = true
  error.value = ''
  try {
    await api('/api/maibot-context/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'rotate_session',
        session_id: sid,
        reason: 'dashboard_manual',
        confirm: true,
      }),
    })
    notice.value = '已下发轮换指令，插件会在几秒内执行'
    await load(false)
  } catch (e) {
    error.value = '操作失败：' + e.message
  } finally {
    busy.value = false
    setTimeout(() => (notice.value = ''), 3000)
  }
}

async function toggleAuto(e) {
  const want = e.target.checked
  busy.value = true
  try {
    await api('/api/maibot-context/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'toggle_auto_rotate',
        auto_rotate: want,
        reason: 'dashboard_toggle',
        confirm: true,
      }),
    })
    autoRotate.value = want
    notice.value = want ? '已开启自动轮换' : '已关闭自动轮换'
  } catch (err) {
    error.value = '切换失败：' + err.message
    e.target.checked = !want
  } finally {
    busy.value = false
    setTimeout(() => (notice.value = ''), 2500)
  }
}

onMounted(() => load(false))
</script>

<style scoped>
.maibot-ctx { padding: 8px; }
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 14px 0; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 14px 0; }
.card { background: var(--card-bg, #fff); border: 1px solid var(--border, #e5e7eb);
        border-radius: 10px; padding: 16px; margin-bottom: 14px; }
.card-title { font-weight: 600; margin-bottom: 8px; display: flex; align-items: center;
              justify-content: space-between; }
.big-number { font-size: 28px; font-weight: 700; line-height: 1.2; }
.big-number .muted { font-size: 16px; color: var(--muted, #9ca3af); }
.sub { color: var(--muted, #6b7280); font-size: 13px; margin-top: 4px; }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; font-weight: 500; }
.badge.ok { background: #dcfce7; color: #166534; }
.badge.bad { background: #fee2e2; color: #991b1b; }
.badge.warn { background: #fef3c7; color: #92400e; }
.alert { padding: 10px 14px; border-radius: 8px; margin: 10px 0; }
.alert.error { background: #fee2e2; color: #991b1b; }
.alert.ok { background: #dcfce7; color: #166534; }
.kv { width: 100%; border-collapse: collapse; font-size: 13px; }
.kv td { padding: 6px 4px; border-bottom: 1px solid var(--border, #f0f0f0); }
.kv td:first-child { color: var(--muted, #6b7280); width: 55%; }
.kv td.bad { color: #991b1b; font-weight: 600; }
.bar { height: 8px; background: var(--border, #eee); border-radius: 4px; overflow: hidden;
       min-width: 120px; display: inline-block; vertical-align: middle; margin-right: 8px; }
.bar-fill { height: 100%; background: var(--primary, #2563eb); }
.ops { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin-top: 12px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.field.inline { flex-direction: row; align-items: center; gap: 8px; }
.field select { padding: 6px 10px; border: 1px solid var(--border, #d1d5db); border-radius: 6px;
                min-width: 320px; }
.btn { padding: 8px 14px; border: 1px solid var(--border, #d1d5db); border-radius: 8px;
       background: var(--card-bg, #fff); cursor: pointer; font-size: 13px; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.danger { background: #dc2626; color: #fff; border-color: #b91c1c; }
</style>
