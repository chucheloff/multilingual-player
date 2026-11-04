// bubbaTV.app - play a movie with dualplay, no terminal needed: drop it on
// the app (Dock or Finder) or use Open With > bubbaTV. A folder of episodes
// works the same way: dualplay plays its videos. tools/make-app.sh
// builds it and bakes two things into its Info.plist: where dualplay is (this
// checkout) and the PATH to find mpv with. The languages come from
// dual_audio.conf, as with dualplay itself.
//
//   bubbaTV --dry-run FILE...   print the dualplay command instead of running it
//
// With two Bluetooth outputs connected (as `dualplay -L` lists them) it plays
// with -2, a pair each. With more, the two headphones played with before
// (remembered in mpv's config dir) are the pairs; if that doesn't settle it,
// it asks. With fewer it plays on one shared pair, a language per ear.
// Opened on its own it shows a home window with recent media, a drop area,
// and Continue. The home window is hidden for the whole playback queue and
// returns when mpv closes. File > Open Media and Finder/Dock drops work too.
// mpv's output goes to ~/Library/Logs/bubbaTV.log (the two runs before to
// bubbaTV.1.log, bubbaTV.2.log). Quitting bubbaTV stops mpv too.
import Cocoa
import UniformTypeIdentifiers

let dualplay = Bundle.main.object(forInfoDictionaryKey: "BubbaTVDualplay") as? String ?? ""
// Apps started from the Dock get a bare PATH, without Homebrew.
let path = Bundle.main.object(forInfoDictionaryKey: "BubbaTVPath") as? String
    ?? ProcessInfo.processInfo.environment["PATH"] ?? ""
let log = NSHomeDirectory() + "/Library/Logs/bubbaTV.log"
let repo = (dualplay as NSString).deletingLastPathComponent
// mpv's config dir, where dual_audio.lua keeps what it remembers.
let mpvConfig = ProcessInfo.processInfo.environment["MPV_HOME"] ?? NSHomeDirectory() + "/.config/mpv"

struct Problem: Error {
    let title: String
    let detail: String

    static let nothing = Problem(title: "Nothing to play", detail:
        "Choose Open Media or drop a media file or folder into the bubbaTV window.")
}

/// dualplay with these arguments, run with the baked-in PATH.
func dualplayProcess(_ args: [String]) -> Process {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: dualplay)
    p.arguments = args
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = path
    p.environment = env
    p.standardInput = FileHandle.nullDevice
    return p
}

/// Runs a program to its end: its exit status and output (stdout, then stderr).
func run(_ p: Process) -> (Int32, String) {
    let out = Pipe()
    p.standardOutput = out
    p.standardError = out
    guard (try? p.run()) != nil else { return (-1, "") }
    let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    p.waitUntilExit()
    return (p.terminationStatus, text.trimmingCharacters(in: .whitespacesAndNewlines))
}

/// Connected Bluetooth outputs by name, the ones `dualplay -2` combines.
/// None if dualplay can't tell (no swiftc to build its helper): one pair then.
func bluetoothOutputs() -> [String] {
    let p = dualplayProcess(["-L"])
    let out = Pipe()
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return [] }
    let list = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    p.waitUntilExit()
    // "  NAME (2 ch, Bluetooth)"
    return list.split(separator: "\n").compactMap { line in
        guard line.hasSuffix(", Bluetooth)"), let open = line.range(of: " (", options: .backwards) else { return nil }
        return line[..<open.lowerBound].trimmingCharacters(in: .whitespaces)
    }
}

func json(_ file: String) -> Any? {
    guard let data = FileManager.default.contents(atPath: mpvConfig + "/" + file) else { return nil }
    return try? JSONSerialization.jsonObject(with: data)
}

/// Headphones dual_audio.lua remembers something for: a language or track
/// picks, or loudness and delay.
func knownHeadphones() -> Set<String> {
    var names = Set<String>()
    for file in ["dual_audio-tracks.json", "dual_audio-devices.json"] {
        if let o = json(file) as? [String: Any] { names.formUnion(o.keys) }
    }
    return names.filter { !$0.hasPrefix("system output") }
}

/// Which outputs the pairs play on.
enum Pairs {
    case one                // one shared pair, a language per ear
    case two([String])      // -2: these two (empty: dualplay takes the two Bluetooth ones)
    case ask([String])      // more than two and none known well enough: these to choose from
}

func pairs() -> Pairs {
    let bluetooth = bluetoothOutputs()
    if bluetooth.count == 2 { return .two([]) }
    if bluetooth.count < 2 { return .one }
    let known = knownHeadphones()
    let familiar = bluetooth.filter(known.contains)
    return familiar.count == 2 ? .two(familiar) : .ask(bluetooth)
}

