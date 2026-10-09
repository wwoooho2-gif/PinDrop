from __future__ import annotations

import hashlib
import html.parser
import io
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


HOST = "127.0.0.1"
PORT = 8765
MAX_URLS = 20
MAX_REQUEST_BYTES = 64 * 1024
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_MEDIA_BYTES = 200 * 1024 * 1024
MAX_BATCH_BYTES = 200 * 1024 * 1024
MAX_GIF_BYTES = 100 * 1024 * 1024
MAX_GIF_DURATION_SECONDS = 15
GIF_MAX_WIDTH = 720
GIF_FRAME_RATE = 25
PIN_DOMAINS = ("pinterest.com", "pin.it")
MEDIA_DOMAINS = ("pinimg.com", "pinterest.com")
IMAGE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
VIDEO_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
}
MEDIA_FILE_EXTENSIONS = {
    ".avif",
    ".gif",
    ".jpg",
    ".m4v",
    ".mov",
    ".mp4",
    ".png",
    ".webm",
    ".webp",
}


class _PinMediaParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image_url: Optional[str] = None
        self.video_urls: List[str] = []
        self.script_text: List[str] = []
        self._in_script = False

    def handle_starttag(
        self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]
    ) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        tag = tag.lower()
        if tag == "meta":
            property_name = values.get("property", "").lower()
            name = values.get("name", "").lower()
            if self.image_url is None and (
                property_name == "og:image"
                or name in {"twitter:image", "twitter:image:src"}
            ):
                content = values.get("content", "").strip()
                if content:
                    self.image_url = content
            if (
                property_name in {"og:video", "og:video:url", "og:video:secure_url"}
                or name == "twitter:player:stream"
                or values.get("itemprop", "").lower() == "contenturl"
            ):
                content = values.get("content", "").strip()
                if content:
                    self.video_urls.append(content)
        elif tag in {"video", "source"}:
            source = values.get("src", "").strip()
            if source:
                self.video_urls.append(source)
        elif tag == "script":
            self._in_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.script_text.append(data)


def _valid_https_url(url: str, allowed_domains: Sequence[str]) -> urllib.parse.ParseResult:
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("That URL is not valid.") from exc

    allowed_host = any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in allowed_domains
    )
    if (
        parsed.scheme.lower() != "https"
        or not allowed_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("Use a public Pinterest pin link.")
    return parsed


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_domains: Sequence[str]) -> None:
        super().__init__()
        self.allowed_domains = allowed_domains

    def redirect_request(self, req, fp, code, msg, headers, new_url):
        _valid_https_url(new_url, self.allowed_domains)
        return super().redirect_request(req, fp, code, msg, headers, new_url)


def _fetch(
    url: str, allowed_domains: Sequence[str], max_bytes: int
) -> Tuple[bytes, str]:
    _valid_https_url(url, allowed_domains)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PinterestDownloader/1.0 (public pin media)",
            "Accept": "text/html,image/avif,image/webp,image/png,image/jpeg,image/gif,video/mp4,video/webm,*/*;q=0.5",
        },
    )
    opener = urllib.request.build_opener(
        _RestrictedRedirectHandler(allowed_domains)
    )
    with opener.open(request, timeout=20) as response:
        final_url = response.geturl()
        _valid_https_url(final_url, allowed_domains)
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("The response is too large to download.")
        return data, response.headers.get_content_type().lower()


def _image_url_from_page(page: bytes) -> Optional[str]:
    parser = _PinMediaParser()
    parser.feed(page.decode("utf-8", errors="replace"))
    return parser.image_url


def _video_url_from_page(page: bytes) -> Optional[str]:
    parser = _PinMediaParser()
    parser.feed(page.decode("utf-8", errors="replace"))
    candidates = list(parser.video_urls)

    for script in parser.script_text:
        normalized = (
            script.replace(r"\/", "/")
            .replace(r"\u002F", "/")
            .replace(r"\u002f", "/")
            .replace(r"\u0026", "&")
        )
        candidates.extend(
            re.findall(r"https?://[^\"'\\\s<>]+", normalized)
        )

    valid_candidates = []
    for candidate in candidates:
        candidate = candidate.strip().rstrip("),;")
        absolute_candidate = urllib.parse.urljoin("https://www.pinterest.com/", candidate)
        try:
            parsed = _valid_https_url(absolute_candidate, MEDIA_DOMAINS)
        except ValueError:
            continue
        if Path(parsed.path).suffix.lower() in {
            ".m4v",
            ".mov",
            ".mp4",
            ".webm",
        }:
            valid_candidates.append(absolute_candidate)

    def resolution(url: str) -> int:
        match = re.search(
            r"(?:^|[/_-])(\d{3,4})(?:p|w)(?:[/_.?]|$)", url.lower()
        )
        return int(match.group(1)) if match else 0

    if not valid_candidates:
        return None
    return max(valid_candidates, key=resolution)


