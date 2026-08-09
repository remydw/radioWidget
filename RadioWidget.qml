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

    // ---- Station config ---------------------------------------------------
    readonly property var stations: {
        "rtl2": {
            "name": "RTL2",
            "logo": "https://static.rtl2.fr/versions/www/7.0.413/img/radios/rtl2.png",
            "fallbackLogo": "https://www.rtl2.fr/apple-touch-icon.png"
        },
        "dance895": {
            "name": "Dance 89.5",
            "logo": "https://www.dance895.org/wp-content/uploads/2024/08/logo.svg",
            "fallbackLogo": "https://www.dance895.org/wp-content/uploads/2024/08/cropped-DANCE895_SiteIcon-1-270x270.png"
        }
    }
    readonly property var stationIds: ["rtl2", "dance895"]

    // ---- Live state -------------------------------------------------------
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
            console.error("[RADIO] state parse failed:", e.message)
        }
    }

    property string station: (widgetState.station && widgetState.station.length > 0) ? widgetState.station : "rtl2"
    property var stationInfo: root.stations[root.station] || root.stations["rtl2"]
    property string title: (widgetState.title && widgetState.title.length > 0) ? widgetState.title : root.stationInfo.name
    property bool isPlaying: widgetState.isPlaying === true
    property string artist: widgetState.artist || ""
    property string artUrl: widgetState.artUrl || ""
    property int volume: widgetState.volume !== undefined ? widgetState.volume : 100
    property string stationLogoUrl: root.stationInfo.logo

    // ---- Pill volume wheel -------------------------------------------------
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

    // ---- Toggle / settings writes -----------------------------------------
    function writeWidgetField(key, value) {
        try {
            const parsed = JSON.parse(stateFile.text() || "{}")
            const w = parsed.rtl2RadioWidget || {}
            w[key] = value
            parsed.rtl2RadioWidget = w
            stateFile.setText(JSON.stringify(parsed, null, 2))
        } catch (e) {
            console.error("[RADIO] write " + key + " failed:", e.message)
        }
    }

    Connections {
        target: root
        function onToggleRequested() {
            root.writeWidgetField("action", "toggle:" + Date.now())
        }
    }

    // ---- Pill: play/pause icon + station name ------------------------------
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
            text: root.volDisplay >= 0 ? root.volDisplay + "%" : root.stationInfo.name
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
            text: root.volDisplay >= 0 ? root.volDisplay + "%" : root.stationInfo.name
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
    popoutHeight: 320

    // ---- Popout content ---------------------------------------------------
    popoutContent: Component {
        PopoutComponent {
            id: popout
            headerText: root.stationInfo.name
            spacing: Theme.spacingS
            property var closePopout: root.closePopout

            // Album art — track art while playing, station logo when paused
            Item {
                width: parent.width
                height: 170
                Image {
                    anchors.centerIn: parent
                    width: 150
                    height: 150
                    source: root.isPlaying ? root.artUrl : root.stationLogoUrl
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    visible: root.isPlaying ? root.artUrl.length > 0 : true
                    onStatusChanged: {
                        if (status === Image.Error && source === root.stationLogoUrl)
                            root.stationLogoUrl = root.stationInfo.fallbackLogo
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

            // ---- Station selector ------------------------------------------
            Item {
                width: parent.width
                height: 28

                Row {
                    anchors.centerIn: parent
                    spacing: Theme.spacingS
                    Repeater {
                        model: root.stationIds
                        delegate: Rectangle {
                            id: stationBtn
                            required property string modelData
                            readonly property bool isActive: root.station === modelData
                            implicitWidth: stationLabel.implicitWidth + Theme.spacingS * 3
                            height: 26
                            radius: 4
                            color: isActive ? Theme.primary : Theme.surfaceVariant
                            border.width: isActive ? 0 : 1
                            border.color: Theme.surfaceVariantText

                            StyledText {
                                id: stationLabel
                                text: root.stations[stationBtn.modelData].name
                                color: isActive ? Theme.onPrimary : Theme.surfaceText
                                font.pixelSize: Theme.fontSizeSmall
                                anchors.centerIn: parent
                            }

                            MouseArea {
                                anchors.fill: parent
                                onClicked: {
                                    if (stationBtn.modelData !== root.station) {
                                        root.writeWidgetField("station", stationBtn.modelData)
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Volume slider
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
