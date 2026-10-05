// Middy mic-state watcher (Lỗi 9b, option A 28/09): reports the labels of the meeting app's mic / mute controls through
// the Accessibility API so main can tell "unmuted in Teams/Zoom" (case 3: always take the mic).
// READ ONLY: only AXUIElementCopyAttributeValue (role, title, description, children, windows, menu bar). It never performs an
// action, never sets an attribute and never prompts for permission (AXIsProcessTrustedWithOptions prompt = false) — the prompt is
// the button in Middy's Settings. The decision (label -> muted / unmuted) is made in main/mic_state.js so it can be unit-tested.
//
// Usage: mic-state (--bundle <bundle id> | --pid <pid>) [--interval <s>]
// Output: one JSON line each time something changes:
//   {"trusted":bool,"running":bool,"controls":[{"role":"AXButton","label":"Unmute mic"}],"scan_ms":n,"nodes":n}
import AppKit
import ApplicationServices

let args = CommandLine.arguments
func arg(_ k: String) -> String? { if let i = args.firstIndex(of: k), i + 1 < args.count { return args[i + 1] }; return nil }
let bundle = arg("--bundle"), fixedPid = arg("--pid").flatMap { pid_t($0) }
let interval = arg("--interval").flatMap { Double($0) } ?? 1.0
let MAX_NODES = 4000, MAX_DEPTH = 60, SCAN_BUDGET_S = 0.3, RESCAN_EVERY_S = 3.0, MAX_CONTROLS = 8
// a control is a candidate when its label talks about mute / mic (EN + VI); main decides what it means
let candidate = try! NSRegularExpression(pattern: "(?i)\\b(un)?mute\\b|\\bmic\\b|micro|tắt tiếng|bật tiếng|micrô")
let menuHint = try! NSRegularExpression(pattern: "(?i)meeting|call|audio|cuộc họp|cuộc gọi|âm thanh")
let controlRoles: Set<String> = ["AXButton", "AXCheckBox", "AXMenuItem", "AXRadioButton", "AXPopUpButton", "AXMenuButton"]

func attr(_ e: AXUIElement, _ a: String) -> CFTypeRef? { var v: CFTypeRef?; return AXUIElementCopyAttributeValue(e, a as CFString, &v) == .success ? v : nil }
func label(_ e: AXUIElement) -> String {
  let parts = [kAXTitleAttribute, kAXDescriptionAttribute].compactMap { attr(e, $0) as? String }.filter { !$0.isEmpty }
  return parts.joined(separator: " | ")
}
func matches(_ re: NSRegularExpression, _ s: String) -> Bool { re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil }

func resolvePid() -> pid_t? {
  if let p = fixedPid { return kill(p, 0) == 0 ? p : nil }
  guard let b = bundle else { return nil }
  return NSRunningApplication.runningApplications(withBundleIdentifier: b).first?.processIdentifier
}

// bounded scan of the app's windows + the menus whose title hints at meeting/call/audio (menus are lazy and can be huge)
func scan(_ pid: pid_t) -> (cands: [AXUIElement], nodes: Int) {
  let app = AXUIElementCreateApplication(pid)
  AXUIElementSetMessagingTimeout(app, 0.25)
  let t0 = Date(); var nodes = 0; var found: [AXUIElement] = []
  func visit(_ e: AXUIElement, _ depth: Int) {
    if nodes >= MAX_NODES || depth > MAX_DEPTH || found.count >= MAX_CONTROLS || Date().timeIntervalSince(t0) > SCAN_BUDGET_S { return }
    nodes += 1
    let role = attr(e, kAXRoleAttribute) as? String ?? ""
    if role == "AXApplication" && depth > 0 { return }                     // some apps loop back to the app element
    if controlRoles.contains(role) && matches(candidate, label(e)) { found.append(e) }
    for k in attr(e, kAXChildrenAttribute) as? [AXUIElement] ?? [] where (attr(k, kAXRoleAttribute) as? String) != "AXMenuBar" { visit(k, depth + 1) }
  }
  for w in attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? [] { visit(w, 1) }
  if let bar = attr(app, kAXMenuBarAttribute) {
    for item in attr(bar as! AXUIElement, kAXChildrenAttribute) as? [AXUIElement] ?? [] where matches(menuHint, label(item)) { visit(item, 1) }
  }
  return (found, nodes)
}

var cached: [AXUIElement] = [], lastScan = Date.distantPast, lastLine = "", lastNodes = 0, lastScanMs = 0
func emit(_ o: [String: Any]) {
  let d = try! JSONSerialization.data(withJSONObject: o, options: [.sortedKeys]); let s = String(data: d, encoding: .utf8)!
  if s != lastLine { lastLine = s; print(s); fflush(stdout) }
}
while true {
  if !AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: false] as CFDictionary) {
    emit(["trusted": false, "running": resolvePid() != nil, "controls": []]); cached = []
    Thread.sleep(forTimeInterval: 5); continue                                 // re-check: the user may grant it while Middy runs
  }
  guard let pid = resolvePid() else { emit(["trusted": true, "running": false, "controls": []]); cached = []; Thread.sleep(forTimeInterval: 3); continue }
  var controls: [[String: String]] = []
  var stale = cached.isEmpty
  for e in cached {
    var role: CFTypeRef?
    if AXUIElementCopyAttributeValue(e, kAXRoleAttribute as CFString, &role) != .success { stale = true; break }   // element gone: rescan
    controls.append(["role": role as? String ?? "", "label": label(e)])
  }
  if stale && Date().timeIntervalSince(lastScan) >= (cached.isEmpty && lastScan != Date.distantPast ? RESCAN_EVERY_S : 0) {
    let t = Date(); let r = scan(pid); lastScan = Date()
    cached = r.cands; lastNodes = r.nodes; lastScanMs = Int(Date().timeIntervalSince(t) * 1000)
    controls = cached.map { ["role": attr($0, kAXRoleAttribute) as? String ?? "", "label": label($0)] }
  }
  emit(["trusted": true, "running": true, "controls": controls, "nodes": lastNodes, "scan_ms": lastScanMs])
  Thread.sleep(forTimeInterval: interval)
}
