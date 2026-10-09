import unittest
import json
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
import io
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from unittest.mock import patch, call

import app


class PinterestUrlTests(unittest.TestCase):
    def test_accepts_pinterest_and_short_pin_domains(self):
        for url in (
            "https://www.pinterest.com/pin/123/",
            "https://pin.it/abc123",
        ):
            with self.subTest(url=url):
                self.assertTrue(app._valid_https_url(url, app.PIN_DOMAINS))

    def test_rejects_non_pinterest_or_non_https_urls(self):
        for url in (
            "http://www.pinterest.com/pin/123/",
            "https://pinterest.com.example.org/pin/123/",
            "https://example.org/pin/123/",
            "https://user@pinterest.com/pin/123/",
            "https://pinterest.com:444/pin/123/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    app._valid_https_url(url, app.PIN_DOMAINS)

    def test_reads_open_graph_image_url(self):
        page = b'<meta property="og:image" content="https://i.pinimg.com/image.jpg">'
        self.assertEqual(
            app._image_url_from_page(page),
            "https://i.pinimg.com/image.jpg",
        )

    def test_twitter_image_is_used_when_open_graph_is_missing(self):
        page = b'<meta name="twitter:image" content="https://i.pinimg.com/image.webp">'
        self.assertEqual(
            app._image_url_from_page(page),
            "https://i.pinimg.com/image.webp",
        )

    def test_twitter_image_src_is_used_when_open_graph_is_missing(self):
        page = b'<meta name="twitter:image:src" content="https://i.pinimg.com/image.webp">'
        self.assertEqual(
            app._image_url_from_page(page),
            "https://i.pinimg.com/image.webp",
        )

    def test_escapes_and_finds_image_metadata(self):
        page = (
            b'<meta property="og:image" content="https://i.pinimg.com/a&amp;b.jpg">'
        )
        self.assertEqual(
            app._image_url_from_page(page),
            "https://i.pinimg.com/a&b.jpg",
        )

    def test_rejects_pages_without_image_metadata(self):
        self.assertIsNone(app._image_url_from_page(b"<html><title>Pin</title></html>"))

    def test_reads_video_from_open_graph_metadata(self):
        page = b'<meta property="og:video" content="https://v1.pinimg.com/videos/mc/720p/clip.mp4">'
        self.assertEqual(
            app._video_url_from_page(page),
            "https://v1.pinimg.com/videos/mc/720p/clip.mp4",
        )

    def test_reads_best_video_resolution_from_pin_script(self):
        page = (
            b'<script>{"video":"https:\\/\\/v1.pinimg.com\\/videos\\/mc\\/360p\\/low.mp4",'
            b'"alternate":"https:\\/\\/v1.pinimg.com\\/videos\\/mc\\/720p\\/high.mp4"}</script>'
        )
        self.assertEqual(
            app._video_url_from_page(page),
            "https://v1.pinimg.com/videos/mc/720p/high.mp4",
        )

    def test_prefers_highest_direct_video_and_skips_hls_playlist(self):
        page = (
            b'<script>{"playlist":"https://v1.pinimg.com/videos/stream.m3u8",'
            b'"low":"https://v1.pinimg.com/videos/clip_360w.mp4",'
            b'"high":"https://v1.pinimg.com/videos/clip_720w.mp4"}</script>'
        )
        self.assertEqual(
            app._video_url_from_page(page),
            "https://v1.pinimg.com/videos/clip_720w.mp4",
        )

    def test_ignores_untrusted_video_urls(self):
        page = b'<meta property="og:video" content="https://example.org/videos/clip.mp4">'
        self.assertIsNone(app._video_url_from_page(page))


class DownloadTests(unittest.TestCase):
    def test_downloads_image_from_public_pin_metadata(self):
        pin = "https://www.pinterest.com/pin/123/"
        image_url = "https://i.pinimg.com/originals/example.png"
        with patch.object(
            app,
            "_fetch",
            side_effect=[
                (b'<meta property="og:image" content="' + image_url.encode() + b'">', "text/html"),
                (b"\x89PNG\r\n\x1a\nimage", "image/png"),
            ],
        ):
            result = app.download_pin(pin)

        self.assertTrue(result["filename"].endswith(".png"))
        self.assertEqual(result["media_type"], "image")
        self.assertEqual(result["data"], b"\x89PNG\r\n\x1a\nimage")

    def test_downloads_video_from_public_pin_metadata(self):
        pin = "https://www.pinterest.com/pin/456/"
        video_url = "https://v1.pinimg.com/videos/mc/720p/clip.mp4"
        with patch.object(
            app,
            "_fetch",
            side_effect=[
                (
                    b'<meta property="og:video" content="'
                    + video_url.encode()
                    + b'">',
                    "text/html",
                ),
                (b"\x00\x00\x00\x18ftypmp42video", "video/mp4"),
            ],
        ):
            result = app.download_pin(pin)

        self.assertTrue(result["filename"].endswith(".mp4"))
        self.assertEqual(result["media_type"], "video")
        self.assertEqual(result["data"], b"\x00\x00\x00\x18ftypmp42video")

    def test_rejects_pin_without_image(self):
        with patch.object(app, "_fetch", return_value=(b"<html></html>", "text/html")):
            with self.assertRaisesRegex(ValueError, "No public image"):
                app.download_pin("https://www.pinterest.com/pin/123/")

    def test_rejects_non_image_response(self):
        pin = "https://www.pinterest.com/pin/123/"
        image_url = "https://i.pinimg.com/originals/example.png"
        with patch.object(
            app,
            "_fetch",
            side_effect=[
                (b'<meta property="og:image" content="' + image_url.encode() + b'">', "text/html"),
                (b"<html>blocked</html>", "text/html"),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "supported image or video"):
                app.download_pin(pin)

    def test_converts_video_to_gif_when_requested(self):
        pin = "https://www.pinterest.com/pin/789/"
        video_url = "https://v1.pinimg.com/videos/mc/720p/clip.mp4"
        with patch.object(
            app,
            "_fetch",
            side_effect=[
                (
                    b'<meta property="og:video" content="'
                    + video_url.encode()
                    + b'">',
                    "text/html",
                ),
                (b"video bytes", "video/mp4"),
            ],
        ), patch.object(
            app,
            "_convert_video_to_gif",
            return_value=(b"GIF89a", "converted.gif"),
        ) as convert:
            result = app.download_pin(pin, convert_video_to_gif=True)

        expected_video_filename = (
            app.hashlib.sha256(pin.encode("utf-8")).hexdigest()[:16] + ".mp4"
        )
        convert.assert_called_once_with(b"video bytes", expected_video_filename)
        self.assertEqual(result["filename"], "converted.gif")
        self.assertEqual(result["media_type"], "gif")
        self.assertEqual(result["content_type"], "image/gif")
        self.assertEqual(result["data"], b"GIF89a")

    def test_does_not_convert_photo_when_gif_option_is_enabled(self):
        pin = "https://www.pinterest.com/pin/123/"
        image_url = "https://i.pinimg.com/originals/example.png"
        with patch.object(
            app,
            "_fetch",
            side_effect=[
                (b'<meta property="og:image" content="' + image_url.encode() + b'">', "text/html"),
                (b"photo bytes", "image/png"),
            ],
        ), patch.object(app, "_convert_video_to_gif") as convert:
            result = app.download_pin(pin, convert_video_to_gif=True)

        convert.assert_not_called()
        self.assertEqual(result["media_type"], "image")
        self.assertEqual(result["filename"].endswith(".png"), True)


class GifConversionTests(unittest.TestCase):
    def test_uses_ffmpeg_and_returns_gif_data(self):
        def write_gif(command, **kwargs):
            Path(command[-1]).write_bytes(b"GIF89a")
            return SimpleNamespace(returncode=0, stderr=b"")

        with patch.object(app.shutil, "which", return_value="/usr/bin/ffmpeg"), patch.object(
            app.subprocess, "run", side_effect=write_gif
        ) as run:
            gif_data, filename = app._convert_video_to_gif(b"video", "pin.mp4")

        self.assertEqual(gif_data, b"GIF89a")
        self.assertEqual(filename, "pin.gif")
        command = run.call_args.args[0]
        self.assertIn(str(app.MAX_GIF_DURATION_SECONDS), command)
        self.assertIn(str(app.MAX_GIF_BYTES), command)
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn(f"fps={app.GIF_FRAME_RATE}", filter_graph)
        self.assertIn(f"min({app.GIF_MAX_WIDTH},iw)", filter_graph)
        self.assertIn("palettegen=stats_mode=full", filter_graph)
        self.assertIn("paletteuse=dither=sierra2_4a", filter_graph)

    def test_reports_missing_ffmpeg(self):
        with patch.object(app.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "requires FFmpeg"):
                app._convert_video_to_gif(b"video", "pin.mp4")


class BrowserDownloadTests(unittest.TestCase):
    def _post_download(self, links, downloads, convert_videos_to_gif=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app._Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/download",
            data=json.dumps(
                {
                    "links": "\n".join(links),
                    "convert_videos_to_gif": convert_videos_to_gif,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with patch.object(app, "download_pin", side_effect=downloads):
                try:
                    response = urllib.request.urlopen(request, timeout=5)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return (
                        response.status,
                        {
                            key.lower(): value
                            for key, value in response.headers.items()
                        },
                        response.read(),
                    )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_serves_tab_icon(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app._Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/favicon.svg",
                timeout=5,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
                self.assertIn(b"<svg", response.read())
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/",
                timeout=5,
            ) as response:
                self.assertIn(b'href="/favicon.svg"', response.read())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_single_pin_is_returned_as_browser_attachment(self):
        item = {
            "filename": "pindrop-test.mp4",
            "media_type": "video",
            "content_type": "video/mp4",
            "data": b"video bytes",
        }
        status, headers, body = self._post_download(
            ["https://www.pinterest.com/pin/123/"], [item]
        )
        self.assertEqual(status, 200)
        self.assertIn('attachment; filename="pindrop-test.mp4"', headers["content-disposition"])
        self.assertEqual(headers["x-pindrop-types"], "video")
        self.assertEqual(body, b"video bytes")

    def test_multiple_pins_are_returned_as_browser_zip(self):
        downloads = [
            {
                "filename": "first.jpg",
                "media_type": "image",
                "content_type": "image/jpeg",
                "data": b"photo bytes",
            },
            {
                "filename": "second.mp4",
                "media_type": "video",
                "content_type": "video/mp4",
                "data": b"video bytes",
            },
        ]
        status, headers, body = self._post_download(
            [
                "https://www.pinterest.com/pin/123/",
                "https://www.pinterest.com/pin/456/",
            ],
            downloads,
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment; filename=\"pindrop-downloads.zip\"", headers["content-disposition"])
        self.assertEqual(headers["x-pindrop-types"], "image,video")
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(set(archive.namelist()), {"first.jpg", "second.mp4"})
            self.assertEqual(archive.read("first.jpg"), b"photo bytes")
            self.assertEqual(archive.read("second.mp4"), b"video bytes")

    def test_gif_option_is_passed_to_pin_download(self):
        item = {
            "filename": "pindrop.gif",
            "media_type": "gif",
            "content_type": "image/gif",
            "data": b"GIF89a",
        }
        pin = "https://www.pinterest.com/pin/123/"
        server = ThreadingHTTPServer(("127.0.0.1", 0), app._Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/download",
            data=json.dumps(
                {"links": pin, "convert_videos_to_gif": True}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with patch.object(app, "download_pin", return_value=item) as download:
                response = urllib.request.urlopen(request, timeout=5)
                with response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"GIF89a")
            download.assert_called_once_with(pin, True)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
