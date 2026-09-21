import logging
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)

class IpcServer(QObject):
    """Listens for incoming magnet links from secondary instances."""
    magnet_received = Signal(str)

    def __init__(self, server_name="PeerStreamIPC", parent=None):
        super().__init__(parent)
        self.server_name = server_name
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_new_connection)

    def start(self):
        # QLocalServer sometimes leaves stale sockets around on Windows if it crashed previously.
        QLocalServer.removeServer(self.server_name)
        if not self.server.listen(self.server_name):
            logger.error("Failed to start IPC server: %s", self.server.errorString())
        else:
            logger.debug("IPC server started on %s", self.server_name)

    @Slot()
    def _on_new_connection(self):
        socket = self.server.nextPendingConnection()
        if not socket:
            return

        # Keep a reference to the socket while it's reading
        socket.readyRead.connect(lambda: self._on_ready_read(socket))
        socket.disconnected.connect(socket.deleteLater)

    def _on_ready_read(self, socket: QLocalSocket):
        data = socket.readAll().data().decode('utf-8')
        if data.startswith("magnet:"):
            logger.info("Received magnet link via IPC")
            self.magnet_received.emit(data)

def send_magnet_to_primary(magnet_uri: str, server_name="PeerStreamIPC", timeout_ms=2000) -> bool:
    """Attempts to connect to the primary instance and send the magnet link.
    Returns True if successful (meaning we are a secondary instance)."""
    socket = QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(timeout_ms):
        socket.write(magnet_uri.encode('utf-8'))
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return True
    return False
