import QtQuick
import Quickshell.Io

Item {
  id: root

  // Injected by omarchy-shell when loading service plugins.
  property var shell: null

  readonly property string scriptPath: Qt.resolvedUrl("nimbus_fix.py").toString().replace(/^file:\/\//, "")
  property bool shuttingDown: false

  Process {
    id: bridgeProcess
    command: ["python3", "-u", root.scriptPath]

    stderr: SplitParser {
      onRead: function(line) {
        if (line.trim().length > 0) {
          console.warn("[steelseries.nimbus-fix]", line)
        }
      }
    }

    onExited: function(exitCode) {
      if (!root.shuttingDown && exitCode !== 0) {
        console.warn("[steelseries.nimbus-fix] daemon exited with code", exitCode, "- restarting")
        restartTimer.restart()
      }
    }
  }

  Timer {
    id: restartTimer
    interval: 3000
    repeat: false
    onTriggered: {
      if (!root.shuttingDown && !bridgeProcess.running) {
        bridgeProcess.running = true
      }
    }
  }

  Component.onCompleted: {
    bridgeProcess.running = true
  }

  Component.onDestruction: {
    root.shuttingDown = true
    restartTimer.stop()
    if (bridgeProcess.running) {
      bridgeProcess.running = false
    }
  }
}
