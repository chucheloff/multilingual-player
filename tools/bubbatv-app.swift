// bubbaTV.app - play a movie with dualplay, no terminal needed: drop it on
// the app (Dock or Finder) or use Open With > bubbaTV. tools/make-app.sh
// builds it and bakes two things into its Info.plist: where dualplay is (this
// checkout) and the PATH to find mpv with. The languages come from
// dual_audio.conf, as with dualplay itself.
//
//   bubbaTV --dry-run FILE...   print the dualplay command instead of running it
//
// With two Bluetooth outputs connected (as `dualplay -L` lists them) it plays
// with -2, a pair each; otherwise on one shared pair, a language per ear.
// mpv's output goes to ~/Library/Logs/bubbaTV.log. The app quits with mpv.
import Cocoa
import UniformTypeIdentifiers

let dualplay = Bundle.main.object(forInfoDictionaryKey: "BubbaTVDualplay") as? String ?? ""
// Apps started from the Dock get a bare PATH, without Homebrew.
let path = Bundle.main.object(forInfoDictionaryKey: "BubbaTVPath") as? String
    ?? ProcessInfo.processInfo.environment["PATH"] ?? ""
let log = NSHomeDirectory() + "/Library/Logs/bubbaTV.log"

struct Problem: Error {
    let title: String
    let detail: String

    static let nothing = Problem(title: "Nothing to play", detail:
        "Drop a movie on bubbaTV in the Dock or in Finder, or right-click a movie and choose Open With > bubbaTV.")
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

/// Connected Bluetooth outputs, the ones `dualplay -2` combines. 0 if dualplay
/// can't tell (no swiftc to build its helper): one pair then.
func bluetoothOutputs() -> Int {
    let p = dualplayProcess(["-L"])
    let out = Pipe()
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return 0 }
    let list = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    p.waitUntilExit()
    return list.split(separator: "\n").filter { $0.hasSuffix(", Bluetooth)") }.count
}

/// The dualplay command for these files, or what stands in its way.
func command(_ files: [String]) throws(Problem) -> [String] {
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
    let files = files.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
    return [dualplay] + (bluetoothOutputs() == 2 ? ["-2"] : []) + files
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

    /// Started without a movie: say how to use it, or pick one.
    private func choose() {
        let alert = NSAlert()
        alert.messageText = Problem.nothing.title
        alert.informativeText = Problem.nothing.detail
        alert.addButton(withTitle: "Choose Movie…")
        alert.addButton(withTitle: "Quit")
        front()
        guard alert.runModal() == .alertFirstButtonReturn else { return NSApp.terminate(nil) }
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = [.movie] + ["mkv", "webm"].compactMap { UTType(filenameExtension: $0) }
        guard panel.runModal() == .OK else { return NSApp.terminate(nil) }
        play(panel.urls.map(\.path))
    }

    private func play(_ files: [String]) {
        if running != nil {
            return show(Problem(title: "Already playing", detail: "Quit mpv first, then open the next movie."))
        }
        let p: Process
        do {
            p = dualplayProcess(Array(try command(files).dropFirst()))
        } catch {
            show(error)
            return NSApp.terminate(nil)
        }
        FileManager.default.createFile(atPath: log, contents: nil)  // a fresh log per movie
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
        print(try command(Array(args.dropFirst())).map(quoted).joined(separator: " "))
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
