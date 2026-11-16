import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/metric'
const columns = [
  "采集器编号",
  "作业名称",
  "指标名称",
  "采集间隔",
  "聚合方式",
  "保留天数",
  "最近采集",
  "采集器状态"
]
const actions = [
  "启动采集",
  "暂停采集",
  "关闭采集"
]
const statuses = [
  "草稿",
  "采集中",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "指标采集器总数",
    "value": 0
  },
  {
    "label": "采集中采集器",
    "value": 0
  },
  {
    "label": "已暂停采集器",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type BatchRecord = Record<string, string | number | unknown>

export default function MetricView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [batchRows, setBatchRows] = useState<BatchRecord[]>([])
  const [batchNo, setBatchNo] = useState('batch-29')
  const [collectorText, setCollectorText] = useState('METR-0001,METR-0002,METR-0003')
  const [expectedVersion, setExpectedVersion] = useState('')
  const [failedText, setFailedText] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('指标采集器列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流作业指标采集列表读取失败')
    }
  }, [filters])

  const reloadBatches = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/batch-watermarks`)
      if (!response.ok) {
        throw new Error('批次水位数据记录读取失败')
      }
      const payload = await response.json()
      setBatchRows(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批次水位数据记录读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadBatches()
  }, [reloadBatches])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('流作业指标采集动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流作业指标采集操作失败')
    }
  }

  async function pauseBatch() {
    setErrorMessage('')
    const collectorNos = collectorText.split(',').map((item) => item.trim()).filter(Boolean)
    const simulateFailed = failedText.split(',').map((item) => item.trim()).filter(Boolean)
    const values: Record<string, unknown> = { batch_no: batchNo.trim(), collector_nos: collectorNos }
    if (expectedVersion.trim()) {
      values.expected_version = Number(expectedVersion.trim())
    }
    if (simulateFailed.length) {
      values.simulate_failed = simulateFailed
    }
    try {
      const response = await request(`${ENDPOINT}/batch-pause`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '批次暂停未生效，请稍后重试')
      }
      setErrorMessage('')
      await Promise.all([reload(), reloadBatches()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批次暂停操作失败')
    }
  }

  return (
    <section className="page" data-module="metric">
      <header className="page-head">
        <div>
          <h2>流作业指标采集管理</h2>
          <p className="page-desc">维护流作业指标的采集间隔、聚合方式与保留策略，为流作业指标采集提供可审计的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('指标采集器登记入口尚未接入审批流')}>
            登记指标采集器
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出流作业指标采集清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form
        className="filter-bar"
        onSubmit={(event) => {
          event.preventDefault()
          void pauseBatch()
        }}
      >
        <label className="filter-item">
          <span>批次编号</span>
          <input value={batchNo} placeholder="如 batch-29" onChange={(event) => setBatchNo(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>采集器编号（逗号分隔）</span>
          <input value={collectorText} placeholder="METR-0001,METR-0002" onChange={(event) => setCollectorText(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>续做版本号（可选）</span>
          <input value={expectedVersion} placeholder="按最后确认水位带入" onChange={(event) => setExpectedVersion(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>模拟失败项（可选）</span>
          <input value={failedText} placeholder="METR-0003" onChange={(event) => setFailedText(event.target.value)} />
        </label>
        <button className="btn primary" type="submit">整组暂停并写批次水位</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>版本号</th>
            <th>批次状态</th>
            <th>已完成采集器</th>
            <th>失败项</th>
            <th>续做位置</th>
            <th>聚合口径</th>
            <th>提交次数</th>
          </tr>
        </thead>
        <tbody>
          {batchRows.map((row) => (
            <tr key={String(row.id)}>
              <td>{String(row.批次编号 ?? '—')}</td>
              <td>{String(row.版本号 ?? '—')}</td>
              <td>{String(row.批次状态 ?? '—')}</td>
              <td>{Array.isArray(row.已完成采集器) ? (row.已完成采集器 as string[]).join('、') : '—'}</td>
              <td>
                {Array.isArray(row.失败项) && (row.失败项 as Array<Record<string, string>>).length
                  ? (row.失败项 as Array<Record<string, string>>).map((item) => `${item.采集器编号}（${item.失败原因}）`).join('；')
                  : '—'}
              </td>
              <td>{String(row.续做位置 ?? '—')}</td>
              <td>
                {row.聚合口径 && typeof row.聚合口径 === 'object'
                  ? Object.entries(row.聚合口径 as Record<string, Record<string, string | number>>)
                      .map(([no, spec]) => `${no}:${spec.聚合方式}/${spec.保留天数}天`)
                      .join('、')
                  : '—'}
              </td>
              <td>{String(row.提交次数 ?? '—')}</td>
            </tr>
          ))}
          {batchRows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={8}>暂无批次水位数据记录，提交整组暂停后在此核对已完成采集器、失败项与续做位置</td>
            </tr>
          )}
        </tbody>
      </table>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无流作业指标采集数据，可先登记指标采集器</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条流作业指标采集记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
