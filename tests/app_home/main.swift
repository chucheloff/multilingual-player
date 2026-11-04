// Native model/window checks. Compiled with the app sources and its @main
// disabled; a fixture bundle supplies a fake dualplay and an isolated profile.
import Cocoa

let args = CommandLine.arguments
let mode = args[1]
let root = URL(fileURLWithPath: args[2], isDirectory: true)
func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fputs("FAIL: \(message)\n", stderr); exit(1) }
}
func touch(_ name: String) -> String {
    let file = root.appendingPathComponent(name)
    try! FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
    FileManager.default.createFile(atPath: file.path, contents: Data())
    return file.path
}
func ui<T>(_ body: @MainActor () -> T) -> T {
    DispatchQueue.main.sync { MainActor.assumeIsolated(body) }
}
func wait(_ description: String, _ predicate: () -> Bool) {
    let deadline = Date().addingTimeInterval(8)
    while Date() < deadline {
        if predicate() { return }
        Thread.sleep(forTimeInterval: 0.03)
    }
    fputs("FAIL: timed out waiting for \(description)\n", stderr)
    exit(1)
}

if mode == "history" {
    let file = root.appendingPathComponent("profile/recent.json")
    let history = RecentMedia(file: file)
    check(history.available.isEmpty, "new history is empty")
    let first = touch("first movie.mkv"), second = touch("second.MP4")
    try! history.remember([first, second, first], at: Date(timeIntervalSince1970: 10))
    check(history.items.map(\.path) == [first, second], "history deduplicates")
    try! history.remember([second], at: Date(timeIntervalSince1970: 20))
    check(history.items.map(\.path) == [second, first], "replayed file moves to top")
    check(RecentMedia(file: file).items == history.items, "history survives restart")
    try! FileManager.default.removeItem(atPath: second)
    check(history.available.map(\.path) == [first], "missing files are hidden")
    _ = touch("second.MP4")
    check(history.available.count == 2, "restored files reappear")
    try! history.remove(first)
    check(FileManager.default.fileExists(atPath: first), "removing history keeps files")
    for n in 0..<25 { try! history.remember([touch("movie-\(n).mkv")]) }
    check(history.items.count == 20, "history is bounded")
    try! history.clear()
    check(RecentMedia(file: file).available.isEmpty, "clearing survives restart")
    try! Data("broken json".utf8).write(to: file)
    check(RecentMedia(file: file).available.isEmpty, "invalid history is tolerated")
    print("ok history")
    exit(0)
}
if mode == "resume" {
    let missing = root.appendingPathComponent("missing.mkv").path
    let current = touch("Episode 2.mkv"), next = touch("Episode 3.mkv")
    let file = root.appendingPathComponent("last.json")
    func save(_ object: [String: Any]) {
        try! JSONSerialization.data(withJSONObject: object).write(to: file)
    }
    save(["playlist": [missing, current, next], "index": 1, "time": 83, "title": "Episode 2"])
    let last = LastPlayback.load(from: file)!
    check(last.canResume, "current file can resume")
    check(last.files == [current, next] && last.resumeIndex == 0, "removed earlier file does not change the resume target")
    check(last.position == "1:23", "position label")
    save(["playlist": [current], "index": 0, "finished": true])
    check(!LastPlayback.load(from: file)!.canResume, "finished media has no Continue")
    save(["playlist": [current], "index": 3])
    check(LastPlayback.load(from: file) == nil, "invalid index is rejected")
    print("ok resume")
    exit(0)
}
if mode == "queue" {
    let ten = touch("season/Episode 10.mkv"), two = touch("season/Episode 2.MP4")
    _ = touch("season/.hidden.mkv")
    _ = touch("season/Extras/extra.mkv")
    _ = touch("season/readme.txt")
    let queued = try! queuedMedia([root.appendingPathComponent("season").path])
    let canonical = { (path: String) in URL(fileURLWithPath: path).resolvingSymlinksInPath().path }
    check(queued.map(canonical) == [two, ten].map(canonical), "folder queue is shallow, filtered, natural order")
    try! appendToPlayer(queued, socketPath: args[3])
    print("ok queue")
    exit(0)
}

let resuming = mode == "window-resume"
let movie = touch("a movie.mkv")
if resuming {
    let config = URL(fileURLWithPath: mpvConfig, isDirectory: true)
    try! FileManager.default.createDirectory(at: config, withIntermediateDirectories: true)
    let data = try! JSONSerialization.data(withJSONObject: ["playlist": [root.appendingPathComponent("missing.mkv").path, movie],
                                                           "index": 1, "time": 83, "title": "A movie"])
    try! data.write(to: config.appendingPathComponent("dual_audio-last.json"))
}
MainActor.assumeIsolated {
    let app = App(logFile: root.appendingPathComponent("playback.log").path)
    NSApplication.shared.setActivationPolicy(.regular)
    NSApplication.shared.delegate = app
    DispatchQueue.global().async {
        wait("home window") { ui { app.home.window?.isVisible == true } }
        check(ui { resuming ? app.home.items.count == 1 : app.home.items.isEmpty }, "home history is seeded only when a previous session exists")
        check(ui { NSApp.mainMenu?.item(withTitle: "File")?.submenu?.item(withTitle: "Open Media…") != nil }, "Open Media menu exists")
        let url = URL(fileURLWithPath: movie)
        ui {
            let pasteboard = NSPasteboard.withUniqueName()
            pasteboard.writeObjects([url as NSURL])
            check(app.home.dropArea.fileURLs(from: pasteboard) == [url], "drop area reads Finder URLs")
            pasteboard.releaseGlobally()
            if resuming { app.home.onResume?() } else { app.home.onDrop?([url]) }
        }
        wait("home hidden during playback") { ui { app.home.window?.isVisible == false } }
        Thread.sleep(forTimeInterval: 0.2)
        check(ui { app.home.window?.isVisible == false }, "home stays hidden while the player runs")
        ui { _ = app.applicationShouldHandleReopen(NSApp, hasVisibleWindows: false) }
        check(ui { app.home.window?.isVisible == false }, "reopening the app does not expose the library during playback")
        wait("home after queue ends") { ui { app.home.window?.isVisible == true } }
        let launch = try! String(contentsOfFile: mpvConfig + "/launch-args", encoding: .utf8)
        check(launch.contains("--force-window=yes\n"), "audio-only media keeps a player window available")
        if resuming {
            check(launch.contains("--playlist-start=0\n") && launch.contains("--start=83.0\n"), "Continue uses the surviving current file and its saved position")
        }
        check(ui { app.home.items.map(\.path) == [movie] }, "played media appears in history")
        ui { app.home.onClear?() }
        check(ui { app.home.items.isEmpty }, "Clear removes recent rows")
        check(FileManager.default.fileExists(atPath: movie), "Clear keeps the actual media")
        print("ok window lifecycle")
        exit(0)
    }
    NSApplication.shared.run()
    withExtendedLifetime(app) {}
}
