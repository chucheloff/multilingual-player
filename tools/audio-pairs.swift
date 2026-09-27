// audio-pairs - combine two headphone outputs into one 4-channel CoreAudio device.
//
//   audio-pairs list                 connected output devices
//   audio-pairs create [NAME NAME]   build the combined device; prints UID<TAB>name1<TAB>name2
//   audio-pairs destroy              remove it again
//
// Without names, `create` uses the two connected Bluetooth outputs. Channels
// 1-2 go to the first device, 3-4 to the second. They are labelled L, R, Ls,
// Rs so mpv sees a plain "quad" layout - with the default labels (L R L R)
// mpv would fall back to stereo and mix both languages into both pairs.
import CoreAudio
import Foundation

let aggregateUID = "com.multilingual-player.audio-pairs"
let aggregateName = "Dual headphones (dualplay)"

struct Device {
    let id: AudioObjectID
    let uid: String
    let name: String
    let transport: UInt32
    let outputChannels: Int

    var isBluetooth: Bool {
        transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE
    }
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("audio-pairs: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func string(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr, let value else { return nil }
    return value.takeRetainedValue() as String
}

func uint32(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32 {
    var addr = address(selector)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value)
    return value
}

func outputChannels(_ id: AudioObjectID) -> Int {
    var addr = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeOutput)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    let list = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, list) == noErr else { return 0 }
    return UnsafeMutableAudioBufferListPointer(list).reduce(0) { $0 + Int($1.mNumberChannels) }
}

func devices() -> [Device] {
    var addr = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids.map { id in
        Device(id: id, uid: string(id, kAudioDevicePropertyDeviceUID) ?? "",
               name: string(id, kAudioObjectPropertyName) ?? "?",
               transport: uint32(id, kAudioDevicePropertyTransportType),
               outputChannels: outputChannels(id))
    }
}

func outputs() -> [Device] {
    devices().filter { $0.outputChannels > 0 && $0.uid != aggregateUID }
}

func describe(_ list: [Device]) -> String {
    list.map { "  \($0.name) (\($0.outputChannels) ch\($0.isBluetooth ? ", Bluetooth" : ""))" }
        .joined(separator: "\n")
}

// Lets the HAL deliver its notifications on its own thread; without this a
// command-line tool can see a stale device list.
func detachHALRunLoop() {
    var addr = address(kAudioHardwarePropertyRunLoop)
    var runLoop: UnsafeRawPointer? = nil  // a NULL CFRunLoopRef
    AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil,
                               UInt32(MemoryLayout<UnsafeRawPointer?>.size), &runLoop)
}

func destroy() {
    for d in devices() where d.uid == aggregateUID {
        AudioHardwareDestroyAggregateDevice(d.id)
    }
}

func pick(_ names: [String]) -> [Device] {
    let all = outputs()
    if names.isEmpty {
        let bluetooth = all.filter(\.isBluetooth).sorted { $0.name < $1.name }
        guard bluetooth.count == 2 else {
            fail("need exactly 2 connected Bluetooth outputs, found \(bluetooth.count). Outputs:\n"
                 + describe(all) + "\nConnect both pairs, or name the two devices. A pair that is connected"
                 + "\nbut missing here: pick it once in Control Center > Sound.")
        }
        return bluetooth
    }
    let chosen = names.map { name in
        let matches = all.filter { $0.name.localizedCaseInsensitiveContains(name) }
        guard matches.count == 1 else {
            fail("\"\(name)\" matches \(matches.count) outputs. Outputs:\n" + describe(all))
        }
        return matches[0]
    }
    guard chosen[0].uid != chosen[1].uid else { fail("both names match \(chosen[0].name)") }
    return chosen
}

func setLayout(_ id: AudioObjectID, _ labels: [AudioChannelLabel]) {
    let descOffset = MemoryLayout<AudioChannelLayout>.offset(of: \.mChannelDescriptions)!
    let size = descOffset + labels.count * MemoryLayout<AudioChannelDescription>.stride
    let raw = UnsafeMutableRawPointer.allocate(byteCount: size, alignment: MemoryLayout<AudioChannelLayout>.alignment)
    defer { raw.deallocate() }
    raw.initializeMemory(as: UInt8.self, repeating: 0, count: size)
    let layout = raw.assumingMemoryBound(to: AudioChannelLayout.self)
    layout.pointee.mChannelLayoutTag = kAudioChannelLayoutTag_UseChannelDescriptions
    layout.pointee.mNumberChannelDescriptions = UInt32(labels.count)
    let descs = (raw + descOffset).assumingMemoryBound(to: AudioChannelDescription.self)
    for (i, label) in labels.enumerated() {
        descs[i].mChannelLabel = label
    }
    var addr = address(kAudioDevicePropertyPreferredChannelLayout, kAudioObjectPropertyScopeOutput)
    let status = AudioObjectSetPropertyData(id, &addr, 0, nil, UInt32(size), raw)
    if status != noErr {
        fail("could not set the channel layout (OSStatus \(status))")
    }
}

func create(_ names: [String]) {
    let pair = pick(names)
    destroy()  // left over from a run that was killed
    let description: [String: Any] = [
        kAudioAggregateDeviceNameKey: aggregateName,
        kAudioAggregateDeviceUIDKey: aggregateUID,
        kAudioAggregateDeviceMainSubDeviceKey: pair[0].uid,
        kAudioAggregateDeviceSubDeviceListKey: pair.enumerated().map { i, d in
            // The first device is the clock; the second follows it.
            [kAudioSubDeviceUIDKey: d.uid, kAudioSubDeviceDriftCompensationKey: i == 0 ? 0 : 1]
        },
    ]
    var id = AudioObjectID(0)
    let status = AudioHardwareCreateAggregateDevice(description as CFDictionary, &id)
    guard status == noErr else { fail("could not create the combined device (OSStatus \(status))") }

    // Sub-devices attach asynchronously.
    let expected = pair.reduce(0) { $0 + $1.outputChannels }
    let deadline = Date().addingTimeInterval(5)
    while outputChannels(id) != expected && Date() < deadline {
        usleep(50_000)
    }
    guard outputChannels(id) == 4, expected == 4 else {
        destroy()
        fail("expected 4 output channels, got \(outputChannels(id)) (devices: "
             + pair.map { "\($0.name) \($0.outputChannels) ch" }.joined(separator: ", ") + ")")
    }
    setLayout(id, [kAudioChannelLabel_Left, kAudioChannelLabel_Right,
                   kAudioChannelLabel_LeftSurround, kAudioChannelLabel_RightSurround])
    print([aggregateUID, pair[0].name, pair[1].name].joined(separator: "\t"))
}

detachHALRunLoop()
let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "list":
    print(describe(outputs()))
case "create" where args.count == 1 || args.count == 3:
    create(Array(args.dropFirst()))
case "destroy":
    destroy()
default:
    fail("usage: audio-pairs list | create [NAME NAME] | destroy")
}
