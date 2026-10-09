import AppKit
import Darwin
import Foundation

private let serviceLabel = "com.danieltalebi.security-cam"

private struct ServiceSnapshot {
    let running: Bool
    let pid: String?
}

private struct CameraSnapshot {
    let name: String
    let mode: String
    let state: String
    let color: NSColor
}

private final class SecurityCamPaths {
    private let defaults = UserDefaults.standard

    var projectURL: URL {
        if let saved = defaults.string(forKey: "projectDirectory"), !saved.isEmpty {
            return URL(fileURLWithPath: saved, isDirectory: true)
        }
        if let configured = ProcessInfo.processInfo.environment["SECURITY_CAM_PROJECT_DIR"], !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("code/security-cam", isDirectory: true)
    }

    var logsURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/security-cam", isDirectory: true)
    }

    var monitorLogURL: URL { logsURL.appendingPathComponent("monitor.log") }
    var dashboardStateURL: URL { logsURL.appendingPathComponent("dashboard-state.json") }
    var mainConfigURL: URL { projectURL.appendingPathComponent("multi-camera.json") }

    func setProjectURL(_ url: URL) {
        defaults.set(url.standardizedFileURL.path, forKey: "projectDirectory")
    }
}

private final class ServiceController {
    private var userDomain: String { "gui/\(getuid())" }
    private var serviceTarget: String { "\(userDomain)/\(serviceLabel)" }
    private var servicePlist: String {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(serviceLabel).plist").path
    }

    @discardableResult
    func launchctl(_ arguments: [String]) -> (Int32, String) {
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = output
        do {
            try process.run()
            process.waitUntilExit()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            return (process.terminationStatus, String(decoding: data, as: UTF8.self))
        } catch {
            return (1, error.localizedDescription)
        }
    }

    func snapshot() -> ServiceSnapshot {
        let result = launchctl(["print", serviceTarget])
        let running = result.0 == 0 && result.1.contains("state = running")
        let pid = result.1
            .split(separator: "\n")
            .first(where: { $0.trimmingCharacters(in: .whitespaces).hasPrefix("pid =") })?
            .split(separator: "=").last?
            .trimmingCharacters(in: .whitespaces)
        return ServiceSnapshot(running: running, pid: pid)
    }

    func start() -> (Int32, String) {
        let result = launchctl(["kickstart", serviceTarget])
        return result.0 == 0 ? result : launchctl(["bootstrap", userDomain, servicePlist])
    }

    func restart() -> (Int32, String) {
        snapshot().running ? launchctl(["kickstart", "-k", serviceTarget]) : start()
    }

    func stop() -> (Int32, String) { launchctl(["bootout", serviceTarget]) }
}

private final class StatusCard: NSBox {
    private let valueLabel = NSTextField(labelWithString: "—")
    private let detailLabel = NSTextField(labelWithString: "")
    private let dot = NSView()

