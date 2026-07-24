export function statusLabel(job) {
  if (job.status === 'done') {
    return job.degraded ? '已完成（部分分析能力降级）' : '已完成';
  }
  if (job.status === 'failed') return '失败';
  return '生成中 · ' + (job.progress || 0) + '%';
}

export function statusClass(job) {
  if (job.status === 'failed') return 'failed';
  if (job.status === 'done') return 'done';
  return 'generating';
}
