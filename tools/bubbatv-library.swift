import Foundation
import UniformTypeIdentifiers

/// The launcher expands only these video types when given a season folder.
let folderVideoExtensions: Set<String> = ["mkv", "mp4", "m4v", "mov", "avi", "webm"]

func mediaExists(_ path: String) -> Bool {
    var directory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: path, isDirectory: &directory) else { return false }
    if directory.boolValue { return true }
    let ext = URL(fileURLWithPath: path).pathExtension.lowercased()
    if folderVideoExtensions.contains(ext) { return true }
    guard let type = UTType(filenameExtension: ext) else { return false }
    return type.conforms(to: .movie) || type.conforms(to: .audio)
}

func normalizedMedia(_ paths: [String]) -> [String] {
    var seen = Set<String>()
    return paths.map { URL(fileURLWithPath: $0).standardizedFileURL.path }
        .filter { mediaExists($0) && seen.insert($0).inserted }
}

/// A small history of opened files/folders; unavailable items stay on disk so
/// reconnecting an external drive brings them back, but they are hidden in UI.
struct RecentItem: Codable, Equatable {
    let path: String
    let openedAt: Date
}

final class RecentMedia {
    let file: URL
    private(set) var items: [RecentItem]

    init(file: URL) {
        self.file = file
        items = (try? JSONDecoder().decode([RecentItem].self, from: Data(contentsOf: file))) ?? []
    }

    var available: [RecentItem] { items.filter { mediaExists($0.path) } }

    func remember(_ paths: [String], at date: Date = Date()) throws {
        let paths = normalizedMedia(paths)
        guard !paths.isEmpty else { return }
        let new = paths.map { RecentItem(path: $0, openedAt: date) }
        try save(Array((new + items.filter { !paths.contains($0.path) }).prefix(20)))
    }

    func remove(_ path: String) throws { try save(items.filter { $0.path != path }) }
    func clear() throws { try save([]) }

    private func save(_ value: [RecentItem]) throws {
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(value).write(to: file, options: .atomic)
        items = value
    }
}

/// The Lua plugin writes this before unloading the current movie. Keep the
/// current file's identity when earlier playlist entries have disappeared.
struct LastPlayback: Decodable {
    let playlist: [String]
    let index: Int
    let title: String?
    let time: Double?
    let finished: Bool?

    static func load(from file: URL) -> LastPlayback? {
        guard let data = try? Data(contentsOf: file),
              let value = try? JSONDecoder().decode(Self.self, from: data),
              value.playlist.indices.contains(value.index) else { return nil }
        return value
    }

    var current: String { playlist[index] }
    var canResume: Bool { finished != true && mediaExists(current) }
    var files: [String] { playlist.filter { mediaExists($0) } }
    var resumeIndex: Int { playlist.prefix(index).filter { mediaExists($0) }.count }
    var displayTitle: String { title ?? URL(fileURLWithPath: current).deletingPathExtension().lastPathComponent }
    var position: String {
        guard let time, time.isFinite, time > 0 else { return "From the beginning" }
        let seconds = Int(min(time, Double(Int32.max)))
        return seconds >= 3600
            ? String(format: "%d:%02d:%02d", seconds / 3600, seconds / 60 % 60, seconds % 60)
            : String(format: "%d:%02d", seconds / 60, seconds % 60)
    }
}

/// Match dualplay's folder rules when adding more media to an already running
/// player's playlist. Initial playback still goes through dualplay itself.
func queuedMedia(_ paths: [String]) throws -> [String] {
    var result: [String] = []
    for path in normalizedMedia(paths) {
        let url = URL(fileURLWithPath: path)
        var directory: ObjCBool = false
        FileManager.default.fileExists(atPath: path, isDirectory: &directory)
        if directory.boolValue {
            let files = try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: [.isRegularFileKey],
                                                                    options: [.skipsHiddenFiles])
                .filter { folderVideoExtensions.contains($0.pathExtension.lowercased())
                    && (try? $0.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true }
                .sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }
            guard !files.isEmpty else {
                throw NSError(domain: "bubbaTV", code: 1, userInfo: [NSLocalizedDescriptionKey: "No videos in \(url.lastPathComponent)."])
            }
            result += files.map(\.path)
        } else { result.append(path) }
    }
    return result
}

/// A short-lived local mpv IPC connection. Used only to append to the existing
/// playlist; the player's own controls remain responsible for playback.
func appendToPlayer(_ files: [String], socketPath: String) throws {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { throw POSIXError(.EIO) }
    defer { close(fd) }
    var noSignal: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout.size(ofValue: noSignal)))
    var timeout = timeval(tv_sec: 3, tv_usec: 0)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    let bytes = socketPath.utf8CString
    guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw POSIXError(.ENAMETOOLONG) }
    withUnsafeMutableBytes(of: &address.sun_path) { target in
        for (i, byte) in bytes.enumerated() { target[i] = UInt8(bitPattern: byte) }
    }
    let deadline = Date().addingTimeInterval(20)
    while true {
        let connected = withUnsafePointer(to: &address) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if connected == 0 { break }
        guard Date() < deadline && (errno == ENOENT || errno == ECONNREFUSED) else { throw POSIXError(.ECONNREFUSED) }
        Thread.sleep(forTimeInterval: 0.1)
    }
    var buffer = Data()
    for (id, file) in files.enumerated() {
        var data = try JSONSerialization.data(withJSONObject: ["command": ["loadfile", file, "append"], "request_id": id])
        data.append(10)
        try data.withUnsafeBytes { raw in
            var sent = 0
            while sent < raw.count {
                let n = write(fd, raw.baseAddress!.advanced(by: sent), raw.count - sent)
                guard n > 0 else { throw POSIXError(.EPIPE) }
                sent += n
            }
        }
        var replied = false
        while !replied {
            if let end = buffer.firstIndex(of: 10) {
                let line = buffer.prefix(upTo: end)
                buffer.removeSubrange(...end)
                if let reply = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                   reply["request_id"] as? Int == id {
                    guard reply["error"] as? String == "success" else { throw POSIXError(.EIO) }
                    replied = true
                }
            } else {
                var chunk = [UInt8](repeating: 0, count: 4096)
                let n = read(fd, &chunk, chunk.count)
                guard n > 0 else { throw POSIXError(.ECONNRESET) }
                buffer.append(contentsOf: chunk.prefix(n))
                guard buffer.count < 1_048_576 else { throw POSIXError(.EIO) }
            }
        }
    }
}
