import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from folio_daemon.cache import CacheStore
from folio_daemon.hindsight import HindsightProvider
from folio_daemon.local_fs import LocalFsProvider
from folio_daemon.markdown_utils import (
    extract_hindsight_refs,
    extract_tags,
    extract_title,
    extract_wikilinks,
    parse_frontmatter,
    serialize_frontmatter,
    strip_provenance_headers,
)
from folio_daemon.notion import NotionProvider
from folio_daemon.secrets import CredentialResolver
from folio_daemon.server import FolioEngine, create_http_handler, resolve_system_binary


class MarkdownUtilsTest(unittest.TestCase):
    def test_frontmatter_roundtrip_and_links(self) -> None:
        raw = (
            '---\n'
            'title: "Theorem Gate"\n'
            'author: "thomas"\n'
            'tags: [lean, formal-specs]\n'
            'is_agent_summary: false\n'
            '---\n\n'
            '# Ignored H1\n\n'
            'See [[Bayesian Calibration & Spec-Driven Verification|Bayes]] and [[kp-47c739bec662419d8d05f283de607b9e]].\n'
            'Inline tag #verification but ignore `[[not-a-link]]` and `#not-a-tag` in code.\n'
            'Source citation: src-8b3da7b4484c4e1a.\n'
        )
        fm, body = parse_frontmatter(raw)
        self.assertEqual(fm["title"], "Theorem Gate")
        self.assertEqual(fm["author"], "thomas")
        self.assertEqual(fm["tags"], ["lean", "formal-specs"])
        self.assertIs(fm["is_agent_summary"], False)
        self.assertEqual(extract_title(fm, body, "fallback"), "Theorem Gate")

        links = extract_wikilinks(body)
        self.assertEqual(
            links,
            ["Bayesian Calibration & Spec-Driven Verification", "kp-47c739bec662419d8d05f283de607b9e"],
        )
        tags = extract_tags(fm, body)
        self.assertEqual(tags, ["lean", "formal-specs", "verification"])

        srcs, kps = extract_hindsight_refs(body)
        self.assertEqual(srcs, ["src-8b3da7b4484c4e1a"])
        self.assertEqual(kps, ["kp-47c739bec662419d8d05f283de607b9e"])

        reserialized = serialize_frontmatter(fm, body)
        fm2, body2 = parse_frontmatter(reserialized)
        self.assertEqual(fm2["title"], "Theorem Gate")
        self.assertEqual(body2.strip(), body.strip())

    def test_strip_provenance_headers(self) -> None:
        text = (
            "[PROVENANCE type=paste date=2026-10-01 source_id=src-12345678abcd]\n"
            "[ATTRIBUTION author=thomas]\n\n"
            "# Actual Note\nBody line."
        )
        meta, clean = strip_provenance_headers(text)
        self.assertEqual(meta["source_id"], "src-12345678abcd")
        self.assertTrue(clean.startswith("# Actual Note"))


class LocalFsAndCacheTest(unittest.TestCase):
    def test_starter_seed_atomic_save_fts_and_backlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = FolioEngine(tmp_path / "state", tmp_path / "notes")
            try:
                boot = engine.dispatch("get_bootstrap", {})
                self.assertTrue(boot["providers"]["local_fs"]["connected"])
                self.assertGreaterEqual(boot["providers"]["local_fs"]["document_count"], 2)

                # Search seeded note via SQLite FTS5
                res = engine.dispatch(
                    "unified_search",
                    {"query": "Bayesian tempered posterior", "include_hindsight": False, "include_notion": False},
                )
                self.assertGreaterEqual(len(res["results"]), 1)
                bayes_uri = res["results"][0]["uri"]

                opened = engine.dispatch("open_document", {"uri": bayes_uri})
                self.assertEqual(opened["title"], "Bayesian Calibration & Spec-Driven Verification")
                # Maxwell note links to Bayesian note -> backlink must resolve
                self.assertEqual(len(opened["backlinks"]), 1)
                self.assertEqual(opened["backlinks"][0]["title"], "Maxwell Optimization Loop")

                # Create, save atomically, and verify path traversal rejection
                created = engine.dispatch(
                    "create_local_document",
                    {"title": "Ergodic Martingale Notes", "folder": "Research"},
                )
                updated_md = created["raw_markdown"] + "\nLink back to [[Maxwell Optimization Loop]].\n"
                saved = engine.dispatch(
                    "save_local_document",
                    {"uri": created["uri"], "raw_markdown": updated_md},
                )
                self.assertIn("Link back to", saved["body"])

                mount_id = created["uri"].split("/")[2]
                with self.assertRaises(ValueError):
                    engine.local_fs.save_document(f"local://{mount_id}/../../escape.md", "bad")
            finally:
                engine.shutdown()


