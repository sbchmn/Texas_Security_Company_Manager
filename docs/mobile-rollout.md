# TSCM mobile rollout

## Release identity and current deliverables

| Setting | Value |
|---|---|
| Store listing | TSCM - Texas Liberty Coalition |
| Home-screen label | TSCM |
| Production origin | `https://tscm.texaslibertycoalition.com` |
| Android application / iOS bundle ID | `com.texaslibertycoalition.tscm` |
| Web manifest ID / scope | `/` |
| Icons | Existing TSCM shield; opaque PNG and maskable variants |

Three distribution paths are prepared:

1. **Website PWA:** supported-browser installation, no store account required.
2. **Android:** a Gradle Trusted Web Activity (TWA) project using Google Android Browser
   Helper, with HTTPS App Links and environment-only upload signing. It opens the live
   production PWA in a compatible browser, not a second copy of Django.
3. **iOS:** an XcodeGen source project using SwiftUI and persistent, app-bound WKWebView
   storage. It adds native connectivity/error reporting, browser navigation, and a
   download/share sheet. It is preparation, not an approved App Store product.

No signing identities, Apple team ID, store accounts or release certificates are bundled.
No APK/AAB/IPA is claimed to be distributable until the checks below pass. Android Gradle
configuration was validated on Windows; a binary build needs the Android SDK. The iOS
project must be generated, compiled and tested on macOS/Xcode.

The Android sources use Gradle 8.13, Android Gradle Plugin 8.13.2, API 36, and Android Browser
Helper 2.7.4. The wrapper verifies the distribution SHA-256. No Node dependency tree is
required. Bubblewrap was evaluated but is not a project dependency: its current upstream
transitive archive/image/auth dependencies had unresolved npm advisories during preparation.

## 1. Deploy the website first

Set the following non-secret deployment variables alongside the existing HTTPS/host settings:

```dotenv
PUBLIC_BASE_URL=https://tscm.texaslibertycoalition.com
PWA_NAME=TSCM - Texas Liberty Coalition
PWA_SHORT_NAME=TSCM
ANDROID_PACKAGE_ID=com.texaslibertycoalition.tscm
ANDROID_SHA256_FINGERPRINTS=
```

The stable manifest name is deployment-wide, not whichever membership happens to be first.
Company branding inside signed-in pages remains tenant-specific. Installations on different
hostnames have separate cookies, browser storage, workers and identity; a custom-domain
installation is not the same app storage as the store package's production origin.

Use the normal image build/migration/deployment sequence. Do not destroy volumes. Verify:

```powershell
curl.exe -f https://tscm.texaslibertycoalition.com/manifest.webmanifest
curl.exe -f https://tscm.texaslibertycoalition.com/service-worker.js
curl.exe -f https://tscm.texaslibertycoalition.com/.well-known/assetlinks.json
```

These must return directly over trusted HTTPS, not login/MFA redirects. An empty asset-links
array is correct before public signing fingerprints are configured. Icons in the manifest
must return PNGs at the advertised sizes. The worker must have JavaScript content type and
`Cache-Control: no-cache`. Static URLs are resolved through collected/hashed assets, so
production offline caches follow the deployed build.

The new anonymous GET/HEAD asset-link endpoint is exempt from MFA background-resource
redirects. It carries only explicitly configured public certificate fingerprints.
Unknown-host checks, CSRF, MFA, tenant permissions and punch-validation gates remain intact.

### Device installation

Point employees to `/install/` or **My account -> Install / mobile help**.

- **Android Chrome:** open the production site and use Install app / Add to Home screen.
  The install-help page exposes the browser install prompt when available.
- **iPhone/iPad Safari:** Share -> Add to Home Screen -> Add. Leave Open as Web App on if
  the system offers it. An in-app browser may need Open in Safari first.
- Sign in and complete MFA in the installed app. Do not assume browser/standalone/native
  sessions share cookies.

Native packages are optional; home-screen installation is the lowest-friction first rollout.

### Phone and narrow-tablet layouts

