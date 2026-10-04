# Stage 2 — Frontend observe

Two frontend classes, different tools:
- **React Native handheld apps** — no web/Expo render path. **Emulator-first** (this doc's
  primary path).
- **Web apps** — observe via the Playwright MCP (secondary, below).

Which repo is which, this machine's SDK, AVDs, node and toolchain quirks, each app's render
paths and auth: `~/.claude/local/qa-e2e/*.md`. Read them first.

Always confirm the change on the **positive case AND its absence on the negative case**, capture
a screenshot of each, and read the runtime logs. Record the **verification level reached**.

## A. React Native — emulator-first (primary)

### 1. Android env bootstrap
If the SDK is installed but not wired into the shell:
```
export ANDROID_HOME=<sdk dir>          # homebrew: /opt/homebrew/share/android-commandlinetools
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$ANDROID_HOME/emulator:$ANDROID_HOME/platform-tools:$PATH"
which emulator adb    # both should now resolve
```

Driver — install Maestro (nice text/testID matching + `takeScreenshot`), or fall back to adb:
```
curl -Ls "https://get.maestro.mobile.dev" | bash    # adds ~/.maestro/bin
```

### 2. Boot the AVD
```
emulator -list-avds
emulator -avd <avd> -no-snapshot -netdelay none -netspeed full &
adb wait-for-device
adb shell 'while [ "$(getprop sys.boot_completed)" != 1 ]; do sleep 1; done'
```
`avdmanager create avd -n <n> -k "system-images;android-35;google_apis;arm64-v8a"` makes one if
needed.

### 3. Install deps + typecheck (the floor gate)
```
cd <fe-repo>
yarn install
node_modules/.bin/tsc --noEmit
```
`yarn install` is not one-time: a base merge can pull in a new native dep, and the first symptom
is Metro's red box `Unable to resolve module`. Reinstall, then rebuild the APK — a new *native*
module also needs a fresh `installDebug`. An old RN version may need an older node flag or its own
Metro port; the overlay records which.

A repo with **pre-existing tsc errors**: never read the raw count as a verdict — diff it:
`git stash && tsc --noEmit | grep -c 'error TS'` vs the same with your change, and check there are
no new errors in the lines you touched. Clean tsc is the floor (`compile-clean`), not proof the
screen renders — keep going.

### 4. Native modules — they bite at BUILD time first
A stale native module that hardcodes an old `compileSdkVersion` fails the Android build under a
modern AGP + JDK before you ever see a screen. Fix it **without editing the repo** via a Gradle
init-script that bumps every android-library subproject:
```gradle
// <scratchpad>/qa-compilesdk-fix.gradle  (scratchpad dir from your system prompt; /tmp is refused)
allprojects { afterEvaluate { project ->
    if (project.plugins.hasPlugin('com.android.library')) {
        project.android { compileSdkVersion 35; buildToolsVersion '35.0.0' }
    }
}}
```
```
cd android && ./gradlew app:installDebug \
  --init-script <scratchpad>/qa-compilesdk-fix.gradle -PreactNativeDevServerPort=8081
# then: adb reverse tcp:8081 tcp:8081 && adb shell am start -n <applicationId>/.MainActivity
```
The same init-script trick overrides a pinned `ndkVersion` that isn't installed. An old
AGP/Gradle needs the JDK it was built for (`JAVA_HOME`) and its build-tools version.

`adb shell monkey` often exits `-5` without launching — use `am start -n <applicationId>/.MainActivity`
(`applicationId` is in `android/app/build.gradle`). A first launch right after install can die on a
boot broadcast receiver's `ClassNotFoundException` (not the UI) — just launch again. If
`am force-stop` provokes that same crash on the next start, a capture script should force-stop
once then start twice (the first start dies, the second mounts); a retry loop that force-stops
on every attempt never gets past it.

At runtime a hardware NativeModule (a scanner) is absent on a generic AVD. A list/detail screen
that **renders from backend data** mounts fine; only **scan-gated** navigation needs the app's
manual-entry fallback or a synthetic scan (`adb` intent).

### 5. Run + wire to a backend
```
yarn start &            # Metro
yarn android            # builds + installs the debug APK onto the running emulator
```
The screen needs data — and this is usually where rung 1 dies. A handheld app that authenticates
by QR-scan / SSO against a *deployed* environment cannot reach the target screen with live data
when the feature isn't deployed yet (common — backend deploys first) or you lack credentials.
Don't fight it: **intercept the app's API call at the Metro layer** and return the known response
payload. That is still a **real on-device pixel render** — just a stubbed network. Say which one
you did in the findings.

