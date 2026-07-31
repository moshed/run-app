import Cocoa
import UserNotifications

class AppState {
    var settings: Settings
    private(set) var history: [String]
    /// Per-app last-used device indices, e.g. ["Jukebox": [0, 1]]. Shared with
    /// the CLI via ~/.cache/run-app-app-devices.json.
    private(set) var appDevices: [String: [Int]]
    private var buildingKeys: Set<String> = []
    private let lock = NSLock()

    private let historyURL: URL
    private let settingsURL: URL
    private let appDevicesURL: URL

    struct Settings: Codable {
        var hotkeyEnabled: Bool
        var defaultDeviceIndices: [Int]
        var menuShortcut: GlobalShortcut
        var rerunShortcut: GlobalShortcut

        enum CodingKeys: String, CodingKey {
            case hotkeyEnabled = "hotkey_enabled"
            case defaultDeviceIndices = "default_devices"
            case menuShortcut = "menu_shortcut"
            case rerunShortcut = "rerun_shortcut"
        }

        init(hotkeyEnabled: Bool = true, defaultDeviceIndices: [Int] = [0],
             menuShortcut: GlobalShortcut? = nil, rerunShortcut: GlobalShortcut? = nil) {
            self.hotkeyEnabled = hotkeyEnabled
            self.defaultDeviceIndices = defaultDeviceIndices
            self.menuShortcut = menuShortcut ?? GlobalShortcut(keyCode: 50, modifiers: NSEvent.ModifierFlags.option.rawValue)
            self.rerunShortcut = rerunShortcut ?? GlobalShortcut(keyCode: 29, modifiers: NSEvent.ModifierFlags.option.rawValue)
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            hotkeyEnabled = try container.decodeIfPresent(Bool.self, forKey: .hotkeyEnabled) ?? true
            defaultDeviceIndices = try container.decodeIfPresent([Int].self, forKey: .defaultDeviceIndices) ?? [0]
            menuShortcut = try container.decodeIfPresent(GlobalShortcut.self, forKey: .menuShortcut)
                ?? GlobalShortcut(keyCode: 50, modifiers: NSEvent.ModifierFlags.option.rawValue)
            rerunShortcut = try container.decodeIfPresent(GlobalShortcut.self, forKey: .rerunShortcut)
                ?? GlobalShortcut(keyCode: 29, modifiers: NSEvent.ModifierFlags.option.rawValue)
        }
    }

    init() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        historyURL = home.appendingPathComponent(".cache/run-app-history.json")
        settingsURL = home.appendingPathComponent(".config/run-app/settings.json")
        appDevicesURL = home.appendingPathComponent(".cache/run-app-app-devices.json")

        if let data = try? Data(contentsOf: settingsURL),
           let s = try? JSONDecoder().decode(Settings.self, from: data) {
            settings = s
        } else {
            settings = Settings()
        }

        if let data = try? Data(contentsOf: historyURL),
           let h = try? JSONDecoder().decode([String].self, from: data) {
            history = h
        } else {
            history = []
        }

