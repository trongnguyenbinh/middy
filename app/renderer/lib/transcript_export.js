// Original transcript export (bug 3, 27/09 22:59): every final segment with [hh:mm:ss] + speaker, in .md or .txt.
const hms = (s) => { s = Math.max(0, Math.floor(s)); return String(Math.floor(s / 3600)).padStart(2, '0') + ':' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0') }

export function transcriptText(meeting, segments, ext) {
  const dur = hms(meeting.audio_end_s || 0)
  const when = new Date((meeting.started_at || 0) * 1000).toLocaleString()
  const lines = (segments || []).map((s) => {
    const spk = s.speaker || 'Speaker ?'
    const body = s.dropped_lang ? `[dropped: ~${Math.round(s.e - s.s)} s in another language]` : s.text
    return ext === 'md' ? `**[${hms(s.s)}] ${spk}:** ${body}` : `[${hms(s.s)}] ${spk}: ${body}`
  })
  const head = ext === 'md'
    ? `# ${meeting.name} — transcript\n\n${when} · ${meeting.language} · ${dur} · ${(segments || []).length} segments · Middy (local, original ASR text, not edited)\n\n`
    : `${meeting.name} — transcript\n${when} · ${meeting.language} · ${dur} · ${(segments || []).length} segments · Middy (local, original ASR text, not edited)\n\n`
  return head + lines.join(ext === 'md' ? '\n\n' : '\n') + '\n'
}
