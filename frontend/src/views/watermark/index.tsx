import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/watermark'
const columns = [
  "配置编号",
  "流名称",
  "时间字段",
  "延迟容忍",
  "空闲超时",
  "发射间隔",
  "对齐策略",
  "配置状态"
]
const actions = [
  "启用水位线",
  "暂停水位线",
  "关闭水位线"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "水位线配置总数",
    "value": 0
  },
  {
    "label": "已启用配置",
    "value": 0
  },
  {
    "label": "已暂停配置",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type BatchRecord = Record<string, string | number | unknown>

export default function WatermarkView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [batchRows, setBatchRows] = useState<BatchRecord[]>([])
  const [batchNo, setBatchNo] = useState('')
  const [expectedVersion, setExpectedVersion] = useState('')
  const [remark, setRemark] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('水位线配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '水位线管理列表读取失败')
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
        throw new Error('水位线管理动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '水位线管理操作失败')
    }
  }

  async function syncBatch() {
    setErrorMessage('')
    if (!expectedVersion.trim()) {
      setErrorMessage('同步写入批次水位必须带入版本号 expected_version')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/batch-watermarks/sync`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            batch_no: batchNo.trim(),
            expected_version: Number(expectedVersion.trim()),
            批次备注: remark.trim(),
          },
        }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '同步写入未生效，请确认版本号')
      }
      await reloadBatches()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批次水位同步写入失败')
    }
  }

  return (
    <section className="page" data-module="watermark">
      <header className="page-head">
        <div>
          <h2>水位线管理管理</h2>
          <p className="page-desc">维护水位线的延迟容忍、空闲超时与发射策略，为水位线管理提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('水位线配置登记入口尚未接入审批流')}>
            登记水位线配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出水位线管理清单
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
          void syncBatch()
        }}
      >
        <label className="filter-item">
          <span>同步批次编号</span>
          <input value={batchNo} placeholder="如 batch-29" onChange={(event) => setBatchNo(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>带入版本号</span>
          <input value={expectedVersion} placeholder="读取记录后填入当前版本号" onChange={(event) => setExpectedVersion(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>批次备注（白名单字段）</span>
          <input value={remark} placeholder="仅备注可跨模块写入" onChange={(event) => setRemark(event.target.value)} />
        </label>
        <button className="btn primary" type="submit">按版本号同步写入批次水位</button>
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
            <th>批次备注</th>
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
                  ? (row.失败项 as Array<Record<string, string>>).map((item) => item.采集器编号).join('、')
                  : '—'}
              </td>
              <td>{String(row.续做位置 ?? '—')}</td>
              <td>{String(row.批次备注 || '—')}</td>
            </tr>
          ))}
          {batchRows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={7}>暂无跨模块批次水位数据记录</td>
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无水位线管理数据，可先登记水位线配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条水位线管理记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