Ordinary tables become labeled vertical rows at widths up to 1050px; every value and
action remains available without sideways scrolling. This is a JavaScript enhancement;
without JavaScript, the original table remains usable in its scroll container. Desktop
tables keep their normal column layout. Long single-choice selections use a constrained
field with the normal native option picker, not a replacement dropdown widget.

The seven-day scheduling grid intentionally scrolls horizontally. Its phone/tablet hint
offers **Day list** on the same schedule page for a vertically readable alternative.
PDF/image preview surfaces remain document viewers rather than reformatted business pages.

## 2. Offline and update contract

Only a **personnel-linked personal-device clock** is cached as a business document, after an
online visit. Login, MFA, records, payroll and schedule pages are not cached for offline use.
When offline, the installed app's root launch falls back to that saved clock. It explicitly
marks the page stale. Without a prepared clock, it says it is unavailable rather than
pretending the workspace is live. Shared PIN kiosks do not acquire an offline clock.

The encrypted queue, signed device credential, geofence validation and **12-hour sync
deadline** are unchanged. The user must reconnect and open Time clock, leaving it open
until synchronization completes. iOS/background suspension is not a reliable sync mechanism.
There is no push-notification subscription or background location service in this release.
SMS/email notices remain their existing, consent-controlled channels.

Sign-out and company-switch requests clear the saved clock/theme cache. They **do not erase
unsynchronized punches**. Sync before changing account/company, signing out, uninstalling,
clearing browser/app storage or replacing a phone. Offline data is not remote-wipeable while
a device is disconnected; use a passcode/MDM and do not offer personal-device offline capture
on shared devices. The saved page may contain the last-loaded schedule/patrol information.

Workers cache only their static shell and clock document. Updates delete only TSCM-owned
cache names, not other applications' caches. There is no forced mid-form reload or
`skipWaiting`: finish work and synchronize, close all TSCM tabs/app windows, then reopen.
Opening Time clock online refreshes the saved document. A first installation may need a
second online clock visit after the worker has taken control.

## 3. Android build and signing

Install Android Studio, JDK 17+, Android SDK platform 36 and its build tools; review/accept
SDK licenses yourself. The Gradle wrapper is checked in, so global Gradle is unnecessary.
Set `ANDROID_HOME` to the SDK or let Android Studio create ignored `local.properties`.

From the repository root on Windows:

```powershell
.\mobile\android\gradlew.bat -p .\mobile\android :app:assembleDebug
```

For release, keep the upload keystore outside the repository and set secrets in your local
environment or protected CI secret store (never copy real values into these docs):

- `TSCM_UPLOAD_KEYSTORE`: absolute path to the upload keystore
- `TSCM_UPLOAD_STORE_PASSWORD`
- `TSCM_UPLOAD_KEY_ALIAS`
- `TSCM_UPLOAD_KEY_PASSWORD`

```powershell
.\mobile\android\gradlew.bat -p .\mobile\android :app:bundleRelease
```

Output is `mobile\android\app\build\outputs\bundle\release\app-release.aab`.
Missing/partial signing configuration fails visibly. `-PallowUnsignedRelease=true` is an
explicit **build-validation-only** escape hatch; its output must not be uploaded/distributed.
Increase `versionCode` on every Play upload; keep version names aligned with the intended
mobile release. The web app and native packages have separate release/update lifecycles.

### Domain trust and Play App Signing

Enable Play App Signing. Get the **app signing certificate SHA-256** from Play Console,
not just the upload-key fingerprint. Set its colon-delimited public fingerprint in
`ANDROID_SHA256_FINGERPRINTS` and redeploy the web service. Multiple current/test/rotation
fingerprints may be comma-separated; add only certificates you explicitly trust.

The server serves `/.well-known/assetlinks.json` without authentication. The Android project
also declares the reverse association to this exact HTTPS origin. Test using Play's internal
testing distribution: Play-signed installations can use a different key from local APKs.
Without matching origin/certificate trust, the browser can fall back to a visible Custom
Tab rather than fullscreen TWA. Never disable verification to hide that failure.

Verify the current Play target-API requirement at submission; API 36 is the preparation
baseline, not a promise about future store requirements. Complete Play Data safety, privacy
policy, app-access credentials and account-deletion declarations based on actual TSCM use.