    init(title: String) {
        super.init(frame: .zero)
        boxType = .custom
        cornerRadius = 12
        borderWidth = 1
        borderColor = .separatorColor
        fillColor = .controlBackgroundColor

        let titleLabel = NSTextField(labelWithString: title.uppercased())
        titleLabel.font = .systemFont(ofSize: 11, weight: .semibold)
        titleLabel.textColor = .secondaryLabelColor
        dot.wantsLayer = true
        dot.layer?.cornerRadius = 5
        dot.translatesAutoresizingMaskIntoConstraints = false
        valueLabel.font = .systemFont(ofSize: 19, weight: .semibold)
        valueLabel.lineBreakMode = .byTruncatingTail
        detailLabel.font = .systemFont(ofSize: 11)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byTruncatingMiddle

        let valueRow = NSStackView(views: [dot, valueLabel])
        valueRow.orientation = .horizontal
        valueRow.alignment = .centerY
        valueRow.spacing = 8
        let stack = NSStackView(views: [titleLabel, valueRow, detailLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.edgeInsets = NSEdgeInsets(top: 14, left: 16, bottom: 14, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false
        contentView?.addSubview(stack)
        NSLayoutConstraint.activate([
            dot.widthAnchor.constraint(equalToConstant: 10),
            dot.heightAnchor.constraint(equalToConstant: 10),
            stack.leadingAnchor.constraint(equalTo: contentView!.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: contentView!.trailingAnchor),
            stack.topAnchor.constraint(equalTo: contentView!.topAnchor),
            stack.bottomAnchor.constraint(equalTo: contentView!.bottomAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func update(value: String, detail: String, color: NSColor) {
        valueLabel.stringValue = value
        detailLabel.stringValue = detail
        dot.layer?.backgroundColor = color.cgColor
    }
}

private final class DashboardViewController: NSViewController {
    private let paths: SecurityCamPaths
    private let service: ServiceController
    private let serviceCard = StatusCard(title: "Monitor")
    private let alarmCard = StatusCard(title: "Alarma")
    private let presenceCard = StatusCard(title: "Presencia")
    private let healthCard = StatusCard(title: "Salud")
    private let cameraStack = NSStackView()
    private let overviewLogView = NSTextView()
    private let fullLogView = NSTextView()
    private let configEditor = NSTextView()
    private let configPopup = NSPopUpButton()
    private let projectLabel = NSTextField(labelWithString: "")
    private let lastUpdatedLabel = NSTextField(labelWithString: "")
    private var configURLs: [URL] = []
    private var refreshTimer: Timer?
    private var loggedInitialState = false
    var onServiceStatusChanged: ((ServiceSnapshot) -> Void)?

    init(paths: SecurityCamPaths, service: ServiceController) {
        self.paths = paths
        self.service = service
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func loadView() {
        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        view = root

        let title = NSTextField(labelWithString: "Security Cam")
        title.font = .systemFont(ofSize: 28, weight: .bold)
        let subtitle = NSTextField(labelWithString: "Monitoreo local de cámaras y presencia")
        subtitle.font = .systemFont(ofSize: 13)
        subtitle.textColor = .secondaryLabelColor
        let titleStack = NSStackView(views: [title, subtitle])
        titleStack.orientation = .vertical
        titleStack.alignment = .leading
        titleStack.spacing = 2

        let refreshButton = makeButton("Actualizar", action: #selector(refreshNow))
        let restartButton = makeButton("Reiniciar monitor", action: #selector(restartService))
        let headerButtons = NSStackView(views: [refreshButton, restartButton])
        headerButtons.orientation = .horizontal
        headerButtons.spacing = 8
        let header = NSStackView(views: [titleStack, NSView(), headerButtons])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.translatesAutoresizingMaskIntoConstraints = false

        let tabs = NSTabView()
        tabs.translatesAutoresizingMaskIntoConstraints = false
        tabs.addTabViewItem(makeOverviewTab())
        tabs.addTabViewItem(makeConfigurationTab())
        tabs.addTabViewItem(makeLogsTab())

        root.addSubview(header)
        root.addSubview(tabs)
        NSLayoutConstraint.activate([
            header.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 24),
            header.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -24),
            header.topAnchor.constraint(equalTo: root.topAnchor, constant: 20),
            tabs.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 20),
            tabs.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -20),
            tabs.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 16),
            tabs.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -20),
        ])
    }

    override func viewDidAppear() {
        super.viewDidAppear()
        refreshNow()
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(timeInterval: 4, target: self, selector: #selector(refreshNow), userInfo: nil, repeats: true)
    }

    private func makeOverviewTab() -> NSTabViewItem {
        let item = NSTabViewItem(identifier: "overview")
        item.label = "Resumen"
        let container = NSView()
        let cards = NSStackView(views: [serviceCard, alarmCard, presenceCard, healthCard])
        cards.orientation = .horizontal
        cards.distribution = .fillEqually
        cards.spacing = 12
        cards.translatesAutoresizingMaskIntoConstraints = false
        cards.heightAnchor.constraint(equalToConstant: 102).isActive = true

        cameraStack.orientation = .vertical
        cameraStack.alignment = .leading
        cameraStack.spacing = 8
        let cameraBox = NSBox()
        cameraBox.boxType = .custom
        cameraBox.cornerRadius = 10
        cameraBox.borderColor = .separatorColor
        cameraBox.fillColor = .controlBackgroundColor
        cameraStack.translatesAutoresizingMaskIntoConstraints = false
        cameraBox.contentView?.addSubview(cameraStack)
        NSLayoutConstraint.activate([
            cameraStack.leadingAnchor.constraint(equalTo: cameraBox.contentView!.leadingAnchor, constant: 16),
            cameraStack.trailingAnchor.constraint(equalTo: cameraBox.contentView!.trailingAnchor, constant: -16),
            cameraStack.topAnchor.constraint(equalTo: cameraBox.contentView!.topAnchor, constant: 12),
            cameraStack.bottomAnchor.constraint(equalTo: cameraBox.contentView!.bottomAnchor, constant: -12),
        ])

        configureLogView(overviewLogView)
        let activityScroll = scrollView(for: overviewLogView)
        lastUpdatedLabel.font = .systemFont(ofSize: 11)
        lastUpdatedLabel.textColor = .tertiaryLabelColor
        lastUpdatedLabel.alignment = .right
        let stack = NSStackView(views: [cards, sectionTitle("Cámaras"), cameraBox, sectionTitle("Actividad reciente"), activityScroll, lastUpdatedLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -12),
            cards.widthAnchor.constraint(equalTo: stack.widthAnchor),
            cameraBox.widthAnchor.constraint(equalTo: stack.widthAnchor),
            cameraBox.heightAnchor.constraint(greaterThanOrEqualToConstant: 82),
            activityScroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            activityScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180),
            lastUpdatedLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        item.view = container
        return item
    }

    private func makeConfigurationTab() -> NSTabViewItem {
        let item = NSTabViewItem(identifier: "configuration")
        item.label = "Configuración"
        let container = NSView()
        projectLabel.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        projectLabel.textColor = .secondaryLabelColor
        projectLabel.lineBreakMode = .byTruncatingMiddle
        let projectRow = NSStackView(views: [projectLabel, NSView(), makeButton("Elegir carpeta…", action: #selector(chooseProject)), makeButton("Abrir en Finder", action: #selector(openProject))])
        projectRow.orientation = .horizontal
        projectRow.alignment = .centerY
        projectRow.spacing = 8

        configPopup.target = self
        configPopup.action = #selector(selectedConfigChanged)
        let saveButton = makeButton("Guardar y reiniciar", action: #selector(saveAndRestart))
        saveButton.keyEquivalent = "s"
        saveButton.keyEquivalentModifierMask = [.command]
        let configRow = NSStackView(views: [configPopup, makeButton("Recargar", action: #selector(loadSelectedConfig)), NSView(), saveButton])
        configRow.orientation = .horizontal
        configRow.spacing = 8

        configEditor.isRichText = false
        configEditor.isAutomaticQuoteSubstitutionEnabled = false
        configEditor.isAutomaticDashSubstitutionEnabled = false
        configEditor.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        configEditor.textContainerInset = NSSize(width: 10, height: 10)
        let editorScroll = scrollView(for: configEditor)
        let help = NSTextField(wrappingLabelWithString: "Los cambios se validan como JSON antes de guardarse. Se crea una copia .backup y el monitor solo se reinicia si el archivo es válido.")
        help.font = .systemFont(ofSize: 11)
        help.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [sectionTitle("Proyecto"), projectRow, sectionTitle("Archivo"), configRow, editorScroll, help])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -12),
            projectRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            configRow.widthAnchor.constraint(equalTo: stack.widthAnchor),
            editorScroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            editorScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 380),
            help.widthAnchor.constraint(equalTo: stack.widthAnchor),
        ])
        item.view = container
        return item
    }

    private func makeLogsTab() -> NSTabViewItem {
        let item = NSTabViewItem(identifier: "logs")
        item.label = "Logs"
        let container = NSView()
        configureLogView(fullLogView)
        let scroll = scrollView(for: fullLogView)
        let controls = NSStackView(views: [makeButton("Abrir carpeta de logs", action: #selector(openLogs)), makeButton("Abrir Home Assistant", action: #selector(openHomeAssistant)), NSView()])
        controls.orientation = .horizontal
        controls.spacing = 8
        let stack = NSStackView(views: [controls, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -12),
            stack.topAnchor.constraint(equalTo: container.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -12),
            controls.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 480),
        ])
        item.view = container
        return item
    }

    private func makeButton(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        return button
    }

    private func sectionTitle(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 14, weight: .semibold)
        return label
    }

    private func configureLogView(_ textView: NSTextView) {
        textView.isEditable = false
        textView.isRichText = false
        textView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        textView.textContainerInset = NSSize(width: 9, height: 9)
    }

    private func scrollView(for textView: NSTextView) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .bezelBorder
        scroll.documentView = textView
        textView.minSize = .zero
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = true
        textView.autoresizingMask = [.width]
        textView.textContainer?.containerSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = false
        return scroll
    }

    @objc func refreshNow() {
        let snapshot = service.snapshot()
        serviceCard.update(value: snapshot.running ? "Activo" : "Detenido", detail: snapshot.pid.map { "PID \($0)" } ?? "LaunchAgent \(serviceLabel)", color: snapshot.running ? .systemGreen : .systemRed)
        let log = readTail(paths.monitorLogURL, maxBytes: 140_000)
        let lines = log.split(separator: "\n").map(String.init)
        let alarm = latestValue(in: lines, prefix: "Home Assistant alarm mode:")
        let alarmDisplay: (String, NSColor)
        switch alarm {
        case "armed_away": alarmDisplay = ("Armada fuera", .systemRed)
        case "armed_home": alarmDisplay = ("Armada en casa", .systemOrange)
        case "disarmed": alarmDisplay = ("Desarmada", .systemGreen)
        default: alarmDisplay = ("Esperando MQTT", .systemGray)
        }
        alarmCard.update(value: alarmDisplay.0, detail: "Estado recibido por el monitor", color: alarmDisplay.1)

        let presenceLine = lines.last(where: { $0.hasPrefix("Wi-Fi presence:") }) ?? ""
        let presence = presenceLine.components(separatedBy: "Wi-Fi presence:").last?.trimmingCharacters(in: .whitespaces) ?? "desconocida"
        let presenceHome = presence.hasPrefix("home")
        let presenceAway = presence.hasPrefix("away")
        presenceCard.update(value: presenceHome ? "En casa" : presenceAway ? "Fuera" : "Desconocida", detail: presence.isEmpty ? "Sin datos de routers" : presence, color: presenceHome ? .systemGreen : presenceAway ? .systemOrange : .systemGray)

        let cameras = cameraSnapshots(lines: lines)
        updateCameraRows(cameras)
        let connected = cameras.filter { $0.state == "Conectada" }.count
        let allConnected = !cameras.isEmpty && connected == cameras.count
        healthCard.update(value: snapshot.running && allConnected ? "Correcta" : "Revisar", detail: cameras.isEmpty ? "No hay cámaras configuradas" : "\(connected)/\(cameras.count) DVRIP conectadas", color: snapshot.running && allConnected ? .systemGreen : .systemOrange)

        overviewLogView.string = Array(lines.suffix(18)).joined(separator: "\n")
        fullLogView.string = Array(lines.suffix(100)).joined(separator: "\n")
        overviewLogView.scrollToEndOfDocument(nil)
        fullLogView.scrollToEndOfDocument(nil)
        lastUpdatedLabel.stringValue = "Actualizado \(DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium))"
        projectLabel.stringValue = paths.projectURL.path
        reloadConfigListIfNeeded()
        onServiceStatusChanged?(snapshot)
        if !loggedInitialState {
            loggedInitialState = true
            print("Dashboard loaded: project=\(paths.projectURL.path), log_lines=\(lines.count), cameras=\(cameras.count), service_running=\(snapshot.running)")
        }
        let diagnostic: [String: Any] = [
            "project": paths.projectURL.path,
            "monitor_log": paths.monitorLogURL.path,
            "log_lines_read": lines.count,
            "cameras_read": cameras.count,
            "service_running": snapshot.running,
            "alarm": alarm ?? "unknown",
            "presence": presence,
            "updated_at": ISO8601DateFormatter().string(from: Date()),
        ]
        if let data = try? JSONSerialization.data(withJSONObject: diagnostic, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: paths.dashboardStateURL, options: .atomic)
        }
    }

    private func cameraSnapshots(lines: [String]) -> [CameraSnapshot] {
        guard let data = try? Data(contentsOf: paths.mainConfigURL), let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let entries = root["cameras"] as? [[String: Any]] else { return [] }
        return entries.compactMap { entry in
            guard let name = entry["name"] as? String else { return nil }
            let mode = entry["mode"] as? String ?? "camera"
            let last = lines.reversed().first(where: { $0.hasPrefix("\(name): DVRIP") })?.lowercased() ?? ""
            if last.contains("disconnected") || last.contains("failed") || last.contains("retry") {
                return CameraSnapshot(name: name, mode: mode, state: "Desconectada", color: .systemRed)
            }
            if last.contains("connected") { return CameraSnapshot(name: name, mode: mode, state: "Conectada", color: .systemGreen) }
            return CameraSnapshot(name: name, mode: mode, state: "Sin datos", color: .systemGray)
        }
    }

    private func updateCameraRows(_ cameras: [CameraSnapshot]) {
        cameraStack.arrangedSubviews.forEach { cameraStack.removeArrangedSubview($0); $0.removeFromSuperview() }
        if cameras.isEmpty {
            let label = NSTextField(labelWithString: "No se pudo leer multi-camera.json")
            label.textColor = .secondaryLabelColor
            cameraStack.addArrangedSubview(label)
            return
        }
        for camera in cameras {
            let dot = NSView()
            dot.wantsLayer = true
            dot.layer?.cornerRadius = 4
            dot.layer?.backgroundColor = camera.color.cgColor
            dot.translatesAutoresizingMaskIntoConstraints = false
            dot.widthAnchor.constraint(equalToConstant: 8).isActive = true
            dot.heightAnchor.constraint(equalToConstant: 8).isActive = true
            let name = NSTextField(labelWithString: camera.name.capitalized)
            name.font = .systemFont(ofSize: 13, weight: .medium)
            let mode = NSTextField(labelWithString: camera.mode)
            mode.textColor = .secondaryLabelColor
            let state = NSTextField(labelWithString: camera.state)
            state.textColor = camera.color
            let row = NSStackView(views: [dot, name, mode, NSView(), state])
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = 9
            cameraStack.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: cameraStack.widthAnchor).isActive = true
        }
    }

    private func latestValue(in lines: [String], prefix: String) -> String? {
        lines.reversed().first(where: { $0.hasPrefix(prefix) }).map { String($0.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces) }
    }

    private func readTail(_ url: URL, maxBytes: UInt64) -> String {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return "" }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size > maxBytes ? size - maxBytes : 0)
        return String(decoding: (try? handle.readToEnd()) ?? Data(), as: UTF8.self)
    }

    private func reloadConfigListIfNeeded(force: Bool = false) {
        let urls = discoverConfigURLs()
        if !force && urls == configURLs { return }
        let selectedPath = configURLs.indices.contains(configPopup.indexOfSelectedItem) ? configURLs[configPopup.indexOfSelectedItem].path : nil
        configURLs = urls
        configPopup.removeAllItems()
        configPopup.addItems(withTitles: urls.map { $0.lastPathComponent })
        if let selectedPath, let index = urls.firstIndex(where: { $0.path == selectedPath }) { configPopup.selectItem(at: index) }
        else if !urls.isEmpty { configPopup.selectItem(at: 0) }
        loadSelectedConfig()
    }

    private func discoverConfigURLs() -> [URL] {
        var urls: [URL] = []
        if FileManager.default.fileExists(atPath: paths.mainConfigURL.path) { urls.append(paths.mainConfigURL) }
        guard let data = try? Data(contentsOf: paths.mainConfigURL), let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let cameras = root["cameras"] as? [[String: Any]] else { return urls }
        for camera in cameras {
            guard let relative = camera["config"] as? String else { continue }
            let url = paths.projectURL.appendingPathComponent(relative).standardizedFileURL
            if FileManager.default.fileExists(atPath: url.path), !urls.contains(url) { urls.append(url) }
        }
        return urls
    }

    @objc private func selectedConfigChanged() { loadSelectedConfig() }

    @objc private func loadSelectedConfig() {
        guard configURLs.indices.contains(configPopup.indexOfSelectedItem) else { configEditor.string = ""; return }
        configEditor.string = (try? String(contentsOf: configURLs[configPopup.indexOfSelectedItem], encoding: .utf8)) ?? ""
    }

    @objc private func saveAndRestart() {
        guard configURLs.indices.contains(configPopup.indexOfSelectedItem) else { return }
        let url = configURLs[configPopup.indexOfSelectedItem]
        guard let data = configEditor.string.data(using: .utf8) else { return }
        do {
            _ = try JSONSerialization.jsonObject(with: data)
            let backupURL = url.appendingPathExtension("backup")
            if FileManager.default.fileExists(atPath: backupURL.path) { try FileManager.default.removeItem(at: backupURL) }
            if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.copyItem(at: url, to: backupURL) }
            try data.write(to: url, options: .atomic)
            let result = service.restart()
            if result.0 != 0 { throw NSError(domain: "SecurityCam", code: Int(result.0), userInfo: [NSLocalizedDescriptionKey: result.1]) }
            showAlert(title: "Configuración guardada", message: "El JSON es válido y el monitor se reinició.")
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.refreshNow() }
        } catch {
            showAlert(title: "No se guardó la configuración", message: error.localizedDescription, style: .critical)
        }
    }