/// The dualplay command for these files, or what stands in its way. `on`:
/// the pairs, as pairs() or the person chose them. Files are put in episode
/// order unless `ordered` (a playlist to continue keeps its own).
func command(_ files: [String], on: Pairs, ordered: Bool = false) throws(Problem) -> [String] {
    guard !files.isEmpty else { throw .nothing }
    guard FileManager.default.isExecutableFile(atPath: dualplay) else {
        throw Problem(title: "dualplay is missing", detail: "bubbaTV plays with \(dualplay), which is gone. "
            + "If the multilingual-player folder has moved, build the app again from there: tools/make-app.sh")
    }
    guard path.split(separator: ":").contains(where: { FileManager.default.isExecutableFile(atPath: "\($0)/mpv") })
    else {
        throw Problem(title: "mpv is not installed",
                      detail: "Install it with Homebrew (brew install mpv), then open the movie again.")
    }
    // Episodes in order: "Episode 2" before "Episode 10".
    let files = ordered ? files : files.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
    var flags: [String] = []
    if case .two(let names) = on { flags = ["-2"] + names.flatMap { ["-p", $0] } }
    // The log gets mpv's messages, with times, but not its status line.
    return [dualplay] + flags + files + ["--quiet", "--msg-time"]
}

/// Brings this checkout up to date: git pull, then dualplay's helpers and,
/// if its own code changed, the app itself. What happened, and whether the
/// app has to start again to be the new one.
func update() -> (Problem, relaunch: Bool) {
    func git(_ args: [String]) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        p.arguments = ["-C", repo] + args
        return run(p)
    }
    if case (0, let changes) = git(["status", "--porcelain", "--untracked-files=no"]), !changes.isEmpty {
        return (Problem(title: "Not updated", detail: "The multilingual-player folder has changes of its own:\n"
            + changes + "\n\nCommit or undo them, then check again."), false)
    }
    let (fetched, why) = git(["fetch", "--quiet"])
    guard fetched == 0 else { return (Problem(title: "Could not check for updates", detail: why), false) }
    guard case (0, let behind) = git(["rev-list", "--count", "HEAD..@{u}"]) else {
        return (Problem(title: "Could not check for updates",
                        detail: "This branch follows no branch on GitHub."), false)
    }
    let branch = git(["rev-parse", "--abbrev-ref", "HEAD"]).1
    guard behind != "0" else {
        return (Problem(title: "bubbaTV is up to date", detail: "Branch \(branch), as on GitHub."), false)
    }
    let news = git(["log", "--format=• %s", "--no-merges", "HEAD..@{u}"]).1
    let changed = git(["diff", "--name-only", "HEAD", "@{u}"]).1.split(separator: "\n")
    let (pulled, pullWhy) = git(["pull", "--ff-only", "--quiet"])
    guard pulled == 0 else {
        return (Problem(title: "Could not update", detail: pullWhy + "\n\nThe branch may have commits of its own."), false)
    }
    var detail = "Branch \(branch):\n" + news
    let (built, buildWhy) = run(dualplayProcess(["-B"]))
    if built != 0 { detail += "\n\nBuilding the helpers failed:\n" + buildWhy }
    let app = ["tools/bubbatv-app.swift", "tools/bubbatv-home.swift", "tools/bubbatv-library.swift",
               "tools/make-icon.swift", "tools/bubbatv-app.plist", "tools/make-app.sh"]
    guard changed.contains(where: { app.contains(String($0)) }) else {
        return (Problem(title: "bubbaTV is updated", detail: detail), false)
    }
    let make = Process()
    make.executableURL = URL(fileURLWithPath: repo + "/tools/make-app.sh")
    make.arguments = [Bundle.main.bundlePath]
    var env = ProcessInfo.processInfo.environment
    env["PATH"] = path
    make.environment = env
    let (made, makeWhy) = run(make)
    guard made == 0 else {
        return (Problem(title: "bubbaTV is updated, but the app could not be rebuilt",
                        detail: detail + "\n\n" + makeWhy + "\n\nRun tools/make-app.sh in the folder."), false)
    }
    return (Problem(title: "bubbaTV is updated", detail: detail + "\n\nbubbaTV opens again to be the new version."), true)
}

