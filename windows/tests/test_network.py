from __future__ import annotations

import queue
import tempfile
import threading
import unittest
from pathlib import Path

from linkdrop.config import ConfigStore, Share
from linkdrop.network import LinkDropError, RemoteClient, TransferHTTPServer


class NetworkIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        store_path = self.root / "config.json"
        self.store = ConfigStore(store_path)
        self.store.config.device_name = "Receiver"
        self.store.config.pair_code = "123456"
        self.store.config.inbox_path = str(self.root / "inbox")
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.store.config.shares = [Share("share1", "Shared", str(self.shared))]
        self.store.save()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_list_download_and_upload(self) -> None:
        (self.shared / "说明.txt").write_text("hello", encoding="utf-8")
        events: queue.Queue = queue.Queue()
        server = TransferHTTPServer(("127.0.0.1", 0), self.store, events)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            client = RemoteClient("127.0.0.1", port, "123456")
            info = client.info()
            self.assertEqual(info["device_name"], "Receiver")
            self.assertTrue(info["tls"])
            self.assertEqual(client.peer_fingerprint, info["cert_fingerprint"])
            shares = client.shares()
            self.assertEqual(shares[0]["id"], "share1")
            entries = client.list_folder("share1")
            self.assertEqual(entries[0]["name"], "说明.txt")

            target = self.root / "downloads" / "说明.txt"
            client.download("share1", "说明.txt", target)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")

            folder = self.shared / "folder"
            folder.mkdir()
            (folder / "inside.txt").write_text("zip", encoding="utf-8")
            archive = self.root / "downloads" / "folder.zip"
            client.download_zip("share1", "folder", archive)
            self.assertTrue(archive.exists())
            self.assertGreater(archive.stat().st_size, 0)

            source = self.root / "payload.bin"
            source.write_bytes(b"x" * 4096)
            result_holder = {}

            def prepare() -> None:
                result_holder["result"] = client.prepare_transfer(
                    [(source, "folder/payload.bin")],
                    "batch",
                    "sender-id",
                    "Sender",
                )

            prepare_thread = threading.Thread(target=prepare)
            prepare_thread.start()
            event_name, event = events.get(timeout=2)
            self.assertEqual(event_name, "incoming_request")
            event.decide(True)
            prepare_thread.join(timeout=3)
            prepared = result_holder["result"]
            session_id = prepared["session_id"]
            file_id = prepared["file_items"][0]["id"]
            client.upload_file(source, session_id, file_id, prepared["tokens"][file_id])
            received = self.root / "inbox" / "batch" / "folder" / "payload.bin"
            self.assertEqual(received.read_bytes(), b"x" * 4096)
            event_name, event = events.get(timeout=2)
            self.assertEqual(event_name, "incoming")
            self.assertEqual(event.size, 4096)
        finally:
            server.shutdown()
            server.server_close()

    def test_auth_failure(self) -> None:
        events: queue.Queue = queue.Queue()
        server = TransferHTTPServer(("127.0.0.1", 0), self.store, events)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = RemoteClient("127.0.0.1", server.server_address[1], "654321")
            with self.assertRaisesRegex(Exception, "配对码"):
                client.info()
        finally:
            server.shutdown()
            server.server_close()

    def test_receiver_rejection(self) -> None:
        source = self.root / "payload.bin"
        source.write_bytes(b"payload")
        events: queue.Queue = queue.Queue()
        server = TransferHTTPServer(("127.0.0.1", 0), self.store, events)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = RemoteClient("127.0.0.1", server.server_address[1], "123456")
            errors = []

            def prepare() -> None:
                try:
                    client.prepare_transfer(
                        [(source, "payload.bin")],
                        "batch",
                        "sender-id",
                        "Sender",
                    )
                except Exception as exc:
                    errors.append(exc)

            prepare_thread = threading.Thread(target=prepare)
            prepare_thread.start()
            _name, session = events.get(timeout=2)
            session.decide(False)
            prepare_thread.join(timeout=3)
            self.assertTrue(errors)
            self.assertIn("拒绝", str(errors[0]))
        finally:
            server.shutdown()
            server.server_close()

    def test_checksum_mismatch_is_removed(self) -> None:
        source = self.root / "payload.bin"
        source.write_bytes(b"good-data")
        events: queue.Queue = queue.Queue()
        server = TransferHTTPServer(("127.0.0.1", 0), self.store, events)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = RemoteClient("127.0.0.1", server.server_address[1], "123456")
            holder = {}

            def prepare() -> None:
                holder["result"] = client.prepare_transfer(
                    [(source, "payload.bin")],
                    "checksum",
                    "sender-id",
                    "Sender",
                )

            prepare_thread = threading.Thread(target=prepare)
            prepare_thread.start()
            _name, session = events.get(timeout=2)
            session.decide(True)
            prepare_thread.join(timeout=3)
            source.write_bytes(b"evil-data")
            prepared = holder["result"]
            file_id = prepared["file_items"][0]["id"]
            with self.assertRaises(LinkDropError):
                client.upload_file(source, prepared["session_id"], file_id, prepared["tokens"][file_id])
            final = self.root / "inbox" / "checksum" / "payload.bin"
            self.assertFalse(final.exists())
            self.assertFalse((final.parent / "payload.bin.linkdrop-part").exists())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

