// Middy launcher: the bundle's main executable (a real Mach-O, so codesign/LaunchServices treat it as a normal app binary).
// It starts the Electron binary `Middy-bin` next to it under the kernel network sandbox (proto/nonet.sb) as a NEW process and
// exits, whatever way the app was opened (Finder, Dock, Spotlight, `open`, terminal). No network, no UI, no dependencies.
//
// Why spawn and not execv (Lỗi 8, measured 28/09 on macOS 26.6): when LaunchServices launches this launcher and the SAME pid then
// execv-s into another binary, Control Center parks that process's menu-bar item off-screen (NSStatusItem frame 0x0) — reproduced
// with a bare Swift status item too, with and without sandbox-exec, regular or accessory. A process started by the launcher checks
// in with LaunchServices by itself (bundle local.midy.app, Dock icon, single instance) and its tray is placed normally.
// Electron code still never runs outside the sandbox: the only unsandboxed code is this file.
import Foundation

let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
let dir = exe.deletingLastPathComponent().path
let env0 = ProcessInfo.processInfo.environment
let root = env0["MIDY_ROOT"] ?? NSHomeDirectory() + "/middy"   // the source checkout; MIDY_ROOT overrides it
// the sandbox profile matches RESOLVED paths: a symlinked root (~/middy -> elsewhere, /tmp -> /private/tmp) made every Unix socket
// bind in run/ fail with EPERM. realpath(3), not URL.resolvingSymlinksInPath (that one strips /private).
let midy = realpath(root, nil).map { p in defer { free(p) }; return String(cString: p) } ?? root
var env = env0
env["MIDY_SANDBOX"] = "1"
let p = Process()
p.executableURL = URL(fileURLWithPath: "/usr/bin/sandbox-exec")
p.arguments = ["-f", midy + "/proto/nonet.sb", "-D", "RUN=" + midy + "/run", dir + "/" + exe.lastPathComponent + "-bin"]  // "Middy" -> "Middy-bin"
  + CommandLine.arguments.dropFirst()
p.environment = env
p.standardInput = FileHandle.nullDevice
let logPath = midy + "/run/midy_child.log"                                      // a GUI launch has no usable stdio
if !FileManager.default.fileExists(atPath: logPath) { FileManager.default.createFile(atPath: logPath, contents: nil, attributes: [.posixPermissions: 0o600]) }
if let log = FileHandle(forWritingAtPath: logPath) { log.seekToEndOfFile(); p.standardOutput = log; p.standardError = log }
do { try p.run() } catch {
  perror("midy launcher: cannot start sandbox-exec")                          // running unsandboxed is NOT an option
  exit(1)
}
exit(0)
