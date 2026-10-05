// Midy audio capture helper (design ②): local microphone + system audio (CoreAudio process tap, macOS >= 14.2).
// Writes 100 ms float32 16 kHz mono frames to stdout in the Midy frame protocol (proto/common.py):
//   header <BddI> = stream id (0 = system audio / far end, 1 = mic), seconds since start at frame end, wall clock, n samples.
// No network, no files. Needs the Microphone and System Audio Recording permissions (TCC) for the host process.
// Build: swiftc -O -o tools/audiocap tools/audiocap.swift -framework CoreAudio -framework AVFoundation
import AVFoundation
import CoreAudio
import Foundation

let micOnly = CommandLine.arguments.contains("--mic-only")   // mic through AVAudioEngine only; system audio comes from audiotee
let sampleRate = 16000.0
let frameSamples = 1600            // 100 ms
let t0 = Date()
let out = FileHandle.standardOutput
let lock = NSLock()

func emit(stream: UInt8, samples: [Float], tEnd: Double) {
    var hdr = Data()
    hdr.append(stream)
    var te = tEnd, now = Date().timeIntervalSince1970, n = UInt32(samples.count)
    hdr.append(Data(bytes: &te, count: 8)); hdr.append(Data(bytes: &now, count: 8)); hdr.append(Data(bytes: &n, count: 4))
    lock.lock(); out.write(hdr); samples.withUnsafeBufferPointer { out.write(Data(buffer: $0)) }; lock.unlock()
}

/// Accumulates converted samples and emits fixed 100 ms frames.
final class Framer {
    let stream: UInt8
    var buf: [Float] = []
    var produced = 0
    init(_ s: UInt8) { stream = s }
    func push(_ x: [Float]) {
        buf.append(contentsOf: x)
        while buf.count >= frameSamples {
            let f = Array(buf[0..<frameSamples]); buf.removeFirst(frameSamples); produced += frameSamples
            emit(stream: stream, samples: f, tEnd: Double(produced) / sampleRate)
        }
    }
}

let outFmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!

func convert(_ inBuf: AVAudioPCMBuffer, _ conv: AVAudioConverter) -> [Float] {
    let cap = AVAudioFrameCount(Double(inBuf.frameLength) * sampleRate / inBuf.format.sampleRate) + 16
    guard let o = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return [] }
    var done = false
    var err: NSError?
    conv.convert(to: o, error: &err) { _, status in
        if done { status.pointee = .noDataNow; return nil }
        done = true; status.pointee = .haveData; return inBuf
    }
    guard err == nil, let p = o.floatChannelData else { return [] }
    return Array(UnsafeBufferPointer(start: p[0], count: Int(o.frameLength)))
}

// ---- 1. microphone (AVAudioEngine) -------------------------------------------------------------------------
let engine = AVAudioEngine()
let micNode = engine.inputNode
let micFmt = micNode.outputFormat(forBus: 0)
let micConv = AVAudioConverter(from: micFmt, to: outFmt)!
let micFramer = Framer(1)
micNode.installTap(onBus: 0, bufferSize: 1600, format: micFmt) { buf, _ in micFramer.push(convert(buf, micConv)) }

// ---- 2. system audio: process tap on every process -> private aggregate device -> IO proc -----------------
if micOnly {
    do { try engine.start() } catch { FileHandle.standardError.write("mic start failed \(error)\n".data(using: .utf8)!); exit(6) }
    FileHandle.standardError.write("audiocap running (mic only): mic \(micFmt.sampleRate) Hz\n".data(using: .utf8)!)
    signal(SIGTERM) { _ in exit(0) }
    signal(SIGINT) { _ in exit(0) }
    DispatchQueue.global().async { _ = FileHandle.standardInput.readDataToEndOfFile(); exit(0) }
    RunLoop.main.run()
}
let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDesc.uuid = UUID()
tapDesc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
var st = AudioHardwareCreateProcessTap(tapDesc, &tapID)
guard st == noErr else { FileHandle.standardError.write("tap create failed \(st)\n".data(using: .utf8)!); exit(2) }

let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Midy Tap",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: tapDesc.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
st = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
guard st == noErr else { FileHandle.standardError.write("aggregate device failed \(st)\n".data(using: .utf8)!); exit(3) }

// tap stream format
var fmtAddr = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
var asbd = AudioStreamBasicDescription()
var sz = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
st = AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &sz, &asbd)
guard st == noErr, let tapFmt = AVAudioFormat(streamDescription: &asbd) else { FileHandle.standardError.write("tap format failed \(st)\n".data(using: .utf8)!); exit(4) }
let sysConv = AVAudioConverter(from: tapFmt, to: outFmt)!
let sysFramer = Framer(0)

var procID: AudioDeviceIOProcID?
st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inData, _, _, _ in
    let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    guard let first = abl.first, first.mDataByteSize > 0 else { return }
    let frames = AVAudioFrameCount(first.mDataByteSize) / UInt32(max(1, tapFmt.streamDescription.pointee.mBytesPerFrame))
    guard let buf = AVAudioPCMBuffer(pcmFormat: tapFmt, bufferListNoCopy: inData, deallocator: nil) else { return }
    buf.frameLength = frames
    sysFramer.push(convert(buf, sysConv))
}
guard st == noErr else { FileHandle.standardError.write("io proc failed \(st)\n".data(using: .utf8)!); exit(5) }

// ---- run until stdin closes or SIGTERM ------------------------------------------------------------------------
do { try engine.start() } catch { FileHandle.standardError.write("mic start failed \(error)\n".data(using: .utf8)!); exit(6) }
st = AudioDeviceStart(aggID, procID)
guard st == noErr else { FileHandle.standardError.write("device start failed \(st)\n".data(using: .utf8)!); exit(7) }
FileHandle.standardError.write("audiocap running: mic \(micFmt.sampleRate) Hz, tap \(tapFmt.sampleRate) Hz\n".data(using: .utf8)!)

signal(SIGTERM) { _ in exit(0) }
signal(SIGINT) { _ in exit(0) }
atexit {
    AudioDeviceStop(aggID, procID)
    if let p = procID { AudioDeviceDestroyIOProcID(aggID, p) }
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
}
DispatchQueue.global().async { _ = FileHandle.standardInput.readDataToEndOfFile(); exit(0) }
RunLoop.main.run()
