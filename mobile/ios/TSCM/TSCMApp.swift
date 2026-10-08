import SwiftUI
import WebKit
import Network
import UIKit

enum AppConfiguration {
    static let home = URL(string: "https://tscm.texaslibertycoalition.com/?source=ios")!

    static func permits(_ url: URL) -> Bool {
        url.scheme == "https" && url.host == home.host && url.port == home.port &&
            url.user == nil && url.password == nil
    }
}

@main
struct TSCMApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}

final class Browser: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    @Published var failure: String?
    @Published var connected = true
    @Published var shareFile: URL?
    let webView: WKWebView
    private let monitor = NWPathMonitor()
    private var files: [ObjectIdentifier: URL] = [:]

    override init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.limitsNavigationsToAppBoundDomains = true
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        monitor.pathUpdateHandler = { [weak self] path in
            DispatchQueue.main.async { self?.connected = path.status == .satisfied }
        }
        monitor.start(queue: DispatchQueue(label: "TSCM.connectivity"))
        webView.load(URLRequest(url: AppConfiguration.home))
    }

    deinit { monitor.cancel() }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            failure = "This link has no valid address."
            decisionHandler(.cancel)
            return
        }
        if AppConfiguration.permits(url) {
            if navigationAction.shouldPerformDownload {
                decisionHandler(.download)
            } else if navigationAction.targetFrame == nil {
                webView.load(navigationAction.request)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.allow)
            }
        } else {
            decisionHandler(.cancel)
            if ["https", "mailto", "tel"].contains(url.scheme ?? "") {
                if url.path.contains("/oauth") || url.host?.contains("accounts.google.") == true ||
                    url.host == "login.microsoftonline.com" {
                    failure = "External sign-in opens in your browser and does not share this app's session. Use a company-issued password and MFA here, or use the installed Safari web app for SSO."
                }
                UIApplication.shared.open(url, options: [:]) { [weak self] opened in
                    if !opened {
                        DispatchQueue.main.async { self?.failure = "No application could open this external link." }
                    }
                }
            } else {
                failure = "This link uses an unsupported address. Only this company's HTTPS site opens inside TSCM."
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let url = navigationResponse.response.url, AppConfiguration.permits(url) else {
            failure = "The response left the approved company host. Open it in Safari if you trust the destination."
            decisionHandler(.cancel)
            return
        }
        let disposition = (navigationResponse.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(!navigationResponse.canShowMIMEType || disposition.lowercased().contains("attachment") ? .download : .allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { failure = nil }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { report(error) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { report(error) }

    private func report(_ error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { failure = error.localizedDescription }
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(type == .camera && origin.protocol == "https" && origin.host == AppConfiguration.home.host &&
                        (origin.port == 443 || origin.port == 0) ? .prompt : .deny)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = UIAlertController(title: "TSCM", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "Continue", style: .default) { _ in completionHandler(true) })
        guard let controller = webView.window?.rootViewController else {
            failure = "The confirmation dialog could not open. The action was cancelled."
            completionHandler(false)
            return
        }
        controller.present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        guard let url = response.url, AppConfiguration.permits(url) else {
            failure = "Downloads from external storage are not supported inside this package. Use Safari."
            completionHandler(nil)
            return
        }
        do {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let destination = directory.appendingPathComponent(URL(fileURLWithPath: suggestedFilename).lastPathComponent)
            files[ObjectIdentifier(download)] = destination
            completionHandler(destination)
        } catch {
            failure = "Could not prepare the download: \(error.localizedDescription)"
            completionHandler(nil)
        }
    }
    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse,
                  newRequest request: URLRequest, decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        let allowed = request.url.map(AppConfiguration.permits) ?? false
        if !allowed { failure = "Download redirected outside the approved host. Use Safari for this document." }
        decisionHandler(allowed ? .allow : .cancel)
    }
    func downloadDidFinish(_ download: WKDownload) {
        shareFile = files.removeValue(forKey: ObjectIdentifier(download))
    }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        failure = "Download failed: \(error.localizedDescription)"
        if let file = files.removeValue(forKey: ObjectIdentifier(download)) { removeTemporary(file) }
    }
    func clearShare() {
        if let file = shareFile { removeTemporary(file) }
        shareFile = nil
    }
    private func removeTemporary(_ file: URL) {
        do { try FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
        catch { failure = "Could not remove the temporary download: \(error.localizedDescription)" }
    }
}

struct WebContent: UIViewRepresentable {
    @ObservedObject var browser: Browser
    func makeUIView(context: Context) -> WKWebView { browser.webView }
    func updateUIView(_ view: WKWebView, context: Context) {}
}

struct FileShare: UIViewControllerRepresentable {
    let file: URL
    let done: () -> Void
    func makeUIViewController(context: Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems: [file], applicationActivities: nil)
        controller.completionWithItemsHandler = { _, _, _, _ in DispatchQueue.main.async(execute: done) }
        return controller
    }
    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}

struct ContentView: View {
    @StateObject private var browser = Browser()
    var body: some View {
        VStack(spacing: 0) {
            if !browser.connected {
                Text("Offline: only a previously prepared personal-device clock can capture punches.")
                    .font(.caption).padding(8).frame(maxWidth: .infinity).background(Color.yellow.opacity(0.2))
            }
            if let failure = browser.failure {
                Text(failure).font(.caption).padding(8).foregroundStyle(.red).accessibilityAddTraits(.isStaticText)
            }
            WebContent(browser: browser)
            HStack {
                Button("Back") { browser.webView.goBack() }
                Spacer()
                Button("Reload") { browser.webView.reload() }
                Spacer()
                Button("Open in Safari") { UIApplication.shared.open(browser.webView.url ?? AppConfiguration.home) }
            }.padding().background(Color(.secondarySystemBackground))
        }
        .sheet(isPresented: Binding(get: { browser.shareFile != nil }, set: { if !$0 { browser.clearShare() } })) {
            if let file = browser.shareFile { FileShare(file: file, done: browser.clearShare) }
        }
    }
}
