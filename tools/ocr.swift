// OCR tại chỗ bằng Apple Vision: in từng dòng chữ nhận được của mỗi ảnh (tab: tên file \t chữ).
import Foundation
import Vision
import AppKit

for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["vi-VN", "en-US"]
    req.usesLanguageCorrection = false
    try? VNImageRequestHandler(cgImage: cg).perform([req])
    for obs in req.results ?? [] {
        if let t = obs.topCandidates(1).first?.string { print("\((path as NSString).lastPathComponent)\t\(t)") }
    }
}
