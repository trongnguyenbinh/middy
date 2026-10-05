// Tiny markdown <-> HTML for notes (headings, bullets, numbered, task items, tables, bold/italic/code). No dependency.
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|\W)\*(.+?)\*(?=\W|$)/g, '$1<em>$2</em>').replace(/`([^`]+)`/g, '<code>$1</code>')

export function mdToHtml(md) {
  const lines = (md || '').replace(/\r/g, '').split('\n')
  const out = []
  let list = null, table = null, para = []
  const flushPara = () => { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = [] } }
  const closeList = () => { if (list) { out.push(list === 'ul' ? '</ul>' : list === 'ol' ? '</ol>' : '</ul>'); list = null } }
  const closeTable = () => { if (table) { out.push('</tbody></table>'); table = null } }
  for (const raw of lines) {
    const l = raw.trimEnd()
    let m
    if (!l.trim()) { flushPara(); closeList(); closeTable(); continue }
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { flushPara(); closeList(); closeTable(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); continue }
    if ((m = l.match(/^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/))) { flushPara(); closeTable(); if (list !== 'task') { closeList(); out.push('<ul data-type="taskList">'); list = 'task' } out.push(`<li data-type="taskItem" data-checked="${m[1] !== ' '}">${inline(m[2])}</li>`); continue }
    if ((m = l.match(/^\s*[-*•]\s+(.*)$/))) { flushPara(); closeTable(); if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul' } out.push('<li>' + inline(m[1]) + '</li>'); continue }
    if ((m = l.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); closeTable(); if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol' } out.push('<li>' + inline(m[1]) + '</li>'); continue }
    if (/^\s*\|.*\|\s*$/.test(l)) {
      flushPara(); closeList()
      const cells = l.trim().slice(1, -1).split('|').map((c) => c.trim())
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue
      if (!table) { out.push('<table><tbody>'); table = 'head'; out.push('<tr>' + cells.map((c) => '<th>' + inline(c) + '</th>').join('') + '</tr>'); table = 'body' }
      else out.push('<tr>' + cells.map((c) => '<td>' + inline(c) + '</td>').join('') + '</tr>')
      continue
    }
    closeList(); closeTable(); para.push(l.trim())
  }
  flushPara(); closeList(); closeTable()
  return out.join('\n')
}

export function htmlToMd(html) {
  const doc = new DOMParser().parseFromString(html || '', 'text/html')
  const txt = (n) => {
    let s = ''
    for (const c of n.childNodes) {
      if (c.nodeType === 3) s += c.textContent
      else if (c.nodeName === 'STRONG' || c.nodeName === 'B') s += '**' + txt(c) + '**'
      else if (c.nodeName === 'EM' || c.nodeName === 'I') s += '*' + txt(c) + '*'
      else if (c.nodeName === 'CODE') s += '`' + txt(c) + '`'
      else if (c.nodeName === 'BR') s += '\n'
      else s += txt(c)
    }
    return s
  }
  const out = []
  const walk = (n, depth = 0) => {
    for (const c of n.children) {
      const t = c.nodeName
      if (/^H[1-6]$/.test(t)) out.push('#'.repeat(Number(t[1])) + ' ' + txt(c).trim(), '')
      else if (t === 'P') out.push(txt(c).trim(), '')
      else if (t === 'UL' || t === 'OL') {
        let i = 1
        for (const li of c.children) {
          if (li.nodeName !== 'LI') continue
          const own = Array.from(li.childNodes).filter((x) => !['UL', 'OL'].includes(x.nodeName)).map((x) => x.nodeType === 3 ? x.textContent : txt(x)).join('').trim()
          const task = li.getAttribute('data-type') === 'taskItem' ? (li.getAttribute('data-checked') === 'true' ? '[x] ' : '[ ] ') : ''
          out.push('  '.repeat(depth) + (t === 'OL' ? `${i++}. ` : '- ') + task + own)
          for (const sub of li.children) if (['UL', 'OL'].includes(sub.nodeName)) walk({ children: [sub] }, depth + 1)
        }
        if (!depth) out.push('')
      } else if (t === 'TABLE') {
        const rows = Array.from(c.querySelectorAll('tr'))
        rows.forEach((r, i) => {
          const cells = Array.from(r.children).map((x) => txt(x).trim())
          out.push('| ' + cells.join(' | ') + ' |')
          if (i === 0) out.push('|' + cells.map(() => ' --- ').join('|') + '|')
        })
        out.push('')
      } else if (t === 'BLOCKQUOTE') out.push('> ' + txt(c).trim(), '')
      else if (t === 'PRE') out.push('```', txt(c), '```', '')
      else walk(c, depth)
    }
  }
  walk(doc.body)
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim() + '\n'
}
