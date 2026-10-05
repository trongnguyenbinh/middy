// Middy meeting probe (Lỗi 7): one-shot JSON snapshot of who is using the mic / speaker, in the SAME shape the reference app's
// `meeting-probe` prints (as the reference app parses it): {"mic","speaker","frontmostBundleId","frontmostName","processes":[...]}.
// Like the reference app's binary it only uses NSWorkspace + CoreAudio AudioObjectGetPropertyData:
// no TCC permission, no network, no audio is read. Per-process flags use the CoreAudio process objects (macOS 14.2+).
import AppKit
import CoreAudio

func getU32(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
  var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
  var v: UInt32 = 0; var size = UInt32(MemoryLayout<UInt32>.size)
  return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &v) == noErr ? v : nil
}
func defaultDeviceRunning(_ sel: AudioObjectPropertySelector) -> String {
  guard let dev = getU32(AudioObjectID(kAudioObjectSystemObject), sel), dev != 0 else { return "error" }
  guard let running = getU32(dev, kAudioDevicePropertyDeviceIsRunningSomewhere) else { return "error" }
  return running != 0 ? "active" : "inactive"
}
func processObjects() -> [AudioObjectID] {
  var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
  var size: UInt32 = 0
  guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
  var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
  guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr else { return [] }
  return ids
}
func bundleId(_ obj: AudioObjectID) -> String {
  var addr = AudioObjectPropertyAddress(mSelector: kAudioProcessPropertyBundleID, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
  var s: Unmanaged<CFString>?; var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
  guard AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &s) == noErr, let v = s?.takeRetainedValue() else { return "" }
  return v as String
}

var processes: [[String: Any]] = []
for obj in processObjects() {
  let bid = bundleId(obj)
  if bid.isEmpty { continue }                                          // the reference app's parser drops entries without a bundle id
  let pid = Int(Int32(bitPattern: getU32(obj, kAudioProcessPropertyPID) ?? 0))
  let mic = (getU32(obj, kAudioProcessPropertyIsRunningInput) ?? 0) != 0
  let spk = (getU32(obj, kAudioProcessPropertyIsRunningOutput) ?? 0) != 0
  processes.append(["pid": pid, "bundleId": bid, "mic": mic, "speaker": spk])
}
let front = NSWorkspace.shared.frontmostApplication
let out: [String: Any] = [
  "mic": defaultDeviceRunning(kAudioHardwarePropertyDefaultInputDevice),
  "speaker": defaultDeviceRunning(kAudioHardwarePropertyDefaultOutputDevice),
  "frontmostBundleId": front?.bundleIdentifier ?? "",
  "frontmostName": front?.localizedName ?? "",
  "processes": processes,
]
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data); FileHandle.standardOutput.write("\n".data(using: .utf8)!)