## 4. iOS generation, signing and TestFlight

On a Mac with a current Xcode capable of the App Store's accepted SDK, install
[XcodeGen](https://github.com/yonaskolb/XcodeGen) using its documented package-manager route.
Then:

```sh
cd mobile/ios
xcodegen generate --spec project.yml
open TSCM.xcodeproj
```

Select your Apple Developer team in Signing & Capabilities. Confirm the bundle ID, icon,
version/build number and iPhone/iPad targets. Build on a physical device, then Product ->
Archive -> Distribute App -> App Store Connect/TestFlight. Signing profiles, `.xcodeproj`
output, archives and provisioning files are ignored; keep signing assets private.

`project.yml` generates Info.plist with explicit location/camera purpose strings.
The native media delegate refuses microphone capture; clock selfies do not require audio.
There are no arbitrary-HTTP transport exceptions. WKAppBoundDomains and navigation checks
keep the native webview on `tscm.texaslibertycoalition.com`; other trusted user-selected HTTPS,
mail and phone links open externally. The package uses persistent WebKit storage, not
cookie/token copying from Safari. Server-side auth and permissions remain authoritative.

### iOS-specific release gates

- **Not yet compiled/device-certified:** Windows cannot run Xcode or produce a signed IPA.
  Verify every native API, signing setting, icon, permission string and iPad share sheet on
  the actual supported iOS versions before TestFlight expansion.
- **External SSO is not bridged:** Google/Microsoft login in an external browser does not
  sign the native webview in. Use company-issued password + MFA for native pilot testing;
  use the Safari-installed PWA for external SSO. Do not advertise native SSO support or
  bypass provider rules by embedding OAuth pages. A compliant authentication-session
  integration is a separate gate if native SSO is required.
- **External object-storage downloads:** the native download path is restricted to the
  production host. Signed downloads redirected to other storage hosts must be tested/opened
  in Safari; do not silently add arbitrary hosts to the allowlist.
- **Apple 4.2 Minimum Functionality:** a WKWebView wrapper may be rejected as a repackaged
  website despite native navigation/export affordances. Apple decides; native packaging is
  not a guarantee of acceptance. Demonstrate the actual employee time/schedule workflow and
  assess Custom App distribution via Apple Business Manager for an organization-only app.
- **Apple 4.8 Login Services:** verify applicable Sign in with Apple requirements/exceptions
  if third-party primary sign-in is exposed; employee enterprise-account scenarios may have
  different applicability. This preparation does not implement Sign in with Apple.
- **Account creation/deletion:** TSCM exposes registration/invitation workflows and maintains
  legally relevant personnel/audit evidence. Before store submission, provide compliant
  in-app deletion-request handling and required public deletion/support links, with documented
  retention exceptions. Do not equate uninstalling, deactivating a membership or deleting a
  browser cache with deleting an account. This preparation does not add that workflow.

## 5. Store listing and reviewer material

Suggested short description:

> Workforce scheduling, secure time capture and employee workflows for Texas Liberty Coalition.

Use synthetic demonstration personnel, never screenshots of real SSNs, banking data, medical
records, employee locations or payroll. Supply screenshots in current store-required sizes,
the 512px Android store icon and opaque 1024px Apple app icon. Icons can be regenerated with
`scripts/generate_app_icons.py` using the project's existing Pillow dependency.

Before submission, the publisher must approve:

- A publicly accessible privacy policy and support contact/URL. No fake public policy URL is
  supplied by these package sources.
- Store disclosure of identities/contact details, precise/coarse location at punch capture,
  uploaded/selfie photos/documents, personnel/employment data and diagnostic/service records
  actually processed by the deployed workflows. No advertising/tracking SDK was added.
- Retention, access, consent, account-deletion requests and legally required exceptions.
  Native privacy manifests and store labels must describe the full service, not merely claim
  that a webview collects nothing.
- A working reviewer account with a safe demonstration tenant, clear password/MFA enrollment
  instructions and all app-access steps. Never include reviewer credentials in source.
- Appropriate distribution: public store versus managed/private organization distribution.
  Company-only availability must be explained honestly to reviewers.

## 6. Release acceptance checklist

Keep signed store release closed until each applicable gate is verified:

- [ ] Production manifest, icons, worker and asset-link URL are reachable over trusted HTTPS.
- [ ] Fresh installation on Android Chrome and iPhone/iPad Safari uses the right name/icon.
- [ ] Login, MFA activation/re-authentication, logout and company selection work without loops.
- [ ] Officer, scoped supervisor, HR and payroll views retain their intended permissions.
- [ ] Online clock in/out, breaks and checkpoints; allow/deny location and camera.
- [ ] Prepare clock online, close the app, enable airplane mode, reopen from the app icon.
- [ ] Record only synthetic test punches offline; reconnect within 12 hours and confirm exactly
  one server event per queued event. Test retries, stale posts and clock/timezone boundaries.
- [ ] Unprepared device gets an honest offline error. No offline payroll/record exposure.
- [ ] Sign-out/company switch removes the saved clock; queued punches are synced first.
- [ ] Upgrade with multiple tabs, an unsaved form and a queued punch; no forced reload/loss.
- [ ] Background/foreground, device restart, low storage and user-cleared app data behavior.
- [ ] Android Play-signed internal-test build validates Digital Asset Links/fullscreen launch.
- [ ] Android SSO, geolocation/camera, uploads and exports in the selected TWA browser.
- [ ] iOS Xcode build, native password/MFA, document/file upload/download/share and iPad layout.
- [ ] iOS SSO/storage limitations resolved or deliberately excluded before claiming support.
- [ ] Privacy/support/deletion workflows, store disclosures and reviewer credentials complete.
- [ ] Play internal testing/TestFlight pilot accepted before general employee rollout.

Automated web checks:

```powershell
.\.venv\Scripts\python.exe manage.py test core.test_pwa core.tests.OfflineClockLaunchTest core.tests.MfaEnrollmentResourcesTest
```

The client lifecycle checks can be run with Node 22:
`node --test scripts/verification/pwa-client.test.cjs`.

Responsive-table mapping tests:
`node --test scripts/verification/mobile-layout.test.cjs`.
The browser regression suite, `scripts/verification/mobile-layout-browser.test.cjs`,
requires Playwright 1.56.1 and its Chromium/WebKit engines (the official
`mcr.microsoft.com/playwright:v1.56.1-noble` image provides the browser prerequisites).
It checks long values and decision controls at 320/390/760/820/1050px, desktop table
restoration at 1366px, and search filtering without counting decorative cell labels.
Playwright is isolated verification tooling, not an application runtime dependency.

`scripts/verification/pwa-worker.test.cjs` additionally executes the rendered worker with Node's
built-in test runner. Set `TSCM_WORKER_FILE` to a locally downloaded worker response and run
`node --test scripts/verification/pwa-worker.test.cjs`. This simulates cache/lifecycle/network
behavior; it is not a replacement for physical-device capture or native build certification.

Local Docker validation also exercised an isolated Chromium browser: worker activation,
hashed shell caching, an unprepared offline launch, a prepared synthetic clock launch with
the stale marker, offline payroll refusal, and company-switch document clearing. The
synthetic clock did not create personnel records or punches. The local development
certificate was bypassed only in that test; trusted production HTTPS and real-device
permissions/synchronization still require the acceptance checks above. The integrated VS
Code browser left registration pending, so use ordinary Chrome/Safari for installation
testing rather than relying on that embedded browser.

## Official references

- [Android Browser Helper](https://github.com/GoogleChrome/android-browser-helper)
- [Trusted Web Activities](https://developer.chrome.com/docs/android/trusted-web-activity)
- [Digital Asset Links](https://developers.google.com/digital-asset-links/v1/getting-started)
- [Play App Signing](https://support.google.com/googleplay/android-developer/answer/9842756)
- [Apple home-screen web apps](https://support.apple.com/guide/iphone/turn-a-website-into-an-app-iph42ab2f3a7/ios)
- [Apple App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)
- [Xcode distribution](https://developer.apple.com/documentation/xcode/distributing-your-app-for-beta-testing-and-releases)
