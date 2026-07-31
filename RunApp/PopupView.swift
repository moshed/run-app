import SwiftUI

// MARK: - View Model

class PopupViewModel: ObservableObject {
    enum Mode: Equatable {
        case apps
        case devices(AppInfo)
    }

    @Published var mode: Mode = .apps
    @Published var cursor: Int = 0
    @Published var selectedDevices: Set<Int> = []
    @Published var filter: String = ""
    /// Bumped whenever the app list changes; re-renders the list.
    @Published private(set) var appsRevision: Int = 0

    let state: AppState
    var onDismiss: (() -> Void)?
    var onLaunch: ((AppInfo, [DeviceInfo]) -> Void)?
    var onOpenSettings: (() -> Void)?
    var onRelaunch: (() -> Void)?

    var apps: [AppInfo] {
        let all = state.sortedApps
        if filter.isEmpty { return all }
        return all.filter { $0.name.localizedCaseInsensitiveContains(filter) }
    }

    init(state: AppState) {
        self.state = state
    }

    func moveUp() {
        if cursor > 0 { cursor -= 1 }
    }

    func moveDown() {
        let count: Int
        switch mode {
        case .apps: count = apps.count
        case .devices: count = DeviceInfo.all.count
        }
        if cursor < count - 1 { cursor += 1 }
    }

    /// Enter key — apps: run on default. Devices: launch on selected.
    func confirm() {
        switch mode {
        case .apps:
            runOnDefault()
        case .devices(let app):
            let indices = selectedDevices.isEmpty ? [cursor] : Array(selectedDevices).sorted()
            launch(app, deviceIndices: indices)
        }
    }

    func runOnDefault() {
        guard case .apps = mode, cursor < apps.count else { return }
        launch(apps[cursor], deviceIndices: state.devicesFor(appName: apps[cursor].name))
    }

    /// Launch an app on the given device indices, remembering them for next time.
    private func launch(_ app: AppInfo, deviceIndices: [Int]) {
        let devices = deviceIndices.compactMap { $0 < DeviceInfo.all.count ? DeviceInfo.all[$0] : nil }
        guard !devices.isEmpty else { return }
        state.recordDevices(appName: app.name, indices: deviceIndices)
        onLaunch?(app, devices)
        onDismiss?()
    }

    func typeCharacter(_ char: String) {
        guard case .apps = mode else { return }
        filter += char
        cursor = 0
    }

    func deleteCharacter() {
        guard case .apps = mode, !filter.isEmpty else { return }
        filter.removeLast()
        cursor = 0
    }

    func runAppOnDefault(_ index: Int) {
        guard index < apps.count else { return }
        launch(apps[index], deviceIndices: state.devicesFor(appName: apps[index].name))
    }

    func openDevicePicker() {
        guard case .apps = mode, cursor < apps.count else { return }
        openDevicePickerForIndex(cursor)
    }

    func openDevicePickerForIndex(_ index: Int) {
        guard index < apps.count else { return }
        let app = apps[index]
        // Pre-tick the devices this app last ran on so one Enter re-launches
        // to the same set.
        let remembered = Set(state.devicesFor(appName: app.name))
        filter = ""
        withAnimation(.easeInOut(duration: 0.15)) {
            mode = .devices(app)
            cursor = 0
            selectedDevices = remembered
        }
    }

    func toggleSelection() {
        guard case .devices = mode else { return }
        if selectedDevices.contains(cursor) {
            selectedDevices.remove(cursor)
        } else {
            selectedDevices.insert(cursor)
        }
    }

    func goBack() {
        switch mode {
        case .apps:
            onDismiss?()
        case .devices:
            withAnimation(.easeInOut(duration: 0.15)) {
                mode = .apps
                cursor = 0
                selectedDevices = []
            }
        }
    }

    func reset() {
        mode = .apps
        cursor = 0
        selectedDevices = []
        filter = ""
    }

    /// Called each time the popover opens. Re-reads apps.json instantly (cheap),
    /// then re-scans /Users/moshe/Apps in the background so an app added while
    /// RunApp was running shows up without restarting it.
    func refreshApps() {
        let onDisk = AppInfo.load()
        if !onDisk.isEmpty, onDisk != AppInfo.all {
            AppInfo.all = onDisk
            appsRevision += 1
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let fresh = AppInfo.rescan()
            guard !fresh.isEmpty else { return }
            DispatchQueue.main.async {
                guard fresh != AppInfo.all else { return }
                AppInfo.all = fresh
                self?.appsRevision += 1
            }
        }
    }
}

// MARK: - Main View

struct PopupView: View {
    @ObservedObject var viewModel: PopupViewModel

    var body: some View {
        VStack(spacing: 0) {
            if !viewModel.filter.isEmpty {
                filterBar
            }

            switch viewModel.mode {
            case .apps:
                appList
            case .devices(let app):
                deviceList(app: app)
            }

            Spacer(minLength: 0)
            Divider()
            footerBar
        }
        .frame(width: 280, height: 400)
    }

    // MARK: - Filter Bar