def _media_extension(content_type: str, media_url: str, is_video: bool) -> Optional[str]:
    extensions = VIDEO_EXTENSIONS if is_video else IMAGE_EXTENSIONS
    extension = extensions.get(content_type)
    if extension:
        return extension

    guessed_extension = Path(urllib.parse.urlparse(media_url).path).suffix.lower()
    if content_type in {"application/octet-stream", "binary/octet-stream"}:
        if is_video and guessed_extension in {".m4v", ".mov", ".mp4", ".webm"}:
            return guessed_extension
        return None

    expected_type = "video/" if is_video else "image/"
    if not content_type.startswith(expected_type):
        return None
    guessed_content_type = mimetypes.guess_type("file" + guessed_extension)[0]
    if (
        guessed_extension in MEDIA_FILE_EXTENSIONS
        and guessed_content_type is not None
        and guessed_content_type.startswith(expected_type)
    ):
        return guessed_extension
    return None


def _convert_video_to_gif(media_data: bytes, filename: str) -> Tuple[bytes, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError(
            "GIF conversion requires FFmpeg. Install FFmpeg and restart PinDrop."
        )

    with tempfile.TemporaryDirectory(prefix="pindrop-gif-") as temporary_directory:
        source = Path(temporary_directory) / filename
        destination = Path(temporary_directory) / "converted.gif"
        source.write_bytes(media_data)
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-t",
                    str(MAX_GIF_DURATION_SECONDS),
                    "-filter_complex",
                    f"[0:v]fps={GIF_FRAME_RATE},"
                    f"scale='min({GIF_MAX_WIDTH},iw)':-1:flags=lanczos,split[a][b];"
                    "[a]palettegen=stats_mode=full[p];"
                    "[b][p]paletteuse=dither=sierra2_4a",
                    "-loop",
                    "0",
                    "-fs",
                    str(MAX_GIF_BYTES),
                    str(destination),
                ],
                capture_output=True,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("GIF conversion timed out. Try a shorter video.") from exc

        if result.returncode != 0 or not destination.is_file():
            error = result.stderr.decode("utf-8", errors="replace").strip()
            detail = error.splitlines()[-1] if error else "FFmpeg could not convert it."
            raise ValueError(f"Could not convert this video to GIF: {detail}")
        if destination.stat().st_size >= MAX_GIF_BYTES:
            raise ValueError("The converted GIF exceeds the 100 MB limit.")
        return destination.read_bytes(), filename.rsplit(".", 1)[0] + ".gif"