/// An argument as the shell would need it, for the dry run.
func quoted(_ s: String) -> String {
    if !s.isEmpty && s.allSatisfy({ $0.isLetter || $0.isNumber || "/._-+=:,@".contains($0) }) { return s }
    return "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

@MainActor func front() {
    if #available(macOS 14, *) { NSApp.activate() } else { NSApp.activate(ignoringOtherApps: true) }
}

@MainActor
final class App: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private let playbackLog: String
    private var opened = false
    private var opening = false
    private var running: Process?
    private var quitting = false
    private var socketDirectory: URL?
    private var pending: [String] = []
    private let ipcQueue = DispatchQueue(label: "bubbaTV.playlist")
    private let history = RecentMedia(file: URL(fileURLWithPath: mpvConfig + "/bubbatv-recent.json"))
    private let lastFile = URL(fileURLWithPath: mpvConfig + "/dual_audio-last.json")
    private var recentMenu = NSMenu(title: "Open Recent")
    lazy var home: HomeWindow = makeHome()

    init(logFile: String = log) {
        playbackLog = logFile
        super.init()
    }

    private func makeHome() -> HomeWindow {
        let home = HomeWindow()
        home.onOpen = { [weak self] in self?.pickFiles() }
        home.onPlay = { [weak self] in self?.play([$0]) }
        home.onDrop = { [weak self] in self?.play($0.map(\.path)) }
        home.onResume = { [weak self] in
            guard let self, let last = LastPlayback.load(from: self.lastFile), last.canResume else { return }
            self.play(last.files, resume: last)
        }
        home.onClear = { [weak self] in
            guard let self else { return }
            do { try self.history.clear(); self.refreshHome() }
            catch { self.show(Problem(title: "Could not clear recent media", detail: error.localizedDescription)) }
        }
        home.onRemove = { [weak self] path in
            guard let self else { return }
            do { try self.history.remove(path); self.refreshHome() }
            catch { self.show(Problem(title: "Could not remove recent media", detail: error.localizedDescription)) }
        }
        return home
    }

    func application(_ app: NSApplication, open urls: [URL]) {
        opened = true
        play(urls.map(\.path))
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        installMenus()
        // One existing watch-later session can seed a newly created history.
        // Clearing history creates an empty file, so it stays cleared.
        if !FileManager.default.fileExists(atPath: history.file.path),
           let last = LastPlayback.load(from: lastFile), mediaExists(last.current) {
            remember([last.current])
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            if !self.opened { self.showHome() }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if running == nil { showHome() }
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // dualplay and mpv share their own process group; its exit trap removes
    // the combined device. Keep the library hidden while the queue plays.
    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        quitting = true
        guard let running else { return .terminateNow }
        kill(-running.processIdentifier, SIGTERM)
        return .terminateLater
    }

    private func installMenus() {
        let bar = NSMenu()
        let application = NSMenu(title: "bubbaTV")
        application.addItem(withTitle: "About bubbaTV", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        let update = application.addItem(withTitle: "Check for Updates…", action: #selector(checkForUpdates), keyEquivalent: "")
        update.target = self
        application.addItem(.separator())
        application.addItem(withTitle: "Hide bubbaTV", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        application.addItem(.separator())
        application.addItem(withTitle: "Quit bubbaTV", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = bar.addItem(withTitle: "bubbaTV", action: nil, keyEquivalent: "")
        appItem.submenu = application
        let file = NSMenu(title: "File")
        let open = file.addItem(withTitle: "Open Media…", action: #selector(pickFiles), keyEquivalent: "o")
        open.target = self
        let recent = file.addItem(withTitle: "Open Recent", action: nil, keyEquivalent: "")
        recent.submenu = recentMenu
        file.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        bar.addItem(withTitle: "File", action: nil, keyEquivalent: "").submenu = file
        let edit = NSMenu(title: "Edit")
        for (title, selector, key) in [("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                        ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: NSSelectorFromString(selector), keyEquivalent: key)
        }
        bar.addItem(withTitle: "Edit", action: nil, keyEquivalent: "").submenu = edit
        let window = NSMenu(title: "Window")
        window.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        window.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        bar.addItem(withTitle: "Window", action: nil, keyEquivalent: "").submenu = window
        NSApp.windowsMenu = window
        NSApp.mainMenu = bar
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(checkForUpdates) { return !opening && running == nil }
        if item.action == #selector(pickFiles) { return !opening }
        return true
    }

    private func refreshHome() {
        let items = history.available
        home.refresh(items: items, last: LastPlayback.load(from: lastFile))
        recentMenu.removeAllItems()
        for item in items {
            let row = recentMenu.addItem(withTitle: URL(fileURLWithPath: item.path).lastPathComponent,
                                        action: #selector(openRecent(_:)), keyEquivalent: "")
            row.target = self
            row.representedObject = item.path
            row.toolTip = item.path
        }
        if items.isEmpty {
            recentMenu.addItem(withTitle: "No recent media", action: nil, keyEquivalent: "").isEnabled = false
        }
    }

    private func showHome() {
        guard running == nil && !quitting else { return }
        NSApp.setActivationPolicy(.regular)
        refreshHome()
        home.setBusy(opening)
        home.showWindow(nil)
        home.window?.makeKeyAndOrderFront(nil)
        front()
    }

    @objc private func openRecent(_ sender: NSMenuItem) {
        if let path = sender.representedObject as? String { play([path]) }
    }

    @objc private func checkForUpdates() {
        guard !opening && running == nil else { return }
        opening = true
        home.setBusy(true)
        DispatchQueue.global(qos: .userInitiated).async {
            let (result, relaunch) = update()
            DispatchQueue.main.async {
                self.opening = false
                self.home.setBusy(false)
                self.show(result)
                guard relaunch else {
                    if self.pending.isEmpty { self.showHome() }
                    else { let files = self.pending; self.pending = []; self.play(files) }
                    return
                }
                let again = Process()
                again.executableURL = URL(fileURLWithPath: "/bin/sh")
                again.arguments = ["-c", "sleep 1; open -a \"$0\" \"$@\"", Bundle.main.bundlePath] + self.pending
                try? again.run()
                NSApp.terminate(nil)
            }
        }
    }

    @objc private func pickFiles() {
        guard !opening else { return }
        let panel = NSOpenPanel()
        panel.title = "Open Media"
        panel.prompt = "Play"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true
        panel.allowedContentTypes = [.movie, .audio] + ["mkv", "webm"].compactMap { UTType(filenameExtension: $0) }
        guard panel.runModal() == .OK else { return }
        play(panel.urls.map(\.path))
    }

    /// More than two Bluetooth outputs and no telling which two are the
    /// pairs: ask. nil: don't play.
    private func askPairs(_ outputs: [String]) -> Pairs? {
        let known = knownHeadphones()
        let order = outputs.filter(known.contains) + outputs.filter { !known.contains($0) }
        let popups = [NSPopUpButton(), NSPopUpButton()]
        let grid = NSGridView(views: zip(["Pair 1", "Pair 2"], popups).map { label, popup in
            popup.addItems(withTitles: order)
            return [NSTextField(labelWithString: label), popup]
        })
        popups[1].selectItem(at: min(1, order.count - 1))
        grid.frame = NSRect(x: 0, y: 0, width: 360, height: 56)
        while true {
            let alert = NSAlert()
            alert.messageText = "Which headphones are the two pairs?"
            alert.informativeText = "\(outputs.count) Bluetooth outputs are connected. Each pair hears its own language."
            alert.accessoryView = grid
            alert.addButton(withTitle: "Two Pairs")
            alert.addButton(withTitle: "One Shared Pair")
            alert.addButton(withTitle: "Cancel")
            front()
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                let names = popups.compactMap(\.titleOfSelectedItem)
                if names.count == 2 && names[0] != names[1] { return .two(names) }
                show(Problem(title: "Pick two different headphones", detail: "One for each pair."))
            case .alertSecondButtonReturn:
                return .one
            default:
                return nil
            }
        }
    }

    private func play(_ paths: [String], resume: LastPlayback? = nil) {
        let files = normalizedMedia(paths)
        guard !files.isEmpty else {
            showHome()
            return show(Problem(title: "No available media", detail: "Choose a media file or a folder that is still on disk."))
        }
        if running != nil { return enqueue(files) }
        if opening { pending += files; return }
        opening = true
        showHome()
        // First-run compilation and CoreAudio discovery can take a while.
        // Keep the home window responsive and visibly preparing playback.
        DispatchQueue.global(qos: .userInitiated).async {
            let on = pairs()
            DispatchQueue.main.async { self.launch(files, on: on, resume: resume) }
        }
    }

    private func launch(_ files: [String], on initial: Pairs, resume: LastPlayback?) {
        guard !quitting else { return }
        var on = initial
        if case .ask(let outputs) = on {
            guard let chosen = askPairs(outputs) else {
                opening = false; pending = []; showHome(); return
            }
            on = chosen
        }
        let p: Process
        do {
            var args = Array(try command(files, on: on, ordered: resume != nil).dropFirst())
            // Audio-only media still needs a player window and its controls
            // while the home window is hidden.
            args.append("--force-window=yes")
            if let resume {
                args.append("--playlist-start=\(resume.resumeIndex)")
                if let time = resume.time, time.isFinite && time >= 0 { args.append("--start=\(time)") }
            }
            let directory = URL(fileURLWithPath: "/tmp/bubbatv-" + UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                    attributes: [.posixPermissions: 0o700])
            socketDirectory = directory
            args.append("--input-ipc-server=" + directory.appendingPathComponent("player.sock").path)
            p = dualplayProcess(args)
        } catch {
            opening = false; pending = []; cleanSocket(); showHome()
            if let problem = error as? Problem { show(problem) }
            else { show(Problem(title: "Could not prepare playback", detail: error.localizedDescription)) }
            return
        }
        let older = { (n: Int) in (self.playbackLog as NSString).deletingPathExtension + ".\(n).log" }
        try? FileManager.default.createDirectory(atPath: (playbackLog as NSString).deletingLastPathComponent,
                                                withIntermediateDirectories: true)
        try? FileManager.default.removeItem(atPath: older(2))
        try? FileManager.default.moveItem(atPath: older(1), toPath: older(2))
        try? FileManager.default.moveItem(atPath: playbackLog, toPath: older(1))
        FileManager.default.createFile(atPath: playbackLog, contents: nil)
        let out = FileHandle(forWritingAtPath: playbackLog) ?? FileHandle.nullDevice
        p.standardOutput = out
        p.standardError = out
        p.terminationHandler = { p in
            let (reason, status) = (p.terminationReason, p.terminationStatus)
            DispatchQueue.main.async { self.finished(reason, status) }
        }
        do { try p.run() }
        catch {
            opening = false; pending = []; cleanSocket(); showHome()
            return show(Problem(title: "Could not start playback", detail: error.localizedDescription))
        }
        running = p
        opening = false
        remember(files)
        home.window?.orderOut(nil)
        // mpv's video window and Dock icon take over until the entire queue ends.
        NSApp.setActivationPolicy(.accessory)
        if !pending.isEmpty { let queued = pending; pending = []; enqueue(queued) }
    }

    private func enqueue(_ paths: [String]) {
        guard let session = running, let directory = socketDirectory else { return }
        let socket = directory.appendingPathComponent("player.sock").path
        ipcQueue.async {
            do {
                let files = try queuedMedia(paths)
                try appendToPlayer(files, socketPath: socket)
                DispatchQueue.main.async {
                    guard self.running === session else { return }
                    self.remember(paths)
                }
            } catch {
                let detail = error.localizedDescription
                DispatchQueue.main.async {
                    guard self.running === session else { return }
                    self.show(Problem(title: "Could not add media to the queue", detail: detail))
                }
            }
        }
    }

    private func remember(_ files: [String]) {
        do { try history.remember(files) }
        catch { NSLog("bubbaTV: could not save recent media: %@", error.localizedDescription) }
    }

    private func cleanSocket() {
        if let directory = socketDirectory { try? FileManager.default.removeItem(at: directory) }
        socketDirectory = nil
    }

    private func finished(_ reason: Process.TerminationReason, _ status: Int32) {
        running = nil
        cleanSocket()
        if quitting { return NSApp.reply(toApplicationShouldTerminate: true) }
        showHome()
        // 4: mpv was stopped by a signal, not a playback error.
        if reason == .exit && status != 0 && status != 4 {
            let text = (try? String(contentsOfFile: playbackLog, encoding: .utf8)) ?? ""
            let tail = text.split(separator: "\n").suffix(12).joined(separator: "\n")
            show(Problem(title: "Playback stopped with an error",
                         detail: tail + "\n\nThe whole output is in ~/Library/Logs/bubbaTV.log."))
        }
    }

    private func show(_ problem: Problem) {
        let alert = NSAlert()
        alert.messageText = problem.title
        alert.informativeText = problem.detail
        front()
        alert.runModal()
    }
}

#if !BUBBATV_TESTING
@main
struct BubbaTV {
    @MainActor static func main() {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.first == "--dry-run" {
            do {
                var on = pairs()
                if case .ask(let outputs) = on {
                    FileHandle.standardError.write(Data("bubbaTV: would ask which two of: \(outputs.joined(separator: ", "))\n".utf8))
                    on = .one
                }
                print(try command(Array(args.dropFirst()), on: on).map(quoted).joined(separator: " "))
            } catch {
                FileHandle.standardError.write(Data("bubbaTV: \(error.title). \(error.detail)\n".utf8))
                exit(1)
            }
            return
        }
        let delegate = App()
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.delegate = delegate
        NSApplication.shared.run()
        withExtendedLifetime(delegate) {}
    }
}
#endif
