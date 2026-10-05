// Việc 20: SoC temperatures without sudo — the IOHIDEventSystemClient temperature services (what menu-bar monitors read),
// plus ProcessInfo.thermalState. Prints one JSON line: {"sensors":{name:°C},"thermal_state":"nominal|fair|serious|critical"}.
// Build: swiftc -O -o tools/thermal tools/thermal.swift
import Foundation
import IOKit

@_silgen_name("IOHIDEventSystemClientCreate") func IOHIDEventSystemClientCreate(_ a: CFAllocator?) -> Unmanaged<AnyObject>?
@_silgen_name("IOHIDEventSystemClientSetMatching") func IOHIDEventSystemClientSetMatching(_ c: AnyObject, _ m: CFDictionary) -> Int32
@_silgen_name("IOHIDEventSystemClientCopyServices") func IOHIDEventSystemClientCopyServices(_ c: AnyObject) -> Unmanaged<CFArray>?
@_silgen_name("IOHIDServiceClientCopyProperty") func IOHIDServiceClientCopyProperty(_ s: AnyObject, _ k: CFString) -> Unmanaged<AnyObject>?
@_silgen_name("IOHIDServiceClientCopyEvent") func IOHIDServiceClientCopyEvent(_ s: AnyObject, _ t: Int64, _ a: Int32, _ b: Int64) -> Unmanaged<AnyObject>?
@_silgen_name("IOHIDEventGetFloatValue") func IOHIDEventGetFloatValue(_ e: AnyObject, _ f: Int32) -> Double

let kTemperature: Int64 = 15
var sensors: [String: Double] = [:]
if let client = IOHIDEventSystemClientCreate(kCFAllocatorDefault)?.takeRetainedValue() {
  _ = IOHIDEventSystemClientSetMatching(client, ["PrimaryUsagePage": 0xff00, "PrimaryUsage": 5] as CFDictionary)
  if let services = IOHIDEventSystemClientCopyServices(client)?.takeRetainedValue() as? [AnyObject] {
    for s in services {
      let name = (IOHIDServiceClientCopyProperty(s, "Product" as CFString)?.takeRetainedValue() as? String) ?? "?"
      if let ev = IOHIDServiceClientCopyEvent(s, kTemperature, 0, 0)?.takeRetainedValue() {
        let v = IOHIDEventGetFloatValue(ev, Int32(kTemperature << 16))
        if v > 0 && v < 150 { sensors[name] = max(sensors[name] ?? 0, (v * 10).rounded() / 10) }
      }
    }
  }
}
let states = ["nominal", "fair", "serious", "critical"]
let st = states[min(ProcessInfo.processInfo.thermalState.rawValue, 3)]
let data = try! JSONSerialization.data(withJSONObject: ["sensors": sensors, "thermal_state": st], options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