    private var filterBar: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
                .font(.system(size: 11))
            Text(viewModel.filter)
                .font(.system(size: 13))
            Spacer()
            Button(action: { viewModel.filter = ""; viewModel.cursor = 0 }) {
                Image(systemName: "xmark.circle.fill")
                    .foregroundStyle(.secondary)
                    .font(.system(size: 11))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(Color.primary.opacity(0.05))
    }

    // MARK: - App List

    private var appList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 2) {
                    ForEach(Array(viewModel.apps.enumerated()), id: \.element.name) { index, app in
                        AppRow(
                            app: app,
                            isHighlighted: index == viewModel.cursor,
                            isBuilding: viewModel.state.isBuilding(appName: app.name),
                            onRun: {
                                let hasModifier = NSApp.currentEvent?.modifierFlags
                                    .intersection([.command, .option, .control, .shift]).isEmpty == false
                                if hasModifier {
                                    viewModel.openDevicePickerForIndex(index)
                                } else {
                                    viewModel.runAppOnDefault(index)
                                }
                            },
                            onPickDevices: {
                                viewModel.openDevicePickerForIndex(index)
                            }
                        )
                        .id(index)
                    }
                }
                .padding(.vertical, 4)
            }
            .onChange(of: viewModel.cursor) { _, newValue in
                withAnimation { proxy.scrollTo(newValue, anchor: .center) }
            }
        }
    }

    // MARK: - Device List

    private func deviceList(app: AppInfo) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.left")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(app.name)
                    .font(.headline)
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .contentShape(Rectangle())
            .onTapGesture { viewModel.goBack() }

            Divider()

            VStack(spacing: 2) {
                ForEach(Array(DeviceInfo.all.enumerated()), id: \.element.name) { index, device in
                    DeviceRow(
                        device: device,
                        isHighlighted: index == viewModel.cursor,
                        isSelected: viewModel.selectedDevices.contains(index)
                    )
                    .contentShape(Rectangle())
                    .onTapGesture {
                        viewModel.cursor = index
                        viewModel.toggleSelection()
                    }
                }
            }
            .padding(.vertical, 4)
        }
    }

    // MARK: - Footer

    private var footerBar: some View {
        HStack(spacing: 0) {
            Group {
                switch viewModel.mode {
                case .apps:
                    Text("\u{2191}\u{2193}  \u{23CE} run  \u{2192} devices  \u{238B} close")
                case .devices:
                    Text("\u{2191}\u{2193}  \u{2423} toggle  \u{23CE} launch  \u{2190} back")
                }
            }
            .font(.system(size: 10))
            .foregroundStyle(.tertiary)

            Spacer()

            Button(action: { viewModel.onRelaunch?() }) {
                Image(systemName: "arrow.clockwise").font(.system(size: 11))
            }
            .buttonStyle(.plain).foregroundStyle(.secondary)
            .help("Relaunch (\u{2318}R)")

            Spacer().frame(width: 10)

            Button(action: { viewModel.onOpenSettings?() }) {
                Image(systemName: "gear").font(.system(size: 11))
            }
            .buttonStyle(.plain).foregroundStyle(.secondary)
            .help("Settings")

            Spacer().frame(width: 10)

            Button(action: { NSApp.terminate(nil) }) {
                Image(systemName: "power").font(.system(size: 11))
            }
            .buttonStyle(.plain).foregroundStyle(.secondary)
            .help("Quit (\u{2318}Q)")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

// MARK: - App Row

struct AppRow: View {
    let app: AppInfo
    let isHighlighted: Bool
    let isBuilding: Bool
    let onRun: () -> Void
    let onPickDevices: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            HStack(spacing: 8) {
                Text(app.name)
                    .lineLimit(1)
                Spacer()
                if isBuilding {
                    ProgressView().controlSize(.small)
                }
            }
            .contentShape(Rectangle())
            .onTapGesture { onRun() }

            Image(systemName: "chevron.right")
                .font(.system(size: 10, weight: .semibold))
                .foregroundColor(isHighlighted ? .white.opacity(0.4) : .gray.opacity(0.3))
                .frame(width: 28, height: 28)
                .contentShape(Rectangle())
                .onTapGesture { onPickDevices() }
        }
        .padding(.leading, 12)
        .padding(.trailing, 2)
        .padding(.vertical, 4)
        .background(
            RoundedRectangle(cornerRadius: 4)
                .fill(isHighlighted ? Color.accentColor : Color.clear)
        )
        .foregroundStyle(isHighlighted ? .white : .primary)
        .padding(.horizontal, 4)
    }
}

// MARK: - Device Row

struct DeviceRow: View {
    let device: DeviceInfo
    let isHighlighted: Bool
    let isSelected: Bool

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(
                    isSelected
                        ? (isHighlighted ? .white : Color.accentColor)
                        : (isHighlighted ? .white.opacity(0.5) : .secondary)
                )

            Text(device.name).lineLimit(1)

            Spacer()

            Image(systemName: device.type == .simulator ? "desktopcomputer" : "iphone")
                .font(.caption)
                .foregroundColor(isHighlighted ? .white.opacity(0.6) : .gray.opacity(0.4))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 4)
                .fill(isHighlighted ? Color.accentColor : Color.clear)
        )
        .foregroundStyle(isHighlighted ? .white : .primary)
        .padding(.horizontal, 4)
    }
}