class MockRemoteApisTest(unittest.TestCase):
    def test_hindsight_readonly_guard_and_provenance(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/v1/default/banks":
                    self._json({"banks": [{"bank_id": "thomas", "name": "Thomas"}]})
                elif self.path == "/v1/default/banks/thomas/knowledge-base/tree":
                    self._json(
                        {
                            "roots": [
                                {
                                    "kind": "folder",
                                    "id": "kf-spec",
                                    "name": "Engineering",
                                    "children": [
                                        {"kind": "page", "id": "kp-demo1234", "name": "Spec Quality"}
                                    ],
                                }
                            ]
                        }
                    )
                elif self.path == "/v1/default/banks/thomas/knowledge-base/pages/kp-demo1234":
                    self._json(
                        {
                            "id": "kp-demo1234",
                            "name": "Spec Quality",
                            "description": "Synthesized guide",
                            "tags": ["specs"],
                            "timestamp": "2026-11-01T10:00:00Z",
                            "markdown": "# Spec Quality\n\nGrounded in src-abcdef123456 and [[kp-other9999]].",
                        }
                    )
                elif self.path.startswith("/api/sources/src-abcdef123456"):
                    self._json(
                        {
                            "source_id": "src-abcdef123456",
                            "document_id": "doc-human-1",
                            "auteur": "thomas",
                            "type_de_source": "paste",
                            "date_originale": "2026-10-30",
                            "tags": ["raw"],
                            "metadata": {"is_agent_summary": False, "doc_name": "Raw Spec Thoughts"},
                        }
                    )
                elif self.path == "/v1/default/banks/thomas/documents/doc-human-1":
                    self._json(
                        {
                            "original_text": "[PROVENANCE source_id=src-abcdef123456]\n\nHuman-written invariant $x > 0$."
                        }
                    )
                else:
                    self._json({"items": [], "results": []})

            def _json(self, obj: dict) -> None:
                data = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_patch = {
                    "FOLIO_HINDSIGHT_URL": f"http://127.0.0.1:{port}",
                    "FOLIO_HINDSIGHT_READONLY": "1",
                    "HINDSIGHT_API_KEY": "test-hs-token-not-real",
                    "SPARK_MEMORY_API_TOKEN": "test-sm-token-not-real",
                }
                with mock.patch.dict(os.environ, env_patch, clear=False):
                    cache = CacheStore(Path(tmp) / "cache.sqlite3")
                    secrets = CredentialResolver()
                    hs = HindsightProvider(cache, secrets)

                    tree = hs.list_tree("thomas")
                    self.assertEqual(len(tree), 1)
                    self.assertEqual(tree[0].children[0].uri, "hindsight://thomas/page/kp-demo1234")

                    kp_doc = hs.get_document("kp-demo1234", scope="thomas")
                    assert kp_doc is not None
                    self.assertTrue(kp_doc.provenance.is_agent_summary)
                    self.assertIn("src-abcdef123456", kp_doc.provenance.related_sources)

                    src_doc = hs.get_document("src-abcdef123456", scope="thomas")
                    assert src_doc is not None
                    self.assertFalse(src_doc.provenance.is_agent_summary)
                    self.assertEqual(src_doc.provenance.author, "thomas")
                    self.assertIn("$x > 0$", src_doc.body)

                    # Read-only safety guard blocks any write attempt
                    with self.assertRaises(PermissionError):
                        hs.save_source_explicit(content="never write in readonly mode")

                    cache.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_notion_block_normalization(self) -> None:
        blocks = [
            {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "plain_text": "Equations"}]}},
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "plain_text": "Energy "},
                        {"type": "equation", "equation": {"expression": "E = mc^2"}},
                    ]
                },
            },
            {"type": "equation", "equation": {"expression": "\\int_0^1 x^2 dx = \\frac{1}{3}"}},
            {"type": "to_do", "to_do": {"checked": True, "rich_text": [{"type": "text", "plain_text": "Done"}]}},
        ]
        rendered = [NotionProvider._block_to_markdown(b) for b in blocks]
        joined = "\n".join(r for r in rendered if r)
        self.assertIn("## Equations", joined)
        self.assertIn("Energy $E = mc^2$", joined)
        self.assertIn("$$\n\\int_0^1 x^2 dx = \\frac{1}{3}\n$$", joined)
        self.assertIn("- [x] Done", joined)

        entries = [
            {"id": "p-root", "title": "Engineering", "object": "page", "parent_id": None},
            {"id": "p-child", "title": "Roadmap", "object": "page", "parent_id": "p-root"},
            {"id": "db-1", "title": "Tasks", "object": "database", "parent_id": None},
        ]
        nodes = NotionProvider._build_workspace_nodes("perso", entries)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].name, "Engineering")
        self.assertEqual(nodes[0].kind, "folder")
        self.assertEqual(len(nodes[0].children), 1)
        self.assertEqual(nodes[0].children[0].uri, "notion://perso/p-child")
        self.assertEqual(nodes[1].badge, "DB")