### 6. Drive + capture
```
# Maestro flow (preferred)
maestro test flow.yaml          # tapOn text/id, inputText, takeScreenshot: <name>
# or adb directly
adb shell input tap <x> <y>; adb shell input text "<id>"
adb exec-out screencap -p > <scratchpad>/qa-<app>-<case>.png
```
Capture the **positive case** (change visible) and the **negative case** (change absent) for each
app. Then read the runtime log — tsc-clean ≠ runtime-safe:
```
adb logcat -d | grep -iE 'ReactNativeJS|error|exception' | tail -50
```
Known noise on a generic AVD: a boot-receiver `ClassNotFoundException` is a **background
broadcast-receiver** crash, not the UI — the app still renders. Don't chase it. Confirm render via
`ActivityTaskManager: Displayed <pkg>/.MainActivity` + `ReactNativeJS: Running "<app>"` in logcat.

### Render paths
Before adding or checking a UI element, read the app's navigation and find **every** surface that
renders the entity: a mini-app detail screen and a generic entity screen can both show it, and the
change must land on each one ops actually reach. The overlay records the paths already traced.

### Degradation ladder — always report which rung you reached
1. **on-device** — emulator wired to a live seeded backend. Best.
2. **rendered (real component on the emulator, payload from the backend test)** — the proven
   no-auth path. Recipe:
   1. In the backend integration test, dump the real responses the screens consume to JSON
      (temporary `json.dump` behind an env var; delete after capture). The payload is then the
      true contract, not an invented shape.
   2. In the app repo add a throwaway `qa-harness/Harness.tsx` that mounts the **real** screen
      component with those payloads, wrapped in the app's UI-kit provider and theme mapping,
      after the app's i18n init. Supply the contexts the component consumes directly (auth,
      alerts, a `NavigationContainer` + one `Stack.Screen` with the entity in `initialParams`).
      Add buttons to switch payload/surface so one build covers every state.
   3. Point `index.js` at the harness, build, capture with `adb exec-out screencap`, then
      `git checkout index.js` and delete the harness. Fast-refresh does not always pick up a new
      import — `adb shell am force-stop <pkg>` and relaunch, and confirm with
      `dumpsys activity activities | grep topResumedActivity` (with several apps installed, the
      wrong one can be in front).
   4. **Re-running a harness needs no rebuild.** The debug APK loads its JS from Metro, so once the app
      is installed you can edit the harness, restart Metro and relaunch. Only a native-dep change needs
      `installDebug` again.
   5. **For screenshots that ship** (an SOP, a PR), give the harness **no on-screen controls**: fire the
      sequence from `setTimeout` on mount and forward only the final toast — a queued alert banner
      holds for seconds, so N scans back-to-back means N× that wait. Buttons are fine while
      exploring, but they land in the frame and read as test scaffolding.
   6. **Timing the capture:** `ReactNativeJS: Running "<app>"` precedes the component mount by seconds,
      so a fixed sleep off that marker misses the banner. Burst-capture (6–7 shots ~1.2s apart) and pick
      the frame afterwards.
   Storybook, where the app ships it, is an alternative; the harness needs none.
3. **rendered (mocked network)** — emulator pixels, app API stubbed at Metro. Requires getting
   **past login** first, so only viable once auth is solved.
4. **rendered (device-free harness)** — jest + `@testing-library/react-native` (or bare
   `react-test-renderer` and a `toJSON()` snapshot), mock the service payload, wrap in the app's
   UI-kit provider, assert the change in the rendered tree. No pixels, but proves the component
   logic + props path.
5. **compile-clean** — tsc only. The floor. Never call this "tested on device".

Descend one rung at a time when a rung blocks, and name the blocker in the findings. **Never fake a
rung:** a hand-built HTML/CSS mockup of the screen is NOT a "rendered" rung — it's a fabrication.
"Rendered" means the real component on a real surface (emulator, Storybook, or harness tree). If
every rung above compile-clean is blocked, report that honestly with a placeholder — never a
look-alike.

## B. Web apps — Playwright MCP (secondary)

Start the dev server the way the repo's rules say, then **stub auth before navigating** —
`browser_route` the identity endpoint to return a mock 200 user (no SSO redirect), and route any
data endpoints the route calls. Then `browser_navigate` to the route, `browser_take_screenshot`,
and read `browser_console_messages` + `browser_network_requests`.

For a **local static HTML artifact** (not a dev server), serve it — `python3 -m http.server <port>`
— and navigate `http://localhost:<port>/…`; **never `file://`** (blocked). The screenshot PNG isn't
on an accessible FS here, so trust the **accessibility snapshot + console messages** as ground
truth, not the image.