        if let data = try? Data(contentsOf: appDevicesURL),
           let d = try? JSONDecoder().decode([String: [Int]].self, from: data) {
            appDevices = d
        } else {
            appDevices = [:]
        }
    }

    // MARK: - Persistence

    func saveSettings() {
        let dir = settingsURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = .prettyPrinted
        if let data = try? encoder.encode(settings) {
            try? data.write(to: settingsURL)
        }
    }

    func saveHistory() {
        let dir = historyURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(history) {
            try? data.write(to: historyURL)
        }
    }

    // MARK: - Per-app device memory

    /// Device indices this app last launched on, filtered to valid entries.
    /// Falls back to the global default, then device 0, if never launched.
    func devicesFor(appName: String) -> [Int] {
        let remembered = (appDevices[appName] ?? []).filter { $0 >= 0 && $0 < DeviceInfo.all.count }
        if !remembered.isEmpty { return remembered }
        let fallback = settings.defaultDeviceIndices.filter { $0 >= 0 && $0 < DeviceInfo.all.count }
        return fallback.isEmpty ? [0] : fallback
    }

    /// Remember the device set an app just launched on (persists immediately).
    func recordDevices(appName: String, indices: [Int]) {
        guard !indices.isEmpty else { return }
        appDevices[appName] = indices.sorted()
        let dir = appDevicesURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(appDevices) {
            try? data.write(to: appDevicesURL)
        }
    }

    // MARK: - Sorted Apps

    var sortedApps: [AppInfo] {
        AppInfo.all.sorted { a, b in
            let aIdx = history.firstIndex(of: a.name)
            let bIdx = history.firstIndex(of: b.name)
            switch (aIdx, bIdx) {
            case let (.some(ai), .some(bi)): return ai < bi
            case (.some, .none): return true
            case (.none, .some): return false
            case (.none, .none): return a.name.lowercased() < b.name.lowercased()
            }
        }
    }

    // MARK: - Build State

    func isBuilding(appName: String, deviceName: String? = nil) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if let deviceName = deviceName {
            return buildingKeys.contains("\(appName):\(deviceName)")
        }
        return buildingKeys.contains { $0.hasPrefix("\(appName):") }
    }

    func buildAndLaunch(app: AppInfo, device: DeviceInfo, completion: @escaping () -> Void) {
        let key = "\(app.name):\(device.name)"

        lock.lock()
        guard !buildingKeys.contains(key) else {
            lock.unlock()
            return
        }
        buildingKeys.insert(key)
        lock.unlock()

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            doBuildAndLaunch(app: app, device: device)

            lock.lock()
            buildingKeys.remove(key)
            lock.unlock()

            if let idx = history.firstIndex(of: app.name) {
                history.remove(at: idx)
            }
            history.insert(app.name, at: 0)
            saveHistory()

            completion()
        }
    }

    // MARK: - Build & Launch

    private func doBuildAndLaunch(app: AppInfo, device: DeviceInfo) {
        notify(title: "run-app", body: "Building \(app.name) for \(device.name)...")

        let deviceIndex = DeviceInfo.all.firstIndex(of: device).map { $0 + 1 } ?? 1

        // Delegate to run-app CLI which handles build, install, launch, and log streaming.
        // -stream prints plain text logs to stdout + log file (no curses). Process stays alive.
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/local/bin/run-app")
        task.arguments = ["--name", app.name, "\(deviceIndex)", "-stream"]

        // Capture output to detect build failures
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe

        do { try task.run() } catch {
            notify(title: "run-app FAILED", body: "\(app.name): \(error.localizedDescription)")
            return
        }

        // Wait up to 5 minutes for the build+install portion, then let it run.
        // -stream keeps the process alive writing logs to stdout + log file.
        var output = ""
        var didDetectFailure = false
        let startTime = Date()
        while task.isRunning && Date().timeIntervalSince(startTime) < 300 {
            let data = pipe.fileHandleForReading.availableData
            if !data.isEmpty, let str = String(data: data, encoding: .utf8) {
                output += str
                // Check if we've passed the build+launch phase
                if output.contains("Streaming logs...") || output.contains("Launched on") {
                    break
                }
                if output.contains("BUILD FAILED") || output.contains("build failed") {
                    showFailureAlert(
                        title: "Build failed: \(app.name)",
                        message: "Build did not produce a runnable .app. Open the project in Xcode to see the full compile log.",
                        fullOutput: output
                    )
                    task.terminate()
                    didDetectFailure = true
                    return
                }
                // Install or launch failure markers emitted by the Python CLI.
                // The CLI also pops its own osascript alert; we just stop here
                // so the menubar doesn't fire a false "Launched ✓" follow-up.
                if output.contains("Install failed on") ||
                   output.contains("Launch failed on") ||
                   output.contains("No devices launched successfully") {
                    showFailureAlert(
                        title: "\(output.contains("Install failed") ? "Install" : "Launch") failed: \(app.name) → \(device.name)",
                        message: shortDiagnosis(from: output),
                        fullOutput: output
                    )
                    didDetectFailure = true
                    // Don't terminate — CLI is wrapping up; let it exit naturally.
                    break
                }
            }
            Thread.sleep(forTimeInterval: 0.5)
        }

        if !didDetectFailure {
            notify(title: "run-app", body: "Launched \(app.name) on \(device.name)")
        }
    }

    // MARK: - Failure presentation

    /// Pops a macOS alert with a Copy Output button. Native AppKit version of
    /// the CLI's osascript helper — same UX, same Copy → pbcopy behavior.
    private func showFailureAlert(title: String, message: String, fullOutput: String) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = title
            alert.informativeText = message
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Copy Output")
            // Bring the menubar app forward so the alert shows on top.
            NSApp.activate(ignoringOtherApps: true)
            let response = alert.runModal()
            if response == .alertSecondButtonReturn {
                let pb = NSPasteboard.general
                pb.clearContents()
                pb.setString(fullOutput, forType: .string)
            }
        }
    }

    /// Pulls a one-liner explanation out of CLI stderr so the alert body is
    /// useful at a glance (full output still available via Copy Output).
    private func shortDiagnosis(from output: String) -> String {
        let lower = output.lowercased()
        if lower.contains("could not be, unlocked") || lower.contains("fbsopenapplicationerrordomain") {
            return "Device is locked. Unlock the phone and try again."
        }
        if lower.contains("unable to locate") || lower.contains("unavailable") {
            return "The device is unavailable. Unlock the phone, plug it in via USB, or check the Devices list in Xcode."
        }
        if lower.contains("connection reset by peer") {
            return "Connection to the device dropped. Try again — usually fine on retry."
        }
        if lower.contains("could not be installed") {
            return "iOS rejected the install. Check provisioning, signing, or that the .app was archived for the device."
        }
        return "Install or launch failed. Tap Copy Output for the full error log."
    }

    // MARK: - Shell

    @discardableResult
    func shell(_ command: String) -> (output: String, exitCode: Int32) {
        let task = Process()
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        task.executableURL = URL(fileURLWithPath: "/bin/zsh")
        task.arguments = ["-c", command]

        do { try task.run() } catch { return ("", 1) }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()

        return (String(data: data, encoding: .utf8) ?? "", task.terminationStatus)
    }

    // MARK: - Notifications

    func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func notify(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
