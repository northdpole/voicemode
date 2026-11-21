#!/usr/bin/env python3
"""Loopback OpenAI-style STT shim for VoiceMode.

POST /v1/audio/transcriptions -> PolyAI Dialog-RSN-1 (via the installed `dictate`
package and its Keychain key). On any PolyAI failure the original request is
forwarded to local Whisper, so voice never goes dark.

Safety: binds 127.0.0.1 only; fallback must be a loopback URL; the API key is
never logged; transcripts are never logged. Audio leaves this machine only on
the PolyAI path.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
from email import message_from_bytes, policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import dictate

HOST = "127.0.0.1"
PORT = int(os.environ.get("POLY_STT_PORT", "2023"))
FALLBACK = os.environ.get("POLY_STT_FALLBACK", "http://127.0.0.1:2022/v1/audio/transcriptions")
MAX_BODY = 25 * 1024 * 1024
POLY_TIMEOUT = 20

if urlparse(FALLBACK).hostname not in ("127.0.0.1", "localhost", "::1"):
    sys.exit("POLY_STT_FALLBACK must be a loopback URL.")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def parse_multipart(content_type: str, body: bytes) -> dict:
    msg = message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body, policy=policy.HTTP)
    out: dict = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            out[name] = (part.get_filename(), part.get_payload(decode=True))
    return out


def poly_transcribe(audio: bytes, filename: str | None) -> str:
    suffix = os.path.splitext(filename or "")[1][:8] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(audio)
        f.flush()
        pcm = dictate.read_audio(f.name)
    return asyncio.run(asyncio.wait_for(dictate.transcribe_pcm(pcm), POLY_TIMEOUT))


def whisper_forward(content_type: str, body: bytes) -> tuple[int, str, bytes]:
    req = urllib.request.Request(FALLBACK, data=body, headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.headers.get("Content-Type", "application/json"), r.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "poly-stt-adapter"

    def log_message(self, *a):  # no default access log
        pass

    def _send(self, code: int, ctype: str, data: bytes, engine: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if engine:
            self.send_header("X-STT-Engine", engine)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/"):
            self._send(200, "application/json", b'{"status":"ok","engine":"poly-adapter"}')
        elif self.path == "/v1/models":
            self._send(200, "application/json", b'{"object":"list","data":[{"id":"whisper-1","object":"model"}]}')
        else:
            self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self):
        if self.path.split("?")[0] != "/v1/audio/transcriptions":
            return self._send(404, "application/json", b'{"error":"not found"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if not 0 < n <= MAX_BODY:
            return self._send(413, "application/json", b'{"error":"bad or oversized body"}')
        ctype = self.headers.get("Content-Type", "")
        body = self.rfile.read(n)
        t0 = time.monotonic()
        try:
            fields = parse_multipart(ctype, body)
            filename, audio = fields["file"]
            fmt = (fields.get("response_format", (None, b"json"))[1] or b"json").decode()
            text = poly_transcribe(audio, filename)
            ms = int((time.monotonic() - t0) * 1000)
            log(f"engine=poly ms={ms} bytes={len(audio)} chars={len(text)}")
            if fmt == "text":
                return self._send(200, "text/plain; charset=utf-8", text.encode(), "poly")
            return self._send(200, "application/json", json.dumps({"text": text}).encode(), "poly")
        except (Exception, SystemExit) as e:  # SystemExit: dictate.resolve() exits when no key
            log(f"engine=poly FAILED ({type(e).__name__}); falling back to local whisper")
        try:
            code, ct, data = whisper_forward(ctype, body)
            log(f"engine=whisper-fallback ms={int((time.monotonic() - t0) * 1000)}")
            self._send(code, ct, data, "whisper-fallback")
        except Exception as e:
            log(f"engine=whisper-fallback FAILED ({type(e).__name__})")
            self._send(502, "application/json", b'{"error":"both engines failed"}')


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"poly-stt-adapter on http://{HOST}:{PORT} (fallback {FALLBACK})")
    srv.serve_forever()
