import Foundation
import Vision
import ImageIO

// 单个本地图片输入，返回实际OCR结果；不读取远程地址。
do {
    guard CommandLine.arguments.count == 2 else {
        throw NSError(domain: "RecipeOCR", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "需要一个本地图片路径"])
    }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any] else {
        throw NSError(domain: "RecipeOCR", code: 2,
                      userInfo: [NSLocalizedDescriptionKey: "无法解码图片"])
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    let supported = try request.supportedRecognitionLanguages()
    request.recognitionLanguages = ["zh-Hans", "en-US"].filter { supported.contains($0) }
    try VNImageRequestHandler(url: url, options: [:]).perform([request])
    let observations = (request.results ?? []).compactMap { observation -> [String: Any]? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": candidate.string, "confidence": candidate.confidence,
                "bbox": ["x": box.origin.x, "y": box.origin.y,
                         "width": box.width, "height": box.height]]
    }
    let result: [String: Any] = [
        "engine": "Apple Vision VNRecognizeTextRequest", "revision": request.revision,
        "recognition_languages": request.recognitionLanguages,
        "language_correction": false,
        "width": props[kCGImagePropertyPixelWidth as String] ?? 0,
        "height": props[kCGImagePropertyPixelHeight as String] ?? 0,
        "exif_orientation": props[kCGImagePropertyOrientation as String] ?? 1,
        "bbox_coordinates": "normalized_0_1_bottom_left_in_oriented_image",
        "observations": observations]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("OCR失败：\(error.localizedDescription)\n".utf8))
    exit(1)
}
