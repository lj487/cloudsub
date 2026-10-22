import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from cloudsub.sources import CloudSubError, HttpSource, RemoteFile, WebDAVSource


def listing(*entries):
    return ('<d:multistatus xmlns:d="DAV:">' + ''.join(entries) + '</d:multistatus>').encode()


def entry(href, *, directory=False, size="4", status=200):
    resource = '<d:collection/>' if directory else ''
    length = f'<d:getcontentlength>{size}</d:getcontentlength>' if size is not None else ''
    return f'<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype>{resource}</d:resourcetype>{length}</d:prop><d:status>HTTP/1.1 {status} Status</d:status></d:propstat></d:response>'


class SourcesTest(unittest.TestCase):
    def test_rejects_unsafe_urls(self):
        for url in ("file:///tmp/media", "https://user:secret@example.test/video", "https://example.test/video#secret", "http://example.test/video", "https://example.test/\nvideo", "https://example.test:bad/video"):
            with self.subTest(url=url), self.assertRaises(CloudSubError):
                HttpSource(url)

    def test_download_redirect_does_not_forward_auth_or_cookie(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"Location": "https://cdn.example.test/media?signature=private", "Set-Cookie": "test-session=test-secret; Domain=example.test"})
            return httpx.Response(200, content=b"data")

        with tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/video?token=test-private", "test-user", "test-secret", transport=httpx.MockTransport(handler)) as source:
            target = Path(folder) / "video"
            source.download("", target)
            self.assertEqual(target.read_bytes(), b"data")
        self.assertIn("Authorization", requests[0].headers)
        self.assertNotIn("Authorization", requests[1].headers)
        self.assertNotIn("Cookie", requests[1].headers)

    def test_cookies_not_sent_across_ports(self):
        seen = []

        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(302, headers={"Location": "https://example.test:8443/media", "Set-Cookie": "test-session=test-secret; Path=/"})
            return httpx.Response(200, content=b"data")

        with tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/media", "test-user", "test-secret", transport=httpx.MockTransport(handler)) as source:
            source.download("", Path(folder) / "video")
        self.assertNotIn("Authorization", seen[1].headers)
        self.assertNotIn("Cookie", seen[1].headers)

    def test_same_origin_redirect_keeps_auth(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "/actual"}) if len(seen) == 1 else httpx.Response(200, content=b"data")

        with tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/media", "test-user", "test-secret", transport=httpx.MockTransport(handler)) as source:
            source.download("", Path(folder) / "video")
        self.assertIn("Authorization", seen[1].headers)

    def test_explicit_zero_port_is_a_different_origin(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={"location": "https://example.test:0/media"}) if len(seen) == 1 else httpx.Response(200, content=b"data")

        with tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/media", "test-user", "test-secret", transport=httpx.MockTransport(handler)) as source:
            source.download("", Path(folder) / "video")
        self.assertIn("Authorization", seen[0].headers)
        self.assertNotIn("Authorization", seen[1].headers)

    def test_https_downgrade_and_redirect_loop_rejected(self):
        for location in ("http://example.test/media", "https://example.test/media"):
            transport = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": location}))
            with tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/media", allow_http=True, transport=transport) as source:
                target = Path(folder) / "video"
                with self.assertRaises(CloudSubError):
                    source.download("", target)
                self.assertFalse(target.exists())

    def test_failure_does_not_leak_url_or_response_body(self):
        for transport in (
            httpx.MockTransport(lambda request: httpx.Response(401, content=b"private-body")),
            httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("private-url?password=secret"))),
        ):
            with tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/private?token=secret", transport=transport) as source:
                with self.assertRaises(CloudSubError) as raised:
                    source.download("", Path(folder) / "video")
                for private in ("secret", "example.test", "private"):
                    self.assertNotIn(private, str(raised.exception))

    def test_download_preserves_existing_target(self):
        with tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/video", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"new"))) as source:
            target = Path(folder) / "video"
            target.write_bytes(b"original")
            with self.assertRaises(CloudSubError):
                source.download("", target)
            self.assertEqual(target.read_bytes(), b"original")

    def test_download_checks_length_and_limit_and_removes_partial_file(self):
        for headers, content, limit in (({"content-length": "5"}, b"data", 100), ({}, b"large", 4), ({"content-length": "bad"}, b"data", 100)):
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as folder, HttpSource("https://example.test/video", transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, content=content))) as source, patch("cloudsub.sources.MAX_DOWNLOAD_BYTES", limit):
                target = Path(folder) / "video"
                with self.assertRaises(CloudSubError):
                    source.download("", target)
                self.assertFalse(target.exists())

    def test_webdav_base_and_encoded_names_and_success_props(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, content=b"data")
            return httpx.Response(207, content=listing(entry("/dav/", directory=True), entry("https://example.test/dav/%E4%B8%AD%20%23%25%2B.mp4"), entry("missing.mp4", status=404)))

        with WebDAVSource("https://example.test/dav", transport=httpx.MockTransport(handler)) as source, tempfile.TemporaryDirectory() as folder:
            files = list(source.files())
            self.assertEqual(files, [RemoteFile("中 #%+.mp4", 4)])
            source.download(files[0].path, Path(folder) / "video")
        self.assertEqual(str(seen[0].url), "https://example.test/dav/")
        self.assertEqual(seen[0].headers["Depth"], "1")
        self.assertEqual(seen[1].url.raw_path, b"/dav/%E4%B8%AD%20%23%25%2B.mp4")

    def test_recursive_listing_and_unknown_size(self):
        def handler(request):
            if request.url.path == "/dav/":
                data = listing(entry("/dav/", directory=True), entry("/dav/sub/", directory=True), entry("video.mp4", size=None))
            else:
                data = listing(entry("/dav/sub/", directory=True), entry("child.mp4"))
            return httpx.Response(207, content=data)

        with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(handler)) as source:
            self.assertEqual(list(source.files()), [RemoteFile("video.mp4")])
            self.assertEqual(list(source.files(recursive=True)), [RemoteFile("video.mp4"), RemoteFile("sub/child.mp4", 4)])

    def test_webdav_rejects_path_escape_external_urls_and_invalid_size(self):
        for href, size in (("/outside/video.mp4", "4"), ("https://evil.test/dav/video.mp4", "4"), ("/dav/%2e%2e/video.mp4", "4"), ("/dav/sub/../video.mp4", "4"), ("/dav/a%5Cb.mp4", "4"), ("/dav/video.mp4", "-1"), ("/dav/video.mp4", "bad")):
            with self.subTest(href=href), WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(lambda request: httpx.Response(207, content=listing(entry(href, size=size))))) as source:
                with self.assertRaises(CloudSubError):
                    list(source.files())

    def test_directory_properties_can_span_successful_propstats(self):
        requested = []

        def handler(request):
            requested.append(request.url.path)
            if request.url.path == "/dav/":
                body = listing('<d:response><d:href>/dav/sub/</d:href>'
                               '<d:propstat><d:prop><d:getcontentlength>0</d:getcontentlength></d:prop>'
                               '<d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                               '<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>'
                               '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
            else:
                body = listing(entry("/dav/sub/child.mp4"))
            return httpx.Response(207, content=body)

        with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(handler)) as source:
            self.assertEqual(list(source.files(recursive=True)), [RemoteFile("sub/child.mp4", 4)])
        self.assertEqual(requested, ["/dav/", "/dav/sub/"])

    def test_webdav_rejects_dangerous_xml_and_limits(self):
        for data in (b'<!DOCTYPE x [<!ENTITY a "secret">]><x/>', '<!DOCTYPE x [<!ENTITY a "secret">]><x/>'.encode("utf-16"), b"not XML", b"<wrong/>"):
            with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(lambda request: httpx.Response(207, content=data))) as source:
                with self.assertRaises(CloudSubError):
                    list(source.files())
        with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(lambda request: httpx.Response(207, content=listing(entry("one"), entry("two"))))) as source:
            with patch("cloudsub.sources.MAX_XML_BYTES", 1), self.assertRaises(CloudSubError):
                list(source.files())
            with patch("cloudsub.sources.MAX_ENTRIES", 1), self.assertRaises(CloudSubError):
                list(source.files())

    def test_webdav_writes_subtitles_only_and_never_follows_write_redirect(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201) if request.method == "PUT" else httpx.Response(200, content=b"subtitles")

        with WebDAVSource("https://example.test/dav/", "test-user", "test-secret", transport=httpx.MockTransport(handler)) as source:
            source.upload("folder/video.srt", b"subtitles")
            with self.assertRaises(CloudSubError):
                source.upload("video.mp4", b"not subtitles")
            with self.assertRaises(CloudSubError):
                source.upload("../video.srt", b"subtitles")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].method, "PUT")
        self.assertEqual(seen[0].headers["If-None-Match"], "*")
        self.assertEqual(seen[1].method, "GET")
        with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.test/dav/video.srt"}))) as source:
            with self.assertRaises(CloudSubError):
                source.upload("video.srt", b"subtitles")

    def test_webdav_upload_conflict_is_safe(self):
        with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(lambda request: httpx.Response(412, content=b"private resource"))) as source:
            with self.assertRaisesRegex(CloudSubError, "HTTP 412"):
                source.upload("video.srt", b"subtitles")

    def test_webdav_upload_existing_equal_content_is_idempotent(self):
        for existing in (b"subtitles", b"different"):
            def handler(request):
                return httpx.Response(412) if request.method == "PUT" else httpx.Response(200, content=existing)

            with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(handler)) as source:
                if existing == b"subtitles":
                    source.upload("video.srt", b"subtitles")
                else:
                    with self.assertRaisesRegex(CloudSubError, "different remote subtitle"):
                        source.upload("video.srt", b"subtitles")

    def test_webdav_upload_rejects_mismatch_and_downgraded_verification(self):
        for response in (httpx.Response(200, content=b"different"), httpx.Response(302, headers={"location": "http://example.test/video.srt"})):
            def handler(request):
                return httpx.Response(201) if request.method == "PUT" else response

            with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(handler)) as source:
                with self.assertRaises(CloudSubError):
                    source.upload("video.srt", b"subtitles")

    def test_webdav_upload_verifies_openlist_cdn_redirect_without_credentials(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.method == "PUT":
                return httpx.Response(201)
            if request.url.host == "example.test":
                return httpx.Response(302, headers={"location": "https://cdn.example.test/video.srt"})
            return httpx.Response(200, content=b"subtitles")

        with WebDAVSource("https://example.test/dav/", "test-user", "test-secret", transport=httpx.MockTransport(handler)) as source:
            source.upload("video.srt", b"subtitles")
        self.assertEqual(len(seen), 3)
        self.assertIn("Authorization", seen[1].headers)
        self.assertNotIn("Authorization", seen[2].headers)
        self.assertNotIn("Cookie", seen[2].headers)

    def test_webdav_upload_verification_is_bounded(self):
        def handler(request):
            return httpx.Response(201) if request.method == "PUT" else httpx.Response(200, content=b"too much data")

        with WebDAVSource("https://example.test/dav/", transport=httpx.MockTransport(handler)) as source, patch("cloudsub.sources.MAX_SUBTITLE_BYTES", 10):
            with self.assertRaisesRegex(CloudSubError, "verification exceeds"):
                source.upload("video.srt", b"subtitles")


if __name__ == "__main__":
    unittest.main()
