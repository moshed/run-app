import SwiftUI
import ServiceManagement

// MARK: - Shortcut Recorder

class ShortcutRecorderMonitor {
    static let shared = ShortcutRecorderMonitor()
    private var monitor: Any?

    func start(onRecord: @escaping (GlobalShortcut) -> Void, onCancel: @escaping () -> Void) {
        stop()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 { // escape = cancel
                onCancel()
                self?.stop()
                return nil
            }
            let mods = event.modifierFlags.intersection([.command, .option, .control, .shift])
            guard !mods.isEmpty else { return nil } // require at least one modifier
            let shortcut = GlobalShortcut(keyCode: Int(event.keyCode), modifiers: mods.rawValue)
            onRecord(shortcut)
            self?.stop()
            return nil
        }
    }

    func stop() {
        if let m = monitor {
            NSEvent.removeMonitor(m)
            monitor = nil
        }
    }
}

struct ShortcutRecorderButton: View {
    let label: String
    @Binding var shortcut: GlobalShortcut
    @State private var isRecording = false

    var body: some View {
        LabeledContent(label) {
            HStack(spacing: 6) {
                Button(action: startRecording) {
                    Text(isRecording ? "Type shortcut\u{2026}" : shortcut.displayString)
                        .frame(minWidth: 80)
                }
                .buttonStyle(.bordered)

                if !shortcut.isEmpty && !isRecording {
                    Button(action: { shortcut = .empty }) {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func startRecording() {
        isRecording = true
        ShortcutRecorderMonitor.shared.start(
            onRecord: { recorded in
                shortcut = recorded
                isRecording = false
            },
            onCancel: {
                isRecording = false
            }
        )
    }
}

// MARK: - Settings View

struct SettingsView: View {
    let state: AppState
    let onSettingsChanged: () -> Void

    @State private var defaultDevices: Set<Int> = []
    @State private var hotkeyEnabled: Bool = true
    @State private var menuShortcut: GlobalShortcut = .empty
    @State private var rerunShortcut: GlobalShortcut = .empty

    var body: some View {
        Form {
            Section("Default Devices") {
                ForEach(Array(DeviceInfo.all.enumerated()), id: \.offset) { index, device in
                    Toggle(device.name, isOn: Binding(
                        get: { defaultDevices.contains(index) },
                        set: { newValue in
                            if newValue { defaultDevices.insert(index) } else { defaultDevices.remove(index) }
                        }
                    ))
                }
            }

            Section("Global Shortcuts") {
                Toggle("Enable Global Shortcuts", isOn: $hotkeyEnabled)

                ShortcutRecorderButton(label: "Show RunApp", shortcut: $menuShortcut)
                ShortcutRecorderButton(label: "Re-run Last", shortcut: $rerunShortcut)
            }

            Section("General") {
                Toggle("Launch at Login", isOn: Binding(
                    get: { SMAppService.mainApp.status == .enabled },
                    set: { newValue in
                        do {
                            if newValue { try SMAppService.mainApp.register() }
                            else { try SMAppService.mainApp.unregister() }
                        } catch {}
                    }
                ))
            }
        }
        .formStyle(.grouped)
        .frame(width: 350, height: 320)
        .onAppear {
            defaultDevices = Set(state.settings.defaultDeviceIndices)
            hotkeyEnabled = state.settings.hotkeyEnabled
            menuShortcut = state.settings.menuShortcut
            rerunShortcut = state.settings.rerunShortcut
        }
        .onChange(of: defaultDevices) { _, newValue in
            state.settings.defaultDeviceIndices = Array(newValue).sorted()
            state.saveSettings()
        }
        .onChange(of: hotkeyEnabled) { _, newValue in
            state.settings.hotkeyEnabled = newValue
            state.saveSettings()
            onSettingsChanged()
        }
        .onChange(of: menuShortcut) { _, newValue in
            state.settings.menuShortcut = newValue
            state.saveSettings()
            onSettingsChanged()
        }
        .onChange(of: rerunShortcut) { _, newValue in
            state.settings.rerunShortcut = newValue
            state.saveSettings()
            onSettingsChanged()
        }
    }
}