    @objc private func chooseProject() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = paths.projectURL
        panel.prompt = "Seleccionar"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard FileManager.default.fileExists(atPath: url.appendingPathComponent("multi_camera_monitor.py").path) else {
            showAlert(title: "Carpeta incorrecta", message: "La carpeta debe contener multi_camera_monitor.py.", style: .warning)
            return
        }
        paths.setProjectURL(url)
        reloadConfigListIfNeeded(force: true)
        refreshNow()
    }

    @objc private func restartService() {
        let result = service.restart()
        if result.0 != 0 { showAlert(title: "No se pudo reiniciar", message: result.1, style: .critical) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.refreshNow() }
    }

    @objc private func openProject() { NSWorkspace.shared.open(paths.projectURL) }
    @objc private func openLogs() { NSWorkspace.shared.open(paths.logsURL) }
    @objc private func openHomeAssistant() { if let url = URL(string: "http://127.0.0.1:8123") { NSWorkspace.shared.open(url) } }

    private func showAlert(title: String, message: String, style: NSAlert.Style = .informational) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = style
        alert.addButton(withTitle: "OK")
        if let window = view.window { alert.beginSheetModal(for: window) } else { alert.runModal() }
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private let paths = SecurityCamPaths()
    private let service = ServiceController()
    private var statusItem: NSStatusItem!
    private let stateItem = NSMenuItem(title: "Comprobando…", action: nil, keyEquivalent: "")
    private lazy var serviceControlItem = menuItem("Reiniciar monitor", #selector(restartService))
    private var window: NSWindow?
    private var dashboard: DashboardViewController?
    private var timer: Timer?
    private let backgroundLaunch = CommandLine.arguments.contains("--background")
    private var started = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        start()
    }

    func start() {
        guard !started else { return }
        started = true
        NSApp.setActivationPolicy(backgroundLaunch ? .accessory : .regular)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        configureStatusItem()
        createDashboard()
        refreshMenuStatus()
        timer = Timer.scheduledTimer(timeInterval: 4, target: self, selector: #selector(refreshMenuStatus), userInfo: nil, repeats: true)
        if !backgroundLaunch { showDashboard() }
    }

    private func configureStatusItem() {
        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "video.fill", accessibilityDescription: "Security Cam")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Security Cam"
        }
        let menu = NSMenu()
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())
        menu.addItem(menuItem("Mostrar dashboard", #selector(showDashboard)))
        menu.addItem(serviceControlItem)
        menu.addItem(menuItem("Detener monitor", #selector(stopService)))
        menu.addItem(.separator())
        menu.addItem(menuItem("Abrir logs", #selector(openLogs)))
        menu.addItem(.separator())
        menu.addItem(menuItem("Salir de Security Cam", #selector(quitApp), key: "q"))
        statusItem.menu = menu
    }

    private func menuItem(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    private func createDashboard() {
        let dashboard = DashboardViewController(paths: paths, service: service)
        dashboard.onServiceStatusChanged = { [weak self] snapshot in self?.applyMenuSnapshot(snapshot) }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1040, height: 720), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Security Cam"
        window.minSize = NSSize(width: 880, height: 620)
        window.center()
        window.contentViewController = dashboard
        window.isReleasedWhenClosed = false
        self.dashboard = dashboard
        self.window = window
        dashboard.refreshNow()
    }

    @objc private func showDashboard() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        dashboard?.refreshNow()
    }

    @objc private func refreshMenuStatus() { applyMenuSnapshot(service.snapshot()) }

    private func applyMenuSnapshot(_ snapshot: ServiceSnapshot) {
        stateItem.title = snapshot.running ? "Monitor activo\(snapshot.pid.map { " · PID \($0)" } ?? "")" : "Monitor detenido"
        serviceControlItem.title = snapshot.running ? "Reiniciar monitor" : "Iniciar monitor"
        statusItem.button?.toolTip = "Security Cam · \(snapshot.running ? "Activo" : "Detenido")"
    }

    @objc private func restartService() {
        _ = service.restart()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.refreshMenuStatus(); self?.dashboard?.refreshNow() }
    }

    @objc private func stopService() {
        _ = service.stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.refreshMenuStatus() }
    }

    @objc private func openLogs() { NSWorkspace.shared.open(paths.logsURL) }
    @objc private func quitApp() { NSApp.terminate(nil) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showDashboard()
        return true
    }
}

@main
enum SecurityCamMain {
    private static var retainedDelegate: AppDelegate?

    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        retainedDelegate = delegate
        app.delegate = delegate
        app.finishLaunching()
        delegate.start()
        app.run()
    }
}
