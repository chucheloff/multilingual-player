// menu_clicks - click through the bubbaTV menu like a user, against a real
// mpv (libmpv) running dual_audio.lua, and check what each click did.
// Built together with tools/bubbatv-menu.swift (hence main.swift) and run by
// tests/test_dual_audio.py (Menu.test_clicks, Menu.test_device_clicks):
//   menu_clicks FILE DUAL_AUDIO_LUA DEVICES_JSON
//       prints "ok ..." lines; exits 1 on a failure. The script remembers
//       the loudness set by a click in DEVICES_JSON.
//   menu_clicks FILE DUAL_AUDIO_LUA DEVICES_JSON HELPER LOG
//       pairs mode as under dualplay -2 ("Pair A", "Pair B"), with HELPER in
//       place of tools/audio-pairs: moves the pairs to other headphones.
//       Outputs come from BUBBATV_DEVICES; mpv logs to LOG.
import Cocoa

let args = CommandLine.arguments
let pairs = args.count == 6
setvbuf(stdout, nil, _IOLBF, 0)
_ = NSApplication.shared
// Stands in for mpv's menu bar: the plugin goes before "Window".
let bar = NSMenu()
bar.addItem(withTitle: "Window", action: nil, keyEquivalent: "")
NSApp.mainMenu = bar

let mpv = mpv_create()!
var options = [("vo", "null"), ("ao", "null"), ("pause", "yes"), ("start", "3"), ("scripts", args[2]),
               ("script-opts", "dual_audio-auto=yes,dual_audio-devices_file=\(args[3])")]
if pairs {
    options += [("script-opts", "dual_audio-auto=yes,dual_audio-devices_file=\(args[3]),dual_audio-mode=pairs,"
                 + "dual_audio-pair_names=Pair A|Pair B,dual_audio-helper=" + args[4]),
                ("log-file", args[5])]
}
for (k, v) in options {
    mpv_set_option_string(mpv, k, v)
}
mpv_initialize(mpv)
mpv_command_string(mpv, "loadfile \"\(args[1])\"")
// mpv runs a C plugin on a thread of its own with a client handle, and
// destroys the handle when the plugin returns.
let client = mpv_create_client(mpv, "bubbatv")
Thread {
    _ = mpvOpenCplugin(client)
    mpv_destroy(client)
}.start()

// The checks run on a thread of their own too: they block on mpv, and mpv's
// core sometimes waits on the main thread, where the menu lives.
func get(_ name: String) -> String {
    guard let v = mpv_get_property_string(mpv, name) else { return "" }
    defer { mpv_free(v) }
    return String(cString: v)
}

func graph() -> String { get("lavfi-complex") }

func ui<T>(_ body: @MainActor () -> T) -> T {
    DispatchQueue.main.sync { MainActor.assumeIsolated(body) }
}

@MainActor func menu() -> NSMenu { bar.items.first { $0.title == "bubbaTV" }!.submenu! }

@MainActor func click(_ m: NSMenu, _ title: String) {
    m.performActionForItem(at: m.indexOfItem(withTitle: title))
}

@MainActor func trackMenus() -> [NSMenuItem] { menu().items.filter { $0.title.hasPrefix("Audio track:") } }

@MainActor func deviceMenus() -> [NSMenuItem] { menu().items.filter { $0.title.hasPrefix("Device:") } }

func wait(_ what: String, _ ok: () -> Bool) {
    let end = Date() + 5
    while !ok() {
        if Date() > end {
            print("FAIL \(what): lavfi-complex=\(graph())")
            exit(1)
        }
        Thread.sleep(forTimeInterval: 0.05)
    }
    print("ok \(what)")
}

// The helper's calls are for the test to check; here each move shows up in
// the menu, and the audio output comes back after the ao-reload.
func devices() {
    wait("dual audio on") { ui { deviceMenus().compactMap(\.submenu).count } == 2 }
    ui { click(deviceMenus()[1].submenu!, "Other Phones") }
    wait("pair 2 moved") { ui { deviceMenus().map(\.title) } == ["Device: Pair A", "Device: Other Phones"] }
    wait("audio output back") { get("current-ao") == "null" }
    ui { click(deviceMenus()[0].submenu!, "Other Phones") }
    wait("the other pair's headphones: the two swap") {
        ui { deviceMenus().map(\.title) } == ["Device: Other Phones", "Device: Pair A"]
    }
    wait("audio output back again") { get("current-ao") == "null" }
}

Thread {
    wait("menu installed before Window") { ui { bar.items.map(\.title) } == ["bubbaTV", "Window"] }
    if pairs {
        devices()
        mpv_terminate_destroy(mpv)
        exit(0)
    }
    wait("dual audio on") { graph().hasPrefix("[aid1]") && ui { menu().item(withTitle: "Swap ears") != nil } }
    ui { click(menu(), "Swap ears") }
    wait("swap") { graph().hasPrefix("[aid2]") }
    wait("menu shows the swap") { ui { trackMenus().first?.title } == "Audio track: [2] Russian 1000Hz 5.1 (rus)" }
    ui { click(trackMenus()[1].submenu!, "[2] Russian 1000Hz 5.1 (rus)") }
    wait("same track for both from the submenu") { graph().hasPrefix("[aid2]asplit=2") }
    wait("menu shows it") { ui { trackMenus().last?.title } == "Audio track: [2] Russian 1000Hz 5.1 (rus)" }
    ui {
        // Slider rows: left loudness, left delay, right loudness, right delay.
        let rows = menu().items.compactMap { $0.view as? SliderRow }
        rows[2].subviews.compactMap { $0 as? NSButton }.first { $0.title == "+" }!.performClick(nil)
    }
    wait("right loudness + after the pause") { graph().contains("volume=1dB[r]") }
    ui { click(menu(), "Subtitles follow ears") }
    wait("subtitles follow the ears") {
        get("user-data/dual_audio").contains("\"follow_subs\":true")
            && ui { menu().item(withTitle: "Subtitles follow ears")?.state } == .on
    }
    ui { click(menu(), "Dual audio") }
    wait("dual audio off") { graph().isEmpty }
    mpv_terminate_destroy(mpv)
    exit(0)
}.start()
RunLoop.main.run()
