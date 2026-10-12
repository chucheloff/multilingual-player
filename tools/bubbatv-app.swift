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
// Opened on its own it offers to continue what played last (dual_audio.lua
// keeps that in dual_audio-last.json), to pick a movie, or to update this
// checkout (git pull, then rebuild what changed).
// mpv's output goes to ~/Library/Logs/bubbaTV.log (the two runs before to
// bubbaTV.1.log, bubbaTV.2.log). The app quits with mpv.
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
        "Drop a movie or a folder of episodes on bubbaTV in the Dock or in Finder, "
        + "or right-click one and choose Open With > bubbaTV.")
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

/// What played last, as dual_audio.lua saved it; nil when there is nothing
/// to continue (it played to its end, or the files are gone).
struct Last {
    let files: [String]
    let title: String
    let time: Double?

    static func load() -> Last? {
        guard let o = json("dual_audio-last.json") as? [String: Any],
              let files = o["playlist"] as? [String], let index = o["index"] as? Int,
              files.indices.contains(index), o["finished"] as? Bool != true,
              FileManager.default.fileExists(atPath: files[index]) else { return nil }
        let title = o["title"] as? String ?? (files[index] as NSString).lastPathComponent
        return Last(files: files.filter { FileManager.default.fileExists(atPath: $0) }, title: title,
                    time: o["time"] as? Double)
    }

    var label: String {
        guard let time, time >= 1 else { return "Continue \u{201C}\(title)\u{201D}" }
        let t = Int(time)
        let at = t >= 3600 ? String(format: "%d:%02d:%02d", t / 3600, t / 60 % 60, t % 60)
                           : String(format: "%d:%02d", t / 60, t % 60)
        return "Continue \u{201C}\(title)\u{201D} at \(at)"
    }
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
    let app = ["tools/bubbatv-app.swift", "tools/bubbatv-app.plist", "tools/make-app.sh"]
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
final class App: NSObject, NSApplicationDelegate {
    private var opened = false
    private var running: Process?
    private var quitting = false

    func application(_ app: NSApplication, open urls: [URL]) {
        opened = true
        play(urls.map(\.path))
    }

    // Files the app was started with arrive before this; allow a moment anyway.
    func applicationDidFinishLaunching(_ note: Notification) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            if !self.opened { self.choose() }
        }
    }

    // Quitting bubbaTV stops the movie too. dualplay runs in a process group
    // of its own (with mpv), and removes the combined device as it exits.
    func applicationShouldTerminate(_ app: NSApplication) -> NSApplication.TerminateReply {
        guard let running else { return .terminateNow }
        quitting = true
        kill(-running.processIdentifier, SIGTERM)
        return .terminateLater
    }

    /// Started without a movie: continue the last one, pick one, or update.
    private func choose() {
        let last = Last.load()
        let alert = NSAlert()
        alert.messageText = Problem.nothing.title
        alert.informativeText = Problem.nothing.detail
        var actions: [() -> Void] = []
        if let last {
            alert.addButton(withTitle: last.label)
            actions.append { self.play(last.files, ordered: true) }
        }
        alert.addButton(withTitle: "Choose Movie…")
        actions.append { self.pickFiles() }
        alert.addButton(withTitle: "Check for Updates")
        actions.append { self.checkForUpdates() }
        alert.addButton(withTitle: "Quit")
        actions.append { NSApp.terminate(nil) }
        front()
        let i = alert.runModal().rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
        actions.indices.contains(i) ? actions[i]() : NSApp.terminate(nil)
    }

    private func checkForUpdates() {
        let (result, relaunch) = update()
        show(result)
        guard relaunch else { return choose() }
        // The bundle is the new one now: open it once this one has quit.
        let again = Process()
        again.executableURL = URL(fileURLWithPath: "/bin/sh")
        again.arguments = ["-c", "sleep 1; open \"$0\"", Bundle.main.bundlePath]
        try? again.run()
        NSApp.terminate(nil)
    }

    private func pickFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true  // a folder of episodes
        panel.allowedContentTypes = [.movie] + ["mkv", "webm"].compactMap { UTType(filenameExtension: $0) }
        guard panel.runModal() == .OK else { return NSApp.terminate(nil) }
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

    private func play(_ files: [String], ordered: Bool = false) {
        if running != nil {
            return show(Problem(title: "Already playing", detail: "Quit mpv first, then open the next movie."))
        }
        var on = pairs()
        if case .ask(let outputs) = on {
            guard let chosen = askPairs(outputs) else { return NSApp.terminate(nil) }
            on = chosen
        }
        let p: Process
        do {
            p = dualplayProcess(Array(try command(files, on: on, ordered: ordered).dropFirst()))
        } catch {
            show(error)
            return NSApp.terminate(nil)
        }
        // A fresh log per movie; the two before stay as bubbaTV.1.log, bubbaTV.2.log.
        let older = { (n: Int) in (log as NSString).deletingPathExtension + ".\(n).log" }
        try? FileManager.default.removeItem(atPath: older(2))
        try? FileManager.default.moveItem(atPath: older(1), toPath: older(2))
        try? FileManager.default.moveItem(atPath: log, toPath: older(1))
        FileManager.default.createFile(atPath: log, contents: nil)
        let out = FileHandle(forWritingAtPath: log) ?? FileHandle.nullDevice
        p.standardOutput = out
        p.standardError = out
        p.terminationHandler = { p in
            let (reason, status) = (p.terminationReason, p.terminationStatus)
            DispatchQueue.main.async { self.finished(reason, status) }
        }
        do {
            try p.run()
        } catch {
            show(Problem(title: "Could not start dualplay", detail: error.localizedDescription))
            return NSApp.terminate(nil)
        }
        running = p
        // mpv has a Dock icon of its own while it plays.
        NSApp.setActivationPolicy(.accessory)
    }

    private func finished(_ reason: Process.TerminationReason, _ status: Int32) {
        running = nil
        if quitting { return NSApp.reply(toApplicationShouldTerminate: true) }
        // 4: mpv was stopped by a signal (Ctrl+C in its window), not an error.
        if reason == .exit && status != 0 && status != 4 {
            let text = (try? String(contentsOfFile: log, encoding: .utf8)) ?? ""
            let tail = text.split(separator: "\n").suffix(12).joined(separator: "\n")
            show(Problem(title: "Playback stopped with an error",
                         detail: tail + "\n\nThe whole output is in ~/Library/Logs/bubbaTV.log."))
        }
        NSApp.terminate(nil)
    }

    private func show(_ problem: Problem) {
        let alert = NSAlert()
        alert.messageText = problem.title
        alert.informativeText = problem.detail
        front()
        alert.runModal()
    }
}

let args = Array(CommandLine.arguments.dropFirst())
if args.first == "--dry-run" {
    do {
        var on = pairs()
        if case .ask(let outputs) = on {
            // No one to ask: one pair, as without the choice.
            FileHandle.standardError.write(Data("bubbaTV: would ask which two of: \(outputs.joined(separator: ", "))\n".utf8))
            on = .one
        }
        print(try command(Array(args.dropFirst()), on: on).map(quoted).joined(separator: " "))
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("bubbaTV: \(error.title). \(error.detail)\n".utf8))
        exit(1)
    }
}
MainActor.assumeIsolated {
    let delegate = App()  // NSApp holds its delegate weakly
    NSApplication.shared.delegate = delegate
    NSApplication.shared.run()
}
