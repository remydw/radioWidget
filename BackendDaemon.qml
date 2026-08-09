import QtQuick
import Quickshell
import Quickshell.Io

// Lifecycle daemon for the RTL2 radio backend.
//
// DMS instantiates this object only while the plugin is enabled
// (PluginService gates daemon surfaces on "enabled" in
// plugin_settings.json), so this component IS the autostart hook:
//   - on load: connect to /tmp/rtl2_radio.sock; if nothing answers,
//     spawn backend/manager.py (adopts an already-running backend
//     otherwise)
//   - on unexpected backend death: the channel drops, we respawn
//   - on unload/disable/DMS quit: QUIT over the persistent daemon channel;
//     if the daemon dies abruptly (crash), the channel EOF shuts the
//     backend down the same way
//
// Quickshell's Socket only auto-reconnects after a *successful* connect
// that then drops (onSocketDisconnected). A *refused* connect leaves a
// stale QLocalSocket in place: connected=true becomes a silent no-op and
// connectionStateChanged never fires again. The only reliable recovery is
// to destroy the Socket and create a fresh one per attempt, which is what
// connectDaemon() does.
//
// The backend is spawned detached (`&`): quickshell's Process teardown
// SIGKILLs its direct child on unload, which would kill the manager before
// it could stop mpv and orphan the stream. A detached backend survives the
// teardown and shuts itself down when the daemon channel goes away.
Item {
    id: root

    readonly property string pluginDir: {
        const url = Qt.resolvedUrl(".");
        return url.toString().replace(/^file:\/\//, "");
    }
    readonly property string backendScript: pluginDir + "/manager.py"
    readonly property string backendLog: "/tmp/rtl2_backend.log"
    readonly property string sockPath: "/tmp/rtl2_radio.sock"
    // Set by PluginService._createDaemonInstance; declared to silence
    // initial-property warnings (pluginService also gives direct access to
    // the settings surface if ever needed).
    property string pluginId: ""
    property var pluginService: null


    property bool backendUp: false
    property bool _stopRequested: false
    property int _lastSpawnMs: 0
    // The single live daemon channel instance (null when disconnected).
    // A connected channel proves a backend is listening (adopt); a refused
    // or dropped connection means none is (spawn).
    property var daemonSock: null

    function startBackend() {
        if (root.backendUp || root._stopRequested)
            return
        // Throttle respawns so a backend that crashes in a loop doesn't
        // spin the spawner. The retry timer keeps probing the socket, so a
        // slow or flaky start still gets adopted the moment it listens.
        const now = Date.now()
        if (now - root._lastSpawnMs < 3000)
            return
        root._lastSpawnMs = now
        // Detached: the sh wrapper exits immediately and the backend is
        // reparented, so the Process teardown on unload has no child to
        // SIGKILL. The `--daemon-spawned` flag arms the backend's
        // registration timeout (it must hear our HELLO or it shuts down).
        backendProcess.exec(["sh", "-c",
            "setsid python3 -u " + root.backendScript + " --daemon-spawned >> " + root.backendLog + " 2>&1 &"])
    }

    // Connect a fresh daemon channel. Destroys any previous instance first:
    // after a failed connect the old Socket holds a dead QLocalSocket and
    // can never connect again, so each attempt gets a brand-new object.
    function connectDaemon() {
        if (root._stopRequested)
            return
        if (root.daemonSock !== null) {
            root.daemonSock.destroy()
            root.daemonSock = null
        }
        root.daemonSock = daemonSockFactory.createObject(root)
        root.daemonSock.connected = true
    }

    Component {
        id: daemonSockFactory
        Socket {
            id: sock
            path: root.sockPath
            onConnectionStateChanged: {
                // Ignore signals from a superseded instance being destroyed.
                if (sock !== root.daemonSock)
                    return
                if (connected) {
                    root.backendUp = true
                    sock.write("HELLO\n")
                    sock.flush()
                } else if (!root._stopRequested) {
                    // Dropped after connecting: the backend died (or never
                    // answered). Respawn it and reconnect.
                    root.backendUp = false
                    root.startBackend()
                    retryTimer.start()
                }
            }
            onError: (error) => {
                if (sock !== root.daemonSock || root._stopRequested)
                    return
                // Refused / not found: no backend listening. Spawn it and
                // keep retrying until it answers.
                root.backendUp = false
                root.startBackend()
                retryTimer.start()
            }
        }
    }

    Timer {
        id: retryTimer
        interval: 1000
        repeat: false
        onTriggered: root.connectDaemon()
    }

    // Backend died while the plugin is still enabled: the channel drop
    // triggers a respawn. The Process itself is only the spawn conduit —
    // its sh child exits immediately by design.
    Process {
        id: backendProcess
        command: ["true"]
        onExited: (exitCode, exitStatus) => {
            // Spawn wrapper exited, not the backend: nothing to do here.
        }
    }

    Component.onCompleted: {
        console.log("[RTL2] Daemon up; backend:", root.backendScript)
        root.connectDaemon()
    }

    Component.onDestruction: {
        console.log("[RTL2] Daemon destroyed; backendUp:", root.backendUp,
            "stopRequested:", root._stopRequested)
        root._stopRequested = true
        if (root.daemonSock !== null && root.daemonSock.connected) {
            root.daemonSock.write("QUIT\n")
            root.daemonSock.flush()
        }
    }
}
