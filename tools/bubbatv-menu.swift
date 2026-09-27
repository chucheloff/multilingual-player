// bubbatv - the "bubbaTV" menu in mpv's menu bar, for scripts/dual_audio.lua:
// for each headphone pair (or ear) its device, audio track, loudness and delay.
//
// An mpv C plugin: a library mpv loads with --script=bubbatv.so. dualplay
// builds and loads it; by hand:
//   swiftc -O -emit-library -o bubbatv.so tools/bubbatv-menu.swift \
//     -import-objc-header "$(brew --prefix mpv)/include/mpv/client.h" \
//     -Xlinker -undefined -Xlinker dynamic_lookup
//
// It shows the state dual_audio.lua keeps in user-data/dual_audio and changes
// it with `script-message-to dual_audio set-track|set-gain|set-delay SIDE VALUE`.
// The menu as built is mirrored as text in user-data/bubbatv/menu (for tests).
import Cocoa
import CoreAudio

struct Track: Sendable {
    let id: Int
    let title: String
}

struct Side: Sendable {
    let name: String
    let device: String?  // nil: mpv plays to the system default output
    let track: Int?
    let gain: Double     // dB
    let delay: Double    // ms
}

/// What dual_audio.lua publishes; `loaded` is false while it hasn't (yet).
struct State: Sendable {
    var loaded = false, enabled = false, active = false, pairs = false
    var tracks: [Track] = []
    var sides: [Side] = []

    init() {}

    init?(json: String) {
        guard let data = json.data(using: .utf8),
              let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
        loaded = true
        enabled = o["enabled"] as? Bool ?? false
        active = o["active"] as? Bool ?? false
        pairs = o["mode"] as? String == "pairs"
        tracks = (o["tracks"] as? [[String: Any]] ?? []).compactMap { t in
            guard let id = t["id"] as? Int else { return nil }
            return Track(id: id, title: t["title"] as? String ?? "[\(id)]")
        }
        sides = (o["sides"] as? [[String: Any]] ?? []).map { s in
            Side(name: s["name"] as? String ?? "?", device: s["device"] as? String,
                 track: s["track"] as? Int, gain: s["gain"] as? Double ?? 0,
                 delay: s["delay"] as? Double ?? 0)
        }
    }
}

/// The plugin's mpv handle. Menu items can still be clicked after mpv has
/// shut the plugin down, so every use checks it under a lock. Only the async
/// API is used from the main thread: mpv's core sometimes waits on the main
/// thread, and a blocking call from there would deadlock.
final class Mpv: @unchecked Sendable {
    private let lock = NSLock()
    private var handle: OpaquePointer?

    init(_ handle: OpaquePointer) { self.handle = handle }

    func command(_ args: String...) {
        lock.lock()
        defer { lock.unlock() }
        guard let handle else { return }
        let argv = args.map { strdup($0) }
        defer { argv.forEach { free($0) } }
        var cargs = argv.map { UnsafePointer($0) } + [nil]
        _ = mpv_command_async(handle, 0, &cargs)
    }

    func set(_ name: String, _ value: String) {
        lock.lock()
        defer { lock.unlock() }
        guard let handle else { return }
        value.withCString { v in
            var p: UnsafePointer<CChar>? = v
            _ = mpv_set_property_async(handle, 0, name, MPV_FORMAT_STRING, &p)
        }
    }

    func close() {
        lock.lock()
        handle = nil
        lock.unlock()
    }
}

/// The system's default output - where mpv plays unless told otherwise.
func defaultOutputName() -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var id = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id) == noErr
    else { return nil }
    addr.mSelector = kAudioObjectPropertyName
    var name: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &name) == noErr, let name else { return nil }
    return name.takeRetainedValue() as String
}

/// A menu row "Loudness  − ━━━●━━━ +  -2 dB". The text follows the knob;
/// the value goes out a moment after the last change, as each change
/// rebuilds the audio graph (a short gap in the sound). Clicking the value
/// resets it to 0.
final class SliderRow: NSView {
    private let title: String
    private let unit: String
    private let step: Double
    private let send: (Double) -> Void
    private let slider = NSSlider()
    private let value = NSButton(title: "", target: nil, action: nil)
    private var timer: Timer?

