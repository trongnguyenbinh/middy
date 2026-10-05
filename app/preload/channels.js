// What the renderer may reach in the main process (preload/index.js) and, through the 'daemon' channel, in the daemon
// (main/index.js). Anything else is refused: a renderer bug or injected script cannot quit the daemon, start a meeting
// behind the UI, or make the daemon write a Word file to an arbitrary path (export_docx goes through 'export:docx' and its
// save dialog). Add a channel / command here when the UI starts using it.
const INVOKE = new Set([
  'ax:open-settings', 'ax:request', 'ax:status', 'daemon', 'daemon-log', 'export:docx', 'export:file',
  'meeting-detected:accept', 'meeting-detected:dismiss', 'meeting-detected:get', 'meeting:ask', 'meeting:language',
  'meeting:start', 'meeting:state', 'meeting:stop', 'meeting:title', 'mic-state:get', 'net:status', 'output-device', 'quit',
  'reminder:end', 'reminder:get', 'reminder:keep', 'settings:get', 'settings:set', 'shortcut:get', 'shortcut:record',
  'shortcut:reset', 'shortcut:suspend', 'toolbar:capture', 'window:close', 'window:hide',
  'window:move', 'window:open', 'window:size',
])
const ON = new Set(['capture-control', 'event', 'meeting', 'meeting-detected-info', 'mic-state', 'net', 'reminder', 'settings',
  'shortcut', 'system-audio', 'ui'])
// daemon commands the renderer sends through the 'daemon' channel (Library, Preview, Overlay)
const DAEMON_CMDS = new Set(['delete_meeting', 'delete_meetings', 'meeting', 'meetings', 'save_note', 'search', 'set_name',
  'set_space', 'snapshot', 'spaces'])

const allowedDaemonRequest = (req) => !!req && typeof req === 'object' && DAEMON_CMDS.has(req.cmd)

module.exports = { INVOKE, ON, DAEMON_CMDS, allowedDaemonRequest }
