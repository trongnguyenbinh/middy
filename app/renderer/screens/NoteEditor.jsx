// TipTap editor (Notes tab / preview / library): markdown shortcuts come with StarterKit (# heading, - bullet,
// 1. ordered) and TaskList ([ ] todo). `html` controlled from outside when the AI writes; user edits reported as HTML.
import React, { useEffect } from 'react'
import { useEditor, EditorContent } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import TaskList from '@tiptap/extension-task-list'
import TaskItem from '@tiptap/extension-task-item'
import { Table } from '@tiptap/extension-table'
import { TableRow } from '@tiptap/extension-table-row'
import { TableHeader } from '@tiptap/extension-table-header'
import { TableCell } from '@tiptap/extension-table-cell'

export function NoteEditor({ html, readOnly, placeholder, onChange }) {
  const editor = useEditor({
    extensions: [StarterKit, TaskList, TaskItem.configure({ nested: true }), Table, TableRow, TableHeader, TableCell, Placeholder.configure({ placeholder: placeholder || 'Type your notes here...' })],
    content: html || '',
    editable: !readOnly,
    onUpdate: ({ editor }) => onChange && onChange(editor.getHTML()),
  })
  useEffect(() => { if (editor) editor.setEditable(!readOnly) }, [editor, readOnly])
  useEffect(() => { if (editor && html !== undefined && html !== editor.getHTML()) editor.commands.setContent(html, { emitUpdate: false }) }, [editor, html])
  return <EditorContent editor={editor} className="tiptap-wrap" />
}
