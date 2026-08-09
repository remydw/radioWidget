import QtQuick
import QtCore
import Quickshell
import qs.Common
import qs.Widgets
import qs.Modules.Plugins
import Quickshell.Io

PluginComponent {
    id: root
    readonly property string pluginId: "rtl2RadioWidget"

    signal toggleRequested

    // Resolve the DMS settings file for the current user (same derivation
    // as DMS's own SettingsData): ~/.config/DankMaterialShell/...
    readonly property string settingsPath: Paths.strip(StandardPaths.writableLocation(StandardPaths.ConfigLocation)) + "/DankMaterialShell/plugin_settings.json"

    // ---- Live state -----------------------------------------------------
    // DMS never re-parses backend writes into its in-memory pluginData, so
    // read the settings file directly and react to backend syncs live.
    FileView {
        id: stateFile
        path: root.settingsPath
        preload: true
        atomicWrites: false
        printErrors: true
        watchChanges: true
        onFileChanged: stateFile.reload()
        onLoaded: root.refreshState()
    }

    property var widgetState: ({})

    function refreshState() {
        try {
            const parsed = JSON.parse(stateFile.text())
            const w = (parsed && parsed.rtl2RadioWidget) ? parsed.rtl2RadioWidget : {}
            root.widgetState = w
        } catch (e) {
            console.error("[RTL2] state parse failed:", e.message)
        }
    }

    property string title: (widgetState.title && widgetState.title.length > 0) ? widgetState.title : "RTL2"
    property bool isPlaying: widgetState.isPlaying === true
    property string artist: widgetState.artist || ""
    property string artUrl: widgetState.artUrl || ""
    property int volume: widgetState.volume !== undefined ? widgetState.volume : 100
    // Official RTL2 station logo (from the site's JSON-LD); apple-touch-icon
    // as fallback if the versioned path ever moves.
    property string rtl2LogoUrl: "https://static.rtl2.fr/versions/www/7.0.413/img/radios/rtl2.png"

    // ---- Pill volume wheel ----------------------------------------------
    // The pill shows only the play/pause state + "RTL2"; hovering it and
    // scrolling the wheel adjusts volume (shared with the popout slider via
    // the settings file). The pill briefly shows the percentage as feedback.
    property int volDisplay: -1

    Timer {
        id: volResetTimer
        interval: 1500
        repeat: false
        onTriggered: root.volDisplay = -1
    }

    function wheelVolume(delta) {
        const v = Math.max(0, Math.min(100, root.volume + delta))
        if (v !== root.volume) {
            root.writeWidgetField("volume", v)
            root.volDisplay = v
            volResetTimer.restart()
        }
    }

    // ---- Toggle / settings writes --------------------------------------
    // pluginService is NOT reliably injected into bar widget instances
    // (observed NULL in the live bar even after load), so widget writes go
    // straight into plugin_settings.json — the same file the backend's 1s
    // poll reads. This is the exact contract that is proven end-to-end,
    // and it needs no DMS plugin API at all.
    function writeWidgetField(key, value) {
        try {
            const parsed = JSON.parse(stateFile.text() || "{}")
            const w = parsed.rtl2RadioWidget || {}
            w[key] = value
            parsed.rtl2RadioWidget = w
            stateFile.setText(JSON.stringify(parsed, null, 2))
        } catch (e) {
            console.error("[RTL2] write " + key + " failed:", e.message)
        }
    }

    Connections {
        target: root
        function onToggleRequested() {
            // Unique value per click: the backend treats each "toggle:"
            // action as a fresh request and clears it after processing.
            root.writeWidgetField("action", "toggle:" + Date.now())
        }
    }

    // Pill: play/pause icon + "RTL2" (detail lives in the media tab and the
    // hover popout). Hover the pill and scroll the wheel for volume; the
    // percentage shows briefly in place of "RTL2".
    horizontalBarPill: Item {
        implicitWidth: hIcon.implicitWidth + hText.implicitWidth + Theme.spacingS * 3
        implicitHeight: Theme.iconSize + 8

        MouseArea {
            anchors.fill: parent
            onClicked: root.toggleRequested()
            onWheel: wheelEvent => {
                root.wheelVolume(wheelEvent.angleDelta.y > 0 ? 5 : -5)
                wheelEvent.accepted = true
            }
        }

        DankIcon {
            id: hIcon
            name: root.isPlaying ? "pause" : "radio"
            color: Theme.primary
            size: Theme.iconSize - 4
            anchors.left: parent.left
            anchors.leftMargin: Theme.spacingS
            anchors.verticalCenter: parent.verticalCenter
        }

        StyledText {
            id: hText
            text: root.volDisplay >= 0 ? root.volDisplay + "%" : "RTL2"
            color: Theme.surfaceText
            font.pixelSize: Theme.fontSizeSmall
            elide: Text.ElideRight
            wrapMode: Text.NoWrap
            anchors.left: hIcon.right
            anchors.leftMargin: Theme.spacingXS
            anchors.right: parent.right
            anchors.rightMargin: Theme.spacingS
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    verticalBarPill: Item {
        implicitWidth: Math.max(Theme.iconSize + Theme.spacingS * 2, vText.implicitWidth + Theme.spacingS * 2)
        implicitHeight: Theme.spacingS + (Theme.iconSize - 4) + Theme.spacingXS + vText.implicitHeight + Theme.spacingS

        MouseArea {
            anchors.fill: parent
            onClicked: root.toggleRequested()
            onWheel: wheelEvent => {
                root.wheelVolume(wheelEvent.angleDelta.y > 0 ? 5 : -5)
                wheelEvent.accepted = true
            }
        }

        DankIcon {
            id: vIcon
            name: root.isPlaying ? "pause" : "radio"
            color: Theme.primary
            size: Theme.iconSize - 4
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: Theme.spacingS
        }

        StyledText {
            id: vText
            text: root.volDisplay >= 0 ? root.volDisplay + "%" : "RTL2"
            color: Theme.surfaceText
            font.pixelSize: Theme.fontSizeSmall
            elide: Text.ElideRight
            wrapMode: Text.NoWrap
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: vIcon.bottom
            anchors.topMargin: Theme.spacingXS
        }
    }

    popoutWidth: 380
    popoutHeight: 260

    // Content must contribute to PopoutComponent's (a Column) implicitHeight
    // — anchor-positioned children are excluded from Column layout, so the
    // popup would collapse to just the header. Plain children + explicit
    // heights keep the hover/click popout fully sized (art, title, artist).
    popoutContent: Component {
        PopoutComponent {
            id: popout
            headerText: "RTL2"
            spacing: Theme.spacingS
            property var closePopout: root.closePopout

            // Album art — track art while playing, the RTL2 logo when paused
            Item {
                width: parent.width
                height: 170
                Image {
                    anchors.centerIn: parent
                    width: 150
                    height: 150
                    source: root.isPlaying ? root.artUrl : root.rtl2LogoUrl
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    visible: root.isPlaying ? root.artUrl.length > 0 : true
                    onStatusChanged: {
                        if (status === Image.Error && source === root.rtl2LogoUrl)
                            root.rtl2LogoUrl = "https://www.rtl2.fr/apple-touch-icon.png"
                    }
                }
                DankIcon {
                    anchors.centerIn: parent
                    name: "music_note"
                    size: 96
                    color: Theme.surfaceVariantText
                    visible: root.isPlaying && root.artUrl.length === 0
                }
            }

            // Title
            StyledText {
                width: parent.width
                text: root.title
                font.bold: true
                font.pixelSize: Theme.fontSizeLarge
                color: Theme.surfaceText
                elide: Text.ElideMiddle
                horizontalAlignment: Text.AlignHCenter
            }

            // Artist
            StyledText {
                width: parent.width
                text: root.artist
                font.pixelSize: Theme.fontSizeMedium
                color: Theme.surfaceVariantText
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignHCenter
                visible: root.artist.length > 0
            }

            // Volume: drag the slider or use the mouse wheel
            // (DankSlider wheelEnabled). Writes are debounced during drags
            // and flushed on release; the backend's 1s poll applies them.
            Row {
                width: parent.width
                spacing: Theme.spacingS

                DankIcon {
                    name: "volume_up"
                    size: Theme.iconSize
                    color: Theme.surfaceVariantText
                    anchors.verticalCenter: parent.verticalCenter
                }

                DankSlider {
                    id: volumeSlider
                    width: parent.width - Theme.iconSize - Theme.spacingS
                    value: root.volume
                    minimum: 0
                    maximum: 100
                    step: 5
                    wheelEnabled: true
                    showValue: true
                    unit: "%"
                    onSliderValueChanged: v => {
                        root.widgetVolume = v
                        volumeWriteTimer.restart()
                    }
                    onSliderDragFinished: v => {
                        root.widgetVolume = v
                        volumeWriteTimer.stop()
                        root.writeWidgetField("volume", v)
                    }
                }
            }
        }
    }

    property int widgetVolume: root.volume
    Timer {
        id: volumeWriteTimer
        interval: 150
        repeat: false
        onTriggered: root.writeWidgetField("volume", root.widgetVolume)
    }
}
