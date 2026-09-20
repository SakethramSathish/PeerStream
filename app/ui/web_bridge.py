import json
import logging
import time
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QFileDialog

from app.services.app_state import AppSnapshot
from app.ui.bridge import EngineBridge

logger = logging.getLogger(__name__)

class WebBridge(QObject):
    stateChanged = Signal(str)
    eventPushed = Signal(str)

    def __init__(self, bridge: EngineBridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._selected_torrent_id = None
        self._last_peers = {} # info_hash -> { peer_key: (downloaded, uploaded, time) }

        # Connect to the EngineBridge signals
        self._bridge.pump.snapshot.connect(self._on_snapshot)
        self._bridge.feed.events_pending.connect(self._on_events)

    @Slot()
    def ready(self):
        """Called by the JS frontend when QWebChannel is ready."""
        self._bridge.refresh()

    @Slot(str)
    def select_torrent(self, info_hash: str):
        """Called by the JS frontend when a torrent row is clicked."""
        self._selected_torrent_id = info_hash
        self._bridge.refresh()

    def _on_snapshot(self, snapshot: AppSnapshot):
        try:
            now = time.monotonic()
            torrents = []
            global_peers = 0

            for t in snapshot.torrents:
                service = self._bridge.session.service_for(t.info_hash)
                metrics = t.metrics

                # Count global peers
                if service:
                    global_peers += len(service.engine.peers.connections)

                t_dict = {
                    "id": t.info_hash,
                    "name": t.name,
                    "size": t.total_length,
                    "pieces": t.piece_count,
                    "have": t.verified_pieces,
                    "ps": [],
                    "state": t.state.value,
                    "dl": metrics.download_rate if metrics else 0.0,
                    "ul": metrics.upload_rate if metrics else 0.0,
                    "peers": [],
                    "downloaded": metrics.downloaded_bytes if metrics else 0,
                    "uploaded": metrics.uploaded_bytes if metrics else 0,
                    "added": 0, 
                    "savePath": "", 
                    "files": [],
                    "trackers": []
                }
                
                # Fetch detailed info if this is the selected torrent
                if t.info_hash == self._selected_torrent_id and service:
                    t_dict["savePath"] = str(service.download_directory)
                    
                    try:
                        # 1. Peers
                        peers_view = service.peers_view()
                        t_peers = []
                        if t.info_hash not in self._last_peers:
                            self._last_peers[t.info_hash] = {}
                        last_peers = self._last_peers[t.info_hash]

                        for p in peers_view:
                            # Calculate peer rates
                            prev_down, prev_up, prev_time = last_peers.get(p.key, (p.downloaded, p.uploaded, now))
                            dt = now - prev_time
                            dl_rate = (p.downloaded - prev_down) / dt if dt > 0 else 0
                            ul_rate = (p.uploaded - prev_up) / dt if dt > 0 else 0
                            last_peers[p.key] = (p.downloaded, p.uploaded, now)

                            t_peers.append({
                                "ip": p.key,
                                "client": p.client,
                                "dl": dl_rate,
                                "ul": ul_rate,
                                "prog": p.share,
                                "held": p.pieces_held
                            })
                        t_dict["peers"] = t_peers

                        # Cleanup disconnected peers
                        current_keys = {p.key for p in peers_view}
                        for k in list(last_peers.keys()):
                            if k not in current_keys:
                                del last_peers[k]

                        # 2. Files
                        files_view = service.files_view()
                        t_dict["files"] = [
                            {
                                "name": f.name,
                                "size": f.length,
                                "prio": "normal"
                            }
                            for f in files_view
                        ]

                        # 3. Trackers
                        trackers_view = service.trackers_view()
                        t_dict["trackers"] = [
                            {
                                "url": tr.url,
                                "st": "ok" if "ok" in tr.status.value.lower() else "idle" if "idle" in tr.status.value.lower() else "err",
                                "peers": tr.seeders + tr.leechers,
                                "next": 0
                            }
                            for tr in trackers_view
                        ]

                        # 4. Pieces
                        piece_map = service.piece_map()
                        t_dict["ps"] = list(piece_map.states)

                    except Exception as e:
                        logger.error(f"Failed to fetch details for {t.info_hash}: {e}")

                torrents.append(t_dict)
            
            # DHT nodes
            dht_nodes = 0
            # Session does not expose dht directly, but we can check the config or just assume 0 for now
            # since DHT status can be extracted from events if needed.
            
            s = {
                "sDown": snapshot.totals.downloaded_bytes,
                "sUp": snapshot.totals.uploaded_bytes,
                "dl": snapshot.totals.download_rate,
                "ul": snapshot.totals.upload_rate,
                "torrents": torrents,
                "dht": {"nodes": dht_nodes},
                "globalPeers": global_peers
            }
            self.stateChanged.emit(json.dumps(s))

        except Exception as e:
            logger.exception("Failed to serialize snapshot")

    def _on_events(self):
        for ev in self._bridge.feed.drain():
            sev = "info"
            if "error" in ev.type.value.lower() or "fail" in ev.type.value.lower(): sev = "err"
            elif "warn" in ev.type.value.lower(): sev = "warn"
            elif "ok" in ev.type.value.lower(): sev = "ok"
            e_dict = {
                "sev": sev,
                "src": ev.type.value,
                "msg": ev.message,
                "tid": ev.torrent_id
            }
            self.eventPushed.emit(json.dumps(e_dict))

    @Slot(str)
    def toggle_torrent(self, info_hash: str):
        service = self._bridge.session.service_for(info_hash)
        if not service: return
        view = service.view()
        if view.active:
            self._bridge.submit(service.pause())
        else:
            self._bridge.submit(service.resume())

    @Slot(str)
    def remove_torrent(self, info_hash: str):
        self._bridge.submit(self._bridge.session.remove_torrent(info_hash, delete_data=False))

    @Slot(str)
    def add_torrent(self, magnet: str):
        self._bridge.submit(self._bridge.session.add_magnet(magnet, "C:\\Users\\ssake\\Downloads"))

    @Slot()
    def browse_download_dir(self):
        path = QFileDialog.getExistingDirectory(None, "Select Download Directory")
        if path:
            toast_ev = {"sev": "info", "src": "UI", "msg": f"Directory picked: {path}", "tid": None}
            self.eventPushed.emit(json.dumps(toast_ev))

    @Slot()
    def browse_torrent_file(self):
        path, _ = QFileDialog.getOpenFileName(None, "Add Torrent", "", "Torrent Files (*.torrent)")
        if path:
            self._bridge.submit(self._bridge.session.add_torrent(path, "C:\\Users\\ssake\\Downloads"))