class HttpServerAndSseTest(unittest.TestCase):
    def test_static_cache_headers_wasm_mime_and_race_free_sse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ui_root = tmp_path / "ui"
            (ui_root / "assets").mkdir(parents=True)
            (ui_root / "index.html").write_text("<!doctype html><title>Folio</title>", encoding="utf-8")
            (ui_root / "assets" / "app-9f8e7d.js").write_text("console.log('ok');", encoding="utf-8")
            (ui_root / "assets" / "KaTeX_Main-Regular.woff2").write_bytes(b"wOF2")
            (ui_root / "math.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")

            engine = FolioEngine(tmp_path / "state", tmp_path / "notes")
            token = "test-locald-token-0123456789abcdef0123456789abcdef"
            srv = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(engine, 0, token, ui_root))
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/index.html")
                r_html = conn.getresponse()
                r_html.read()
                self.assertEqual(r_html.status, 200)
                self.assertEqual(r_html.getheader("Cache-Control"), "no-cache")

                conn.request("GET", "/assets/app-9f8e7d.js")
                r_js = conn.getresponse()
                r_js.read()
                self.assertEqual(r_js.status, 200)
                self.assertEqual(r_js.getheader("Cache-Control"), "public, max-age=31536000, immutable")

                conn.request("GET", "/math.wasm")
                r_wasm = conn.getresponse()
                r_wasm.read()
                self.assertEqual(r_wasm.status, 200)
                self.assertEqual(r_wasm.getheader("Content-Type"), "application/wasm")
                self.assertEqual(r_wasm.getheader("Cache-Control"), "public, max-age=31536000, immutable")

                conn.request("GET", "/__folio_events")
                r_forbid = conn.getresponse()
                r_forbid.read()
                self.assertEqual(r_forbid.status, 403)
                conn.close()

                sse_conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                sse_conn.request("GET", f"/__folio_events?token={token}")
                sse_resp = sse_conn.getresponse()
                self.assertEqual(sse_resp.status, 200)
                self.assertTrue((sse_resp.getheader("Content-Type") or "").startswith("text/event-stream"))
                ready_line = sse_resp.fp.readline().decode("utf-8")
                self.assertEqual(ready_line, ": ready\n")
                sse_resp.fp.readline()

                engine.push_event("theme:changed", {"mode": "light"})
                data_line = sse_resp.fp.readline().decode("utf-8")
                self.assertTrue(data_line.startswith("data: "))
                ev_obj = json.loads(data_line[len("data: "):].strip())
                self.assertEqual(ev_obj["event"], "theme:changed")
                self.assertEqual(ev_obj["payload"], {"mode": "light"})
                sse_conn.close()
            finally:
                engine.shutdown()
                srv.shutdown()
                srv.server_close()

    def test_binary_resolution_fallback(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(resolve_system_binary("definitely-nonexistent-folio-bin-xyz"))
            self.assertIsNone(CredentialResolver._resolve_binary("definitely-nonexistent-folio-bin-xyz"))


if __name__ == "__main__":
    unittest.main()
