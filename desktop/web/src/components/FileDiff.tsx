export type DiffRow = { kind: 'same' | 'added' | 'removed'; text: string; oldLine?: number; newLine?: number }

/** LCS line diff, capped to bound CPU and DOM costs on untrusted workspace text. */
export function lineDiff(before: string, after: string, maxLines = 250): DiffRow[] | null {
  if (before === after) return []
  const a = before.split('\n')
  const b = after.split('\n')
  if (a.length > maxLines || b.length > maxLines) return null
  const matrix: Uint16Array[] = Array.from({ length: a.length + 1 },
    () => new Uint16Array(b.length + 1))
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      matrix[i][j] = a[i] === b[j] ? 1 + matrix[i + 1][j + 1]
        : Math.max(matrix[i + 1][j], matrix[i][j + 1])
    }
  }
  const rows: DiffRow[] = []
  let i = 0, j = 0
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      rows.push({ kind: 'same', text: a[i], oldLine: ++i, newLine: ++j })
    } else if (i < a.length && (j === b.length || matrix[i + 1][j] >= matrix[i][j + 1])) {
      rows.push({ kind: 'removed', text: a[i], oldLine: ++i })
    } else {
      rows.push({ kind: 'added', text: b[j], newLine: ++j })
    }
  }
  return rows
}

export default function FileDiff({ before, after }: { before: string; after: string }) {
  const rows = lineDiff(before, after)
  if (rows === null) return <p className="p-4 text-sm text-amber-700">文件超过 250 行，预览对比已禁用以限制浏览器资源消耗。</p>
  if (rows.length === 0) return <p className="p-4 text-sm text-gray-500">与记录的基线相同，尚无变化。</p>
  const added = rows.filter(row => row.kind === 'added').length
  const removed = rows.filter(row => row.kind === 'removed').length
  return <div aria-label="文件快照对比">
    <div className="flex gap-4 border-b border-gray-200 p-3 text-xs">
      <span className="text-green-700">+{added} 行</span>
      <span className="text-red-700">-{removed} 行</span>
    </div>
    <div className="overflow-x-auto max-h-[62vh] overflow-y-auto font-mono text-xs">
      {rows.map((row, index) => (
        <div key={index} className={'diff-line diff-' + row.kind}>
          <span className="diff-number">{row.oldLine ?? ''}</span>
          <span className="diff-number">{row.newLine ?? ''}</span>
          <span className="diff-prefix">{row.kind === 'added' ? '+' : row.kind === 'removed' ? '-' : ' '}</span>
          <span className="whitespace-pre">{row.text || ' '}</span>
        </div>
      ))}
    </div>
  </div>
}
