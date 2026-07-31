import Cocoa
import SwiftUI
import ServiceManagement

@main
class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var viewModel: PopupViewModel!
    private var globalMonitor: Any?
    private var localMonitor: Any?
    private var settingsWindow: NSWindow?

    let state = AppState()

    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = AppDelegate()
        app.delegate = delegate

        let mainMenu = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "Relaunch", action: #selector(AppDelegate.relaunchApp(_:)), keyEquivalent: "r"))
        appMenu.addItem(NSMenuItem(title: "Quit RunApp", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        let appMenuItem = NSMenuItem()
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)
        app.mainMenu = mainMenu

        app.run()
    }

    // MARK: - App Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: "RunApp")
            button.action = #selector(togglePopover(_:))
            button.target = self
        }

        viewModel = PopupViewModel(state: state)
        viewModel.onDismiss = { [weak self] in self?.closePopover() }
        viewModel.onLaunch = { [weak self] app, devices in self?.launchApp(app, on: devices) }
        viewModel.onOpenSettings = { [weak self] in
            self?.closePopover()
            self?.openSettings()
        }
        viewModel.onRelaunch = { [weak self] in self?.relaunchApp(nil) }

        popover = NSPopover()
        popover.contentSize = NSSize(width: 280, height: 400)
        popover.behavior = .transient
        popover.delegate = self
        popover.contentViewController = NSHostingController(rootView: PopupView(viewModel: viewModel))

        installGlobalMonitor()
        state.requestNotificationPermission()

        // Refresh the app list from disk (auto-discovers any newly added iOS
        // app) so the menu stays in sync without editing source.
        DispatchQueue.global(qos: .utility).async { AppInfo.reload() }
    }

    // MARK: - Popover

    @objc private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            closePopover()
        } else {
            showPopover()
        }
    }

    private func showPopover() {
        guard let button = statusItem.button else { return }
        viewModel.reset()
        viewModel.refreshApps()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        installLocalMonitor()
        popover.contentViewController?.view.window?.makeKey()
    }

    private func closePopover() {
        popover.performClose(nil)
    }

    func popoverDidClose(_ notification: Notification) {
        removeLocalMonitor()
    }

    // MARK: - Local Keyboard Monitor

    private func installLocalMonitor() {
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, self.popover.isShown else { return event }
            return self.handleKey(event)
        }
    }

    private func removeLocalMonitor() {
        if let m = localMonitor {
            NSEvent.removeMonitor(m)
            localMonitor = nil
        }
    }

    private func handleKey(_ event: NSEvent) -> NSEvent? {
        switch event.keyCode {
        case 126: // up
            viewModel.moveUp()
            return nil
        case 125: // down
            viewModel.moveDown()
            return nil
        case 36: // return
            viewModel.confirm()
            return nil
        case 124: // right arrow → open device picker
            if case .apps = viewModel.mode {
                viewModel.openDevicePicker()
                return nil
            }
            return event
        case 123: // left arrow → go back
            if case .devices = viewModel.mode {
                viewModel.goBack()
                return nil
            }
            return event
        case 49: // space
            if case .devices = viewModel.mode {
                viewModel.toggleSelection()
            } else if case .apps = viewModel.mode {
                viewModel.typeCharacter(" ")
            }
            return nil
        case 51: // backspace
            if case .apps = viewModel.mode {
                if viewModel.filter.isEmpty {
                    return event
                }
                viewModel.deleteCharacter()
                return nil
            }
            return event
        case 53: // escape
            if case .apps = viewModel.mode, !viewModel.filter.isEmpty {
                viewModel.filter = ""
                viewModel.cursor = 0
                return nil
            }
            if case .devices = viewModel.mode {
                viewModel.goBack()
                return nil
            }
            return event
        default:
            // In apps mode, printable characters filter the list
            if case .apps = viewModel.mode, let chars = event.characters,
               !chars.isEmpty, event.modifierFlags.intersection([.command, .control]).isEmpty {
                viewModel.typeCharacter(chars)
                return nil
            }
            return event
        }
    }

    // MARK: - Launch

    private func launchApp(_ app: AppInfo, on devices: [DeviceInfo]) {
        for device in devices {
            state.buildAndLaunch(app: app, device: device) {}
        }
    }

    private func runLast() {
        guard let lastName = state.history.first,
              let app = AppInfo.all.first(where: { $0.name == lastName }) else { return }
        let indices = state.devicesFor(appName: lastName)
        let devices = indices.compactMap { $0 < DeviceInfo.all.count ? DeviceInfo.all[$0] : nil }
        launchApp(app, on: devices)
    }

    // MARK: - Settings

    private func openSettings() {
        if let window = settingsWindow, window.isVisible {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let hostingController = NSHostingController(
            rootView: SettingsView(state: state, onSettingsChanged: { [weak self] in
                self?.installGlobalMonitor()
            })
        )
        let window = NSWindow(contentViewController: hostingController)
        window.title = "RunApp Settings"
        window.styleMask = [.titled, .closable]
        window.setContentSize(NSSize(width: 350, height: 350))
        window.center()
        window.isReleasedWhenClosed = false
        settingsWindow = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Relaunch

    @objc func relaunchApp(_ sender: Any?) {
        let bundlePath = Bundle.main.bundlePath
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/sh")
        task.arguments = ["-c", "sleep 0.5 && open \"\(bundlePath)\""]
        try? task.run()
        NSApp.terminate(nil)
    }

    // MARK: - Global Hotkeys

    func installGlobalMonitor() {
        if let m = globalMonitor {
            NSEvent.removeMonitor(m)
            globalMonitor = nil
        }

        guard state.settings.hotkeyEnabled else { return }

        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self else { return }

            let menuSC = self.state.settings.menuShortcut
            let rerunSC = self.state.settings.rerunShortcut

            if menuSC.matches(event: event) {
                DispatchQueue.main.async { self.togglePopover(nil) }
            } else if rerunSC.matches(event: event) {
                self.runLast()
            }
        }
    }
}
