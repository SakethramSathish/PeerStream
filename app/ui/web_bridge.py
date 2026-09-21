import json
import logging
import time
from PySide6.QtCore import QObject, Slot, Signal
from PySide6.QtWidgets import QFileDialog, QSystemTrayIcon

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
        self._added_times = {} # info_hash -> float
        self._parsed_torrents = {} # info_hash -> (MagnetResolution|None, Torrent)


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
                service = self._bridge.session.get(t.info_hash)
                metrics = t.metrics

                # Count global peers
                if service:
                    global_peers += len(service.engine.peers.connections)

                if t.info_hash not in self._added_times:
                    self._added_times[t.info_hash] = getattr(t.resumed, "saved_at", now) if t.resumed else now

                t_dict = {
                    "id": t.info_hash,
                    "name": t.name,
                    "size": t.total_length,
                    "pieces": t.piece_count,
                    "have": t.verified_pieces,
                    "ps": [],
                    "state": t.state.value,
                    "dl": metrics.download.displayed if metrics else 0.0,
                    "ul": metrics.upload.displayed if metrics else 0.0,
                    "peers": [],
                    "downloaded": metrics.download.total if metrics else 0,
                    "uploaded": metrics.upload.total if metrics else 0,
                    "added": self._added_times[t.info_hash] * 1000, 
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
                                "st": "ok" if "ok" in tr.state.value.lower() else "idle" if "unknown" in tr.state.value.lower() else "err",
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
            dht_buckets = []
            dht = getattr(self._bridge.session, '_dht', None)
            if dht and dht.table:
                dht_nodes = dht.table.size
                dht_buckets = [len(b) for b in dht.table._buckets]
                dht_buckets += [0] * (160 - len(dht_buckets))
            
            s = {
                "sDown": snapshot.totals.downloaded_bytes,
                "sUp": snapshot.totals.uploaded_bytes,
                "dl": snapshot.totals.download_rate,
                "ul": snapshot.totals.upload_rate,
                "torrents": torrents,
                "dht": {"nodes": dht_nodes, "buckets": dht_buckets},
                "globalPeers": global_peers
            }
            self.stateChanged.emit(json.dumps(s))

        except Exception as e:
            logger.exception("Failed to serialize snapshot")

    def _on_events(self):
        # Ignore these completely to avoid spamming the UI log / QWebChannel
        ignore_events = {
            "piece_block_received", "piece_requested", "piece_downloaded", "piece_uploaded",
            "disk_write", "stats_sample", "dht_node_discovered", "dht_query",
            "dht_announced", "dht_response", "peer_connecting", "peer_discovered",
            "peer_bitfield", "peer_choked", "peer_unchoked", "peer_interested"
        }
        for ev in self._bridge.feed.drain():
            if ev.type.value in ignore_events:
                continue

            sev = "info"
            if "error" in ev.type.value.lower() or "fail" in ev.type.value.lower(): sev = "err"
            elif "warn" in ev.type.value.lower(): sev = "warn"
            elif "ok" in ev.type.value.lower() or "completed" in ev.type.value.lower(): sev = "ok"
            e_dict = {
                "sev": sev,
                "src": ev.type.value,
                "msg": ev.message,
                "tid": ev.torrent_id
            }
            self.eventPushed.emit(json.dumps(e_dict))

    @Slot(str)
    def toggle_torrent(self, info_hash: str):
        service = self._bridge.session.get(info_hash)
        if not service: return
        view = service.view()
        if view.active:
            self._bridge.submit(service.pause())
        else:
            self._bridge.submit(service.resume())

    @Slot(str, bool)
    def remove_torrent(self, info_hash: str, delete_data: bool):
        self._bridge.submit(self._bridge.session.remove(info_hash, delete_data=delete_data))

    @Slot()
    def minimize_tray(self):
        window = self.parent()
        if window:
            window.hide()
            window.tray_icon.show()
            window.tray_icon.showMessage("PeerStream", "Running in background.", QSystemTrayIcon.Information, 2000)

    @Slot()
    def exit_app(self):
        window = self.parent()
        if window:
            window.force_close()

    @Slot(str)
    def open_folder(self, info_hash: str):
        import os, subprocess
        service = self._bridge.session.get(info_hash)
        if not service: return
        path = str(service.download_directory)
        try:
            if os.name == 'nt':
                os.startfile(path)
            elif os.uname().sysname == 'Darwin':
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            logger.error("Failed to open folder: %s", e)

    def _on_add_done(self, fut):
        import json
        try:
            fut.result()
        except Exception as e:
            logger.error("Error adding torrent: %s", e, exc_info=True)
            err_dict = {"sev": "err", "src": "ENGINE", "msg": f"Magnet resolution failed: {e}", "tid": None}
            self.eventPushed.emit(json.dumps(err_dict))

    @Slot(str)
    def parse_magnet(self, magnet: str) -> None:
        """Parse a magnet link and return metadata to the UI without starting the engine."""
        import json
        info_dict = {"sev": "warn", "src": "ENGINE", "msg": "Resolving magnet link... (this may take a minute)", "tid": None}
        self.eventPushed.emit(json.dumps(info_dict))
        
        def _on_resolved(fut):
            try:
                resolution = fut.result()
                torrent = resolution.torrent
                self._parsed_torrents[torrent.info_hash.hex()] = (resolution, torrent)
                
                # Emit to UI
                meta = {
                    "info_hash": torrent.info_hash.hex(),
                    "name": torrent.name,
                    "size": torrent.total_length,
                    "files": [[f.path.name, f.length] for f in torrent.files],
                    "peers": len(resolution.peers),
                    "seeds": 0,
                    "trackers": len(torrent.trackers)
                }
                event = {"sev": "info", "src": "TORRENT_PARSED", "msg": json.dumps(meta), "tid": None}
                self.eventPushed.emit(json.dumps(event))
            except Exception as e:
                logger.error("Error resolving magnet: %s", e, exc_info=True)
                err_dict = {"sev": "err", "src": "ENGINE", "msg": f"Magnet resolution failed: {e}", "tid": None}
                self.eventPushed.emit(json.dumps(err_dict))

        fut = self._bridge.submit(self._bridge.session.resolve_magnet_only(magnet))
        fut.add_done_callback(_on_resolved)

    @Slot()
    def browse_download_dir(self):
        """Open a directory picker."""
        QFileDialog.getExistingDirectory(None, "Select Download Directory", "")

    @Slot()
    def browse_torrent_file(self):
        """Open a file dialog to pick a .torrent file."""
        import json
        path, _ = QFileDialog.getOpenFileName(None, "Open Torrent", "", "Torrents (*.torrent)")
        if path:
            from app.torrent import parse_torrent_file
            try:
                torrent = parse_torrent_file(path)
                self._parsed_torrents[torrent.info_hash.hex()] = (None, torrent)
                
                meta = {
                    "info_hash": torrent.info_hash.hex(),
                    "name": torrent.name,
                    "size": torrent.total_length,
                    "files": [[f.path.name, f.length] for f in torrent.files],
                    "peers": 0,
                    "seeds": 0,
                    "trackers": len(torrent.trackers)
                }
                event = {"sev": "info", "src": "TORRENT_PARSED", "msg": json.dumps(meta), "tid": None}
                self.eventPushed.emit(json.dumps(event))
            except Exception as e:
                logger.error("Failed to parse torrent file: %s", e, exc_info=True)
                err_dict = {"sev": "err", "src": "ENGINE", "msg": f"Torrent parsing failed: {e}", "tid": None}
                self.eventPushed.emit(json.dumps(err_dict))

    @Slot(str)
    def parse_torrent_base64(self, b64_data: str):
        """Parse a base64 encoded .torrent file (from drag and drop)."""
        import base64, json
        from app.torrent import parse_torrent
        try:
            data = base64.b64decode(b64_data)
            torrent = parse_torrent(data)
            self._parsed_torrents[torrent.info_hash.hex()] = (None, torrent)
            
            meta = {
                "info_hash": torrent.info_hash.hex(),
                "name": torrent.name,
                "size": torrent.total_length,
                "files": [[f.path.name, f.length] for f in torrent.files],
                "peers": 0,
                "seeds": 0,
                "trackers": len(torrent.trackers)
            }
            event = {"sev": "info", "src": "TORRENT_PARSED", "msg": json.dumps(meta), "tid": None}
            self.eventPushed.emit(json.dumps(event))
        except Exception as e:
            logger.error("Failed to parse dropped torrent file: %s", e, exc_info=True)
            err_dict = {"sev": "err", "src": "ENGINE", "msg": f"Torrent parsing failed: {e}", "tid": None}
            self.eventPushed.emit(json.dumps(err_dict))

    @Slot(str)
    def confirm_add_torrent(self, config_json: str):
        import json
        try:
            config = json.loads(config_json)
            info_hash = config.get("info_hash")
            if info_hash not in self._parsed_torrents:
                raise ValueError("Torrent metadata not found in cache. Please try parsing again.")
            
            resolution, torrent = self._parsed_torrents.pop(info_hash)
            
            # TODO: filter files based on config.get("files") once the engine supports it
            
            fut = self._bridge.submit(self._bridge.session.add_torrent(
                torrent, 
                directory=config.get("save_dir"),
                start=config.get("start", True)
            ))
            
            def _on_added(f):
                try:
                    engine = f.result()
                    if resolution and resolution.peers:
                        engine.add_peers(list(resolution.peers), source="magnet")
                except Exception as e:
                    logger.error("Error adding torrent: %s", e, exc_info=True)
                    err_dict = {"sev": "err", "src": "ENGINE", "msg": f"Failed to add torrent: {e}", "tid": None}
                    self.eventPushed.emit(json.dumps(err_dict))

            fut.add_done_callback(_on_added)
        except Exception as e:
            logger.error("Error confirming add torrent: %s", e, exc_info=True)