    init(_ title: String, _ range: ClosedRange<Double>, step: Double, unit: String,
         value v: Double, send: @escaping (Double) -> Void) {
        self.title = title
        self.unit = unit
        self.step = step
        self.send = send
        super.init(frame: NSRect(x: 0, y: 0, width: 340, height: 28))
        let font = NSFont.menuFont(ofSize: 0)
        let label = NSTextField(labelWithString: title)
        label.font = font
        let minus = NSButton(title: "−", target: self, action: #selector(down))
        let plus = NSButton(title: "+", target: self, action: #selector(up))
        for b in [minus, plus] { b.bezelStyle = .circular; b.controlSize = .small }
        slider.minValue = range.lowerBound
        slider.maxValue = range.upperBound
        slider.doubleValue = v
        slider.controlSize = .small
        slider.target = self
        slider.action = #selector(moved)
        value.isBordered = false
        value.font = font
        value.alignment = .right
        value.target = self
        value.action = #selector(reset)
        value.toolTip = "Click to reset to 0"
        label.frame = NSRect(x: 20, y: 6, width: 72, height: 16)
        minus.frame = NSRect(x: 92, y: 3, width: 22, height: 22)
        slider.frame = NSRect(x: 116, y: 4, width: 132, height: 20)
        plus.frame = NSRect(x: 250, y: 3, width: 22, height: 22)
        value.frame = NSRect(x: 272, y: 5, width: 60, height: 18)
        for v in [label, minus, slider, plus, value] as [NSView] { addSubview(v) }
        show(v)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// "Loudness -2 dB", as mirrored in user-data/bubbatv/menu.
    var text: String { title + " " + value.title }

    private func show(_ v: Double) {
        value.title = v == 0 ? "0 \(unit)" : String(format: "%+g %@", v, unit)
    }

    private func change(to v: Double) {
        let v = min(max((v / step).rounded() * step, slider.minValue), slider.maxValue)
        slider.doubleValue = v
        show(v)
        timer?.invalidate()
        // The menu is being tracked while it is open: .common includes that run loop mode.
        let t = Timer(timeInterval: 0.35, repeats: false) { [send] _ in send(v) }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    @objc private func moved() { change(to: slider.doubleValue) }
    @objc private func down() { change(to: slider.doubleValue - step) }
    @objc private func up() { change(to: slider.doubleValue + step) }
    @objc private func reset() { change(to: 0) }
}

@MainActor
final class BubbaMenu: NSObject, NSMenuDelegate {
    private let mpv: Mpv
    private let menu = NSMenu(title: "bubbaTV")
    private var state = State()
    private var open = false

    init(_ mpv: Mpv) {
        self.mpv = mpv
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
    }

    /// mpv builds its menu bar as it starts up, so wait for it (up to ~10 s).
    func install(tries: Int = 50) {
        guard let main = NSApp?.mainMenu, !main.items.isEmpty else {
            if tries > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.install(tries: tries - 1) }
            }
            return
        }
        let item = NSMenuItem(title: "bubbaTV", action: nil, keyEquivalent: "")
        item.submenu = menu
        // Before Window and Help, where an app's own menus go.
        main.insertItem(item, at: main.items.firstIndex { $0.title == "Window" } ?? main.items.count)
        mpv.set("user-data/bubbatv/menubar", main.items.map(\.title).joined(separator: ","))
        rebuild()
    }

    func update(_ state: State) {
        self.state = state
        // Not under the pointer: rebuilding would pull a slider out of a drag.
        if !open { rebuild() }
    }

    func menuWillOpen(_ menu: NSMenu) { if menu === self.menu { open = true } }
    func menuDidClose(_ menu: NSMenu) { if menu === self.menu { open = false; rebuild() } }

    private func rebuild() {
        menu.removeAllItems()
        let toggle = item("Dual audio", #selector(toggle))
        toggle.state = state.enabled ? .on : .off
        toggle.isEnabled = state.loaded
        menu.addItem(toggle)
        if !state.loaded {
            menu.addItem(disabled("dual_audio.lua is not loaded"))
        } else if state.enabled && !state.active {
            menu.addItem(disabled("Needs 2+ audio tracks"))
        }
        // Off, everyone hears the one normal track: nothing to set per side.
        for (i, side) in state.sides.enumerated() where state.active {
            menu.addItem(.separator())
            menu.addItem(header(side.name))
            menu.addItem(disabled("Device: " + (side.device ?? defaultOutputName() ?? "system output")))
            menu.addItem(tracks(for: side, i + 1))
            menu.addItem(row(SliderRow("Loudness", -30...6, step: 1, unit: "dB", value: side.gain) { [mpv] v in
                mpv.command("script-message-to", "dual_audio", "set-gain", "\(i + 1)", "\(v)")
            }))
            menu.addItem(row(SliderRow("Delay", -1000...1000, step: 10, unit: "ms", value: side.delay) { [mpv] v in
                mpv.command("script-message-to", "dual_audio", "set-delay", "\(i + 1)", "\(Int(v))")
            }))
        }
        if state.active {
            menu.addItem(.separator())
            menu.addItem(item(state.pairs ? "Swap pairs" : "Swap ears", #selector(swap)))
        }
        mirror()
    }

    /// "Audio track: [1] English (eng)", with every track to pick from.
    private func tracks(for side: Side, _ n: Int) -> NSMenuItem {
        let current = state.tracks.first { $0.id == side.track }?.title ?? "none"
        let parent = NSMenuItem(title: "Audio track: " + current, action: nil, keyEquivalent: "")
        let sub = NSMenu()
        sub.autoenablesItems = false
        for t in state.tracks {
            let it = item(t.title, #selector(pick(_:)))
            it.representedObject = [n, t.id]
            it.state = t.id == side.track ? .on : .off
            sub.addItem(it)
        }
        parent.submenu = sub
        return parent
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: "")
        it.target = self
        return it
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        it.isEnabled = false
        return it
    }

    private func header(_ title: String) -> NSMenuItem {
        if #available(macOS 14, *) { return NSMenuItem.sectionHeader(title: title) }
        return disabled(title)
    }

    private func row(_ view: SliderRow) -> NSMenuItem {
        let it = NSMenuItem()
        it.view = view
        return it
    }

    @objc private func toggle() { mpv.command("script-binding", "dual_audio/toggle") }
    @objc private func swap() { mpv.command("script-binding", "dual_audio/swap") }

    @objc private func pick(_ sender: NSMenuItem) {
        guard let v = sender.representedObject as? [Int] else { return }
        mpv.command("script-message-to", "dual_audio", "set-track", "\(v[0])", "\(v[1])")
    }

    /// The menu as text, one item per line: "[x]" marks checked items,
    /// submenus are indented.
    private func mirror() {
        func lines(_ m: NSMenu, _ indent: String) -> [String] {
            m.items.flatMap { it -> [String] in
                if it.isSeparatorItem { return [indent + "---"] }
                let text = (it.view as? SliderRow)?.text ?? it.title
                let line = indent + (it.state == .on ? "[x] " : "") + text
                    + (it.isEnabled || it.view != nil ? "" : " (disabled)")
                return [line] + (it.submenu.map { lines($0, indent + "  ") } ?? [])
            }
        }
        mpv.set("user-data/bubbatv/menu", lines(menu, "").joined(separator: "\n"))
    }
}

@MainActor var bubba: BubbaMenu?

@_cdecl("mpv_open_cplugin")
public func mpvOpenCplugin(_ handle: OpaquePointer?) -> Int32 {
    guard let handle else { return -1 }
    let mpv = Mpv(handle)
    DispatchQueue.main.async {
        bubba = BubbaMenu(mpv)
        bubba?.install()
    }
    mpv_observe_property(handle, 0, "user-data/dual_audio", MPV_FORMAT_STRING)
    while true {
        let event = mpv_wait_event(handle, -1).pointee
        if event.event_id == MPV_EVENT_SHUTDOWN { break }
        guard event.event_id == MPV_EVENT_PROPERTY_CHANGE,
              let prop = event.data?.assumingMemoryBound(to: mpv_event_property.self).pointee
        else { continue }
        var state = State()
        if prop.format == MPV_FORMAT_STRING,
           let json = prop.data?.assumingMemoryBound(to: UnsafePointer<CChar>?.self).pointee {
            state = State(json: String(cString: json)) ?? State()
        }
        DispatchQueue.main.async { bubba?.update(state) }
    }
    mpv.close()
    return 0
}
