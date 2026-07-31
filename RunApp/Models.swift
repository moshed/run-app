import Cocoa

struct AppInfo: Hashable, Codable {
    let name: String
    let path: String
    let scheme: String
    let bundle: String
    let log: String

    // Shared source of truth with the CLI. run.py auto-discovers iOS apps under
    // /Users/moshe/Apps and writes this file; we read it so a newly added app
    // shows up here too — no manual list to maintain.
    static let jsonPath = "/Users/moshe/Apps/run-app/apps.json"
    static let runPy = "/Users/moshe/Apps/run-app/run.py"

    static var all: [AppInfo] = load()

    /// Read apps.json from disk. Returns [] if missing/unreadable.
    static func load() -> [AppInfo] {
        guard let data = FileManager.default.contents(atPath: jsonPath),
              let apps = try? JSONDecoder().decode([AppInfo].self, from: data)
        else { return [] }
        return apps
    }

    /// Re-run discovery (python, ~100ms) so apps added since launch are picked
    /// up, then return the fresh list. Call off the main thread — it shells out
    /// to run.py. Returns [] if the scan failed.
    static func rescan() -> [AppInfo] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", runPy, "--emit-apps"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
        p.waitUntilExit()
        return load()
    }

    /// Background rescan that publishes the result on the main thread.
    static func reload() {
        let fresh = rescan()
        guard !fresh.isEmpty else { return }
        DispatchQueue.main.async { all = fresh }
    }
}

struct DeviceInfo: Hashable {
    let name: String
    let short: String
    let udid: String
    let hwUdid: String?
    let type: DeviceType

    enum DeviceType: String, Hashable {
        case physical
        case simulator
    }

    static let all: [DeviceInfo] = [
        DeviceInfo(name: "Moshe\u{2019}s iPhone",  short: "MOSHE",  udid: "0C449D4B-C525-5E08-B643-0FEB379A1FFF", hwUdid: "00008150-001A096E1AF8401C", type: .physical),
        DeviceInfo(name: "Summit\u{2019}s iPhone", short: "SUMMIT", udid: "49160EDB-AA57-52AA-A592-BE81B2B29D05", hwUdid: "00008110-001849EA14A1A01E", type: .physical),
        DeviceInfo(name: "Simulator",              short: "SIM",    udid: "EACEFB3A-1643-4100-82A1-80410DD87344", hwUdid: nil,                          type: .simulator),
    ]
}

struct GlobalShortcut: Codable, Equatable {
    var keyCode: Int
    var modifiers: UInt

    var isEmpty: Bool { keyCode == 0 && modifiers == 0 }

    static let empty = GlobalShortcut(keyCode: 0, modifiers: 0)

    func matches(event: NSEvent) -> Bool {
        guard !isEmpty else { return false }
        let activeMods = event.modifierFlags.intersection([.command, .option, .control, .shift])
        return Int(event.keyCode) == keyCode && activeMods.rawValue == modifiers
    }

    var displayString: String {
        if isEmpty { return "Not Set" }
        var s = ""
        let flags = NSEvent.ModifierFlags(rawValue: modifiers)
        if flags.contains(.control) { s += "\u{2303}" }
        if flags.contains(.option) { s += "\u{2325}" }
        if flags.contains(.shift) { s += "\u{21E7}" }
        if flags.contains(.command) { s += "\u{2318}" }
        s += keyName
        return s
    }

    private var keyName: String {
        let names: [Int: String] = [
            0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X",
            8: "C", 9: "V", 11: "B", 12: "Q", 13: "W", 14: "E", 15: "R",
            16: "Y", 17: "T", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6",
            23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0",
            30: "]", 31: "O", 32: "U", 33: "[", 34: "I", 35: "P",
            37: "L", 38: "J", 39: "'", 40: "K", 41: ";",
            42: "\\", 43: ",", 44: "/", 45: "N", 46: "M", 47: ".",
            48: "Tab", 49: "Space", 50: "`",
        ]
        return names[keyCode] ?? "Key\(keyCode)"
    }
}