def download_pin(pin_url: str, convert_video_to_gif: bool = False) -> Dict[str, object]:
    pin_url = pin_url.strip()
    if len(pin_url) > 2048:
        raise ValueError("That pin link is too long.")
    parsed_pin = _valid_https_url(pin_url, PIN_DOMAINS)
    if not parsed_pin.path.strip("/"):
        raise ValueError("Paste a link to a pin, not a Pinterest homepage.")

    page, _ = _fetch(pin_url, PIN_DOMAINS, MAX_PAGE_BYTES)
    video_url = _video_url_from_page(page)
    is_video = video_url is not None
    media_url = video_url or _image_url_from_page(page)
    if not media_url:
        raise ValueError("No public image or video was found for that pin.")

    absolute_media_url = urllib.parse.urljoin(pin_url, media_url)
    _valid_https_url(absolute_media_url, MEDIA_DOMAINS)
    media_data, content_type = _fetch(
        absolute_media_url, MEDIA_DOMAINS, MAX_MEDIA_BYTES
    )
    extension = _media_extension(content_type, absolute_media_url, is_video)
    if not extension:
        raise ValueError("Pinterest did not return a supported image or video.")

    filename = hashlib.sha256(pin_url.encode("utf-8")).hexdigest()[:16] + extension
    media_type = "video" if is_video else "image"
    if convert_video_to_gif and is_video:
        media_data, filename = _convert_video_to_gif(media_data, filename)
        content_type = "image/gif"
        media_type = "gif"
    return {
        "filename": filename,
        "media_type": media_type,
        "content_type": content_type,
        "data": media_data,
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <title>PinDrop - Pinterest image downloader</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
      background: radial-gradient(ellipse at 50% 0, #4a1826 0, #171315 45%, #111 100%); color: #f7f1f2; }
    main { width: min(100%, 680px); padding: clamp(24px, 6vw, 48px); border: 1px solid #ffffff1c;
      border-radius: 24px; background: #1b1819e8; box-shadow: 0 24px 80px #0008; }
    .brand { color: #ff7088; font-size: .8rem; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; }
    h1 { margin: 12px 0 8px; font-size: clamp(2rem, 7vw, 3.25rem); letter-spacing: -.05em; }
    p { color: #bfb5b8; line-height: 1.6; }
    label { display: block; margin: 28px 0 9px; font-weight: 700; }
    textarea { display: block; width: 100%; min-height: 150px; resize: vertical; padding: 15px;
      color: #f7f1f2; background: #111; border: 1px solid #51474a; border-radius: 12px;
      font: 500 .95rem/1.6 ui-monospace, monospace; }
    textarea:focus { outline: 2px solid #ff7088; border-color: transparent; }
    button { margin-top: 14px; padding: 13px 20px; border: 0; border-radius: 10px;
      background: #df3858; color: white; font: inherit; font-weight: 800; cursor: pointer; }
    button:hover { background: #f04b6b; }
    button:disabled { opacity: .6; cursor: wait; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; }
    #status { min-height: 24px; margin: 18px 0 8px; }
    ul { list-style: none; margin: 0; padding: 0; }
    li { display: flex; justify-content: space-between; gap: 12px; align-items: center;
      margin-top: 8px; padding: 12px; border-radius: 10px; background: #ffffff0b; overflow-wrap: anywhere; }
    li a { color: #ff91a4; font-weight: 700; white-space: nowrap; }
    .note { margin-top: 24px; padding-top: 16px; border-top: 1px solid #ffffff1c; font-size: .82rem; }
  </style>
</head>
<body>
  <main>
    <div class="brand">PinDrop</div>
    <h1>Save the pins you love.</h1>
    <p>Paste public Pinterest pin links below, one per line. Images and videos download straight to your browser.</p>
    <form id="form">
      <label for="links">Pinterest links</label>
      <textarea id="links" placeholder="https://www.pinterest.com/pin/…&#10;https://pin.it/…" required></textarea>
      <div class="actions">
        <button id="download" type="submit">Download to browser</button>
        <button id="download-gif" type="submit">Download as GIF</button>
      </div>
    </form>
    <p id="status" role="status" aria-live="polite"></p>
    <ul id="results"></ul>
    <p class="note">“Download as GIF” converts the first 15 seconds of videos at 25 fps and up to 720 pixels wide; requires FFmpeg. Photos download normally. Multiple pins are saved together as a ZIP.</p>
  </main>
  <script>
    const form = document.querySelector("#form");
    const buttons = [...form.querySelectorAll('button[type="submit"]')];
    const status = document.querySelector("#status");
    const results = document.querySelector("#results");
    const linksInput = document.querySelector("#links");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const convertToGif = event.submitter?.id === "download-gif";
      buttons.forEach(button => button.disabled = true);
      results.replaceChildren();
      status.textContent = convertToGif ? "Converting videos to GIF and downloading…" : "Downloading…";
      const linksText = linksInput.value;
      try {
        const response = await fetch("/download", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            links: linksText,
            convert_videos_to_gif: convertToGif
          })
        });
        if (!response.ok) {
          const data = await response.json();
          throw new Error(data.error || "The request could not be completed.");
        }
        const file = await response.blob();
        const disposition = response.headers.get("Content-Disposition") || "";
        const filename = disposition.match(/filename="([^"]+)"/)?.[1] || "pindrop-download";
        const link = document.createElement("a");
        const objectUrl = URL.createObjectURL(file);
        link.href = objectUrl;
        link.download = filename;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        linksInput.value = "";

        const types = (response.headers.get("X-PinDrop-Types") || "").split(",").filter(Boolean);
        const videos = types.filter(type => type === "video").length;
        const photos = types.filter(type => type === "image").length;
        const gifs = types.filter(type => type === "gif").length;
        const downloaded = [];
        if (videos) downloaded.push(`${videos} video${videos === 1 ? "" : "s"}`);
        if (photos) downloaded.push(`${photos} photo${photos === 1 ? "" : "s"}`);
        if (gifs) downloaded.push(`${gifs} GIF${gifs === 1 ? "" : "s"}`);
        const errors = Number(response.headers.get("X-PinDrop-Errors") || 0);
        status.textContent = `Browser download started: ${downloaded.join(", ")}. Links cleared.${errors ? ` ${errors} link(s) could not be downloaded; see download-errors.txt in the ZIP.` : ""}`;
      } catch (error) {
        status.textContent = error.message;
      } finally {
        buttons.forEach(button => button.disabled = false);
      }
    });
  </script>
</body>
</html>
"""

FAVICON = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="18" fill="#df3858"/>
  <path fill="#fff" d="M32 10c-12 0-21 8-21 20 0 8 4 14 11 16l2-8c-2-2-3-5-3-8 0-7 5-13 12-13 7 0 11 5 11 12 0 8-3 14-8 14-3 0-5-3-4-6l3-12s-1-4-5-4c-5 0-8 5-8 10 0 3 1 5 2 7l-4 16c-1 4 0 9 0 9s5-6 6-10l2-8c1 2 4 4 8 4 10 0 17-9 17-21 0-10-8-18-21-18z" transform="translate(2 0) scale(.94)"/>
</svg>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "PinDrop/1.0"

    def do_GET(self) -> None:
        if self.path == "/favicon.svg":
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(FAVICON)
            return

        if self.path == "/":
            page = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/download":
            self.send_error(404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid request size."})
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(400, {"error": "Request must contain up to 20 pin links."})
            return

        try:
            request_data = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "Request body must be valid JSON."})
            return
        if not isinstance(request_data, dict) or not isinstance(
            request_data.get("links"), str
        ):
            self._send_json(400, {"error": "Provide pin links as text."})
            return
        convert_video_to_gif = request_data.get("convert_videos_to_gif", False)
        if not isinstance(convert_video_to_gif, bool):
            self._send_json(400, {"error": "GIF conversion option must be true or false."})
            return

        links = [line.strip() for line in request_data["links"].splitlines() if line.strip()]
        if not links or len(links) > MAX_URLS:
            self._send_json(400, {"error": "Paste between 1 and 20 pin links."})
            return

        results: List[Dict[str, object]] = []
        for link in links:
            try:
                results.append(
                    {
                        "input": link,
                        **download_pin(link, convert_video_to_gif),
                    }
                )
            except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
                results.append({"input": link, "error": str(exc)})

        successful_results = [result for result in results if "data" in result]
        failures = [result for result in results if "error" in result]
        if not successful_results:
            error_details = "\n".join(
                f'{result["input"]}: {result["error"]}' for result in failures
            )
            self._send_json(
                422,
                {
                    "error": error_details
                    or "No public image or video could be downloaded.",
                    "results": results,
                },
            )
            return

        media_types = ",".join(
            str(result["media_type"]) for result in successful_results
        )
        errors_count = str(len(failures))
        if len(links) == 1:
            result = successful_results[0]
            body = result["data"]
            if not isinstance(body, bytes):
                raise TypeError("Downloaded media payload must be bytes.")
            self._send_attachment(
                body,
                str(result["content_type"]),
                str(result["filename"]),
                media_types,
                errors_count,
            )
            return

        total_size = sum(len(result["data"]) for result in successful_results)
        if total_size > MAX_BATCH_BYTES:
            self._send_json(
                413,
                {"error": "The combined download exceeds the 200 MB ZIP limit. Try fewer pins."},
            )
            return

        archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        try:
            with zipfile.ZipFile(
                archive, mode="w", compression=zipfile.ZIP_STORED
            ) as bundle:
                for result in successful_results:
                    body = result["data"]
                    if not isinstance(body, bytes):
                        raise TypeError("Downloaded media payload must be bytes.")
                    bundle.writestr(str(result["filename"]), body)
                if failures:
                    report = "\n".join(
                        f'{result["input"]}: {result["error"]}' for result in failures
                    )
                    bundle.writestr("download-errors.txt", report)
            archive.seek(0, io.SEEK_END)
            archive_size = archive.tell()
            archive.seek(0)
            self._send_attachment_headers(
                "application/zip",
                "pindrop-downloads.zip",
                archive_size,
                media_types,
                errors_count,
            )
            while chunk := archive.read(64 * 1024):
                self.wfile.write(chunk)
        finally:
            archive.close()

    def _send_attachment(
        self,
        body: bytes,
        content_type: str,
        filename: str,
        media_types: str,
        errors_count: str,
    ) -> None:
        self._send_attachment_headers(
            content_type, filename, len(body), media_types, errors_count
        )
        self.wfile.write(body)

    def _send_attachment_headers(
        self,
        content_type: str,
        filename: str,
        content_length: int,
        media_types: str,
        errors_count: str,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(content_length))
        self.send_header("X-PinDrop-Types", media_types)
        self.send_header("X-PinDrop-Errors", errors_count)
        self.end_headers()

    def _send_json(self, status: int, data: Dict[str, object]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    server.daemon_threads = True
    url = f"http://{HOST}:{PORT}"
    print(f"PinDrop is running at {url} (Ctrl+C to stop)")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping PinDrop.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
