import AppKit
import Darwin
import Foundation

private let serviceLabel = "com.danieltalebi.security-cam"

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let stateItem = NSMenuItem(title: "Checking…", action: nil, keyEquivalent: "")
    private let startItem = NSMenuItem(title: "Start", action: #selector(startService), keyEquivalent: "")
    private let restartItem = NSMenuItem(title: "Restart", action: #selector(restartService), keyEquivalent: "")
    private let stopItem = NSMenuItem(title: "Stop", action: #selector(stopService), keyEquivalent: "")
    private var refreshTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "video.fill", accessibilityDescription: "Security camera monitor")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Security Camera Monitor"
        }

        startItem.target = self
        restartItem.target = self
        stopItem.target = self

        let logsItem = NSMenuItem(title: "Open Logs", action: #selector(openLogs), keyEquivalent: "")
        logsItem.target = self
        let quitItem = NSMenuItem(title: "Quit Menu Bar App", action: #selector(quitMenuApp), keyEquivalent: "q")
        quitItem.target = self

        let menu = NSMenu()
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())
        menu.addItem(startItem)
        menu.addItem(restartItem)
        menu.addItem(stopItem)
        menu.addItem(.separator())
        menu.addItem(logsItem)
        menu.addItem(.separator())
        menu.addItem(quitItem)
        statusItem.menu = menu

        refreshStatus()
        refreshTimer = Timer.scheduledTimer(
            timeInterval: 3,
            target: self,
            selector: #selector(refreshStatus),
            userInfo: nil,
            repeats: true
        )
    }

    private var serviceTarget: String {
        "gui/\(getuid())/\(serviceLabel)"
    }

    @discardableResult
    private func launchctl(_ arguments: [String]) -> (Int32, String) {
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

    @objc private func refreshStatus() {
        let result = launchctl(["print", serviceTarget])
        let running = result.0 == 0 && result.1.contains("state = running")
        let pid = result.1
            .split(separator: "\n")
            .first(where: { $0.trimmingCharacters(in: .whitespaces).hasPrefix("pid =") })?
            .split(separator: "=").last?
            .trimmingCharacters(in: .whitespaces)

        stateItem.title = running ? "Running\(pid.map { " · PID \($0)" } ?? "")" : "Stopped"
        startItem.isEnabled = !running
        stopItem.isEnabled = running
        restartItem.isEnabled = true
        statusItem.button?.toolTip = "Security Camera Monitor · \(running ? "Running" : "Stopped")"
    }

    private func refreshSoon() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.refreshStatus()
        }
    }

    @objc private func startService() {
        _ = launchctl(["kickstart", serviceTarget])
        refreshSoon()
    }

    @objc private func restartService() {
        _ = launchctl(["kickstart", "-k", serviceTarget])
        refreshSoon()
    }

    @objc private func stopService() {
        _ = launchctl(["kill", "SIGTERM", serviceTarget])
        refreshSoon()
    }

    @objc private func openLogs() {
        let path = NSString(string: "~/Library/Logs/security-cam").expandingTildeInPath
        NSWorkspace.shared.open(URL(fileURLWithPath: path, isDirectory: true))
    }

    @objc private func quitMenuApp() {
        NSApp.terminate(nil)
    }
}

@main
enum SecurityCamMenuMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.run()
    }
}
