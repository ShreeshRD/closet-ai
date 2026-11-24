"""Continuous hands-free voice commands for capture (dictate audio + local ASR).

When the webcam is on, a background listener records the mic, transcribes short
speech segments with the same Bark/parakeet stack dictate uses, and pushes
`shoot` or `retake` the moment a command word is recognized. No hotkey.
`shoot` captures a frame and analyzes it in one step.
"""
from __future__ import annotations

import array
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("closet_ai.voice")

# ---------------------------------------------------------------------------
# Command matching — only `shoot` and `retake`
# ---------------------------------------------------------------------------

_SHOOT = re.compile(
    r"\b(shoot|snapped?|snap(?:ping|ped)?(?:\s+it)?|capture"
    r"|take\s+(?:a\s+)?(?:photo|picture|shot)|cheese"
    r"|got\s+(?:it|the\s+shot)|click)\b",
    re.I,
)

_RETAKE = re.compile(
    r"\b(retake|re-take|take\s+(?:a\s+)?again|again|redo|restart"
    r"|start\s+over|try\s+again|new\s+photo|another\s+photo"
    r"|discard|throw\s+it\s+away|delete\s+(?:the\s+)?photo)\b",
    re.I,
)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS // 10  # 100 ms

MAX_SEGMENT_S = 2.5
MAX_OPEN_S = 4.0
TAIL_S = 0.35
COMMAND_COOLDOWN_S = 1.2
IDLE_COOLDOWN_S = 0.4
SPEECH_RMS = 0.0015


def match_command(text: str) -> str | None:
    """Map a transcript to `shoot` or `retake`, or None if noise."""
    raw = str(text or "")
    cleaned = " ".join(raw.split()).lower().strip()
    cleaned = cleaned.strip(".,!?;:\"'`…")
    if not cleaned:
        return None

    words = re.findall(r"[a-z']+", cleaned)
    fillers = {
        "okay",
        "ok",
        "um",
        "uh",
        "ummm",
        "hey",
        "please",
        "now",
        "yeah",
        "yep",
        "so",
        "the",
        "a",
        "an",
        "it",
        "this",
        "that",
        "just",
        "do",
        "can",
        "will",
        "you",
        "me",
        "lets",
        "let's",
    }
    content = [w for w in words if w not in fillers]
    if not content:
        return None
    content_s = " ".join(content)
    compact = "".join(content)

    # Prefer retake when both appear
    if _RETAKE.search(content_s) or _RETAKE.search(cleaned):
        return "retake"
    if _SHOOT.search(content_s) or _SHOOT.search(cleaned):
        return "shoot"

    if len(compact) <= 14:
        if "retak" in compact or "again" in compact or compact in (
            "redo",
            "restart",
            "takeagain",
        ):
            return "retake"
        if compact in ("shoot", "shots", "shot", "chute", "cheese", "snap", "capture"):
            return "shoot"
        if compact.startswith("sho") and "t" in compact:
            return "shoot"
        if compact.startswith("ret") and "ak" in compact:
            return "retake"

    if len(content) == 1:
        w = content[0]
        if len(w) >= 3:
            if w in ("again", "redo", "restart", "retake"):
                return "retake"
            if w in ("shoot", "shot", "snap", "capture", "click", "cheese"):
                return "shoot"
            if w[0] == "r" and "tak" in w:
                return "retake"
            if w[0] == "s" and ("ho" in w or "nap" in w):
                return "shoot"
    return None


# ---------------------------------------------------------------------------
# Mic capture via PipeWire (same approach as dictate's ASR.audio)
# ---------------------------------------------------------------------------


def _pcm_stats(data: bytes) -> float:
    usable = len(data) - (len(data) % SAMPLE_WIDTH)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(data[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples)) / 32768.0


class _Mic:
    """Spawns `pw-cat --record` and yields 16 kHz mono s16le chunks."""

    def __init__(self, source: str = ""):
        import shutil
        import subprocess

        if shutil.which("pw-cat") is None:
            raise RuntimeError("pw-cat not found (need pipewire-audio)")
        command = [
            "pw-cat",
            "--record",
            "--raw",
            "--format",
            "s16",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            str(CHANNELS),
            "--latency",
            "20ms",
        ]
        if source:
            command.extend(["--target", source])
        command.append("-")
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
        time.sleep(0.2)
        if self._process.poll() is not None:
            err = (self._process.stderr.read() or b"").decode("utf-8", "replace")
            raise RuntimeError(err.strip() or "pw-cat exited immediately")

    def read_chunk(self) -> bytes:
        assert self._process.stdout is not None
        return self._process.stdout.read(CHUNK_BYTES)

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.5)
            except Exception:
                self._process.kill()
                try:
                    self._process.wait(timeout=1)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Local ASR (reuse dictate checkout when available; never touch GPU)
# ---------------------------------------------------------------------------


def _dictate_root() -> Path | None:
    env = os.environ.get("DICTATE_ROOT")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            Path.home() / "programs" / "personal_organization" / "VoiceAssistant" / "linux",
            Path("/home/arch/programs/personal_organization/VoiceAssistant/linux"),
            Path.home() / ".local" / "share" / "speech-flow" / "app" / "linux",
        ]
    )
    for root in candidates:
        if (root / "ASR" / "asr.py").is_file():
            return root
    return None


class LocalAsr:
    """Thin wrapper around dictate's asr.transcribe with a cached config."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._error = ""
        self._transcribe: Callable[..., str] | None = None
        self._config: Any = None

    def ensure(self) -> None:
        with self._lock:
            if self._ready:
                return
            root = _dictate_root()
            if root is None:
                self._error = "dictate ASR package not found"
                raise RuntimeError(self._error)
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            try:
                from ASR import asr as asr_mod
                from ASR import config as config_mod

                cfg = config_mod.Config()
                asr_mod.find_asr(cfg)
                self._transcribe = asr_mod.transcribe
                self._config = cfg
                self._ready = True
                self._error = ""
            except Exception as exc:
                self._error = str(exc)
                raise

    @property
    def error(self) -> str:
        return self._error

    def transcribe_file(self, path: Path, timeout: float = 8.0) -> str:
        self.ensure()
        assert self._transcribe is not None and self._config is not None
        return self._transcribe(path, self._config, timeout=timeout)


# ---------------------------------------------------------------------------
# Listener state machine
# ---------------------------------------------------------------------------


class VoiceListener:
    """Background mic → VAD segments → ASR → command events."""

    def __init__(
        self,
        *,
        transcribe: Callable[[Path], str] | None = None,
        mic_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_command: Callable[[str, str], None] | None = None,
    ):
        self._transcribe_fn = transcribe
        self._mic_factory = mic_factory or (
            lambda: _Mic(os.environ.get("CLOSET_AI_AUDIO_SOURCE", ""))
        )
        self._clock = clock
        self._on_command = on_command
        self._asr = LocalAsr()

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._error = ""
        self._state = "idle"  # idle | listening | thinking | error
        self._level = 0.0
        self._heard = ""
        self._last_command = ""
        self._last_command_at = 0.0
        self._events: deque[dict[str, Any]] = deque(maxlen=64)
        self._event_seq = 0
        self._consumed_seq = 0

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self._status_locked()
            self._stop.clear()
            self._error = ""
            self._state = "listening"
            self._heard = ""
            self._thread = threading.Thread(
                target=self._run, name="closet-voice", daemon=True
            )
            self._thread.start()
            return self._status_locked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop.set()
            thread = self._thread
            self._running = False
            self._state = "idle"
            self._error = ""
            self._level = 0.0
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        with self._lock:
            self._thread = None
            return self._status_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def pop_command(self) -> dict[str, Any]:
        with self._lock:
            if not self._events:
                return {
                    "ok": True,
                    "command": None,
                    "heard": self._heard,
                    "reason": "listening" if self._running else "idle",
                    "running": self._running,
                    "state": self._state,
                    "error": self._error,
                }
            for event in self._events:
                if event["seq"] > self._consumed_seq:
                    self._consumed_seq = event["seq"]
                    payload = dict(event)
                    payload.update(
                        {
                            "ok": True,
                            "running": self._running,
                            "state": self._state,
                            "error": self._error,
                        }
                    )
                    return payload
            return {
                "ok": True,
                "command": None,
                "heard": self._heard,
                "reason": "waiting",
                "running": self._running,
                "state": self._state,
                "error": self._error,
            }

    def reset_events(self) -> None:
        with self._lock:
            self._events.clear()
            self._event_seq = 0
            self._consumed_seq = 0

    def _status_locked(self) -> dict[str, Any]:
        return {
            "ok": not self._error,
            "running": self._running,
            "state": self._state,
            "error": self._error,
            "level": round(self._level, 4),
            "heard": self._heard,
            "last_command": self._last_command,
            "cooldown_left": max(
                0.0, COMMAND_COOLDOWN_S - (self._clock() - self._last_command_at)
            )
            if self._last_command
            else 0.0,
        }

    def _emit(self, command: str | None, heard: str, reason: str) -> None:
        self._event_seq += 1
        event = {
            "seq": self._event_seq,
            "command": command,
            "heard": heard,
            "reason": reason,
            "at": self._clock(),
        }
        self._events.append(event)
        LOG.info("voice event command=%s reason=%s heard=%r", command, reason, heard)
        if command:
            self._last_command = command
            self._last_command_at = event["at"]
            if self._on_command:
                try:
                    self._on_command(command, heard)
                except Exception:
                    pass

    def _transcribe(self, path: Path) -> str:
        if self._transcribe_fn is not None:
            return self._transcribe_fn(path)
        return self._asr.transcribe_file(path)

    def _run(self) -> None:
        with self._lock:
            self._running = True
        mic = None
        try:
            mic = self._mic_factory()
            buf = bytearray()
            speech_bytes = 0
            tail_bytes = 0
            in_speech = False
            open_since = 0.0
            min_speech = int(0.2 * SAMPLE_RATE * SAMPLE_WIDTH)
            tail_need = int(TAIL_S * SAMPLE_RATE * SAMPLE_WIDTH)

            while not self._stop.is_set():
                data = mic.read_chunk()
                if not data:
                    break
                rms = _pcm_stats(data)
                with self._lock:
                    self._level = 0.25 * rms + 0.75 * self._level

                now = self._clock()
                is_voice = rms >= SPEECH_RMS
                flush = False

                if is_voice:
                    if not in_speech:
                        in_speech = True
                        open_since = now
                        speech_bytes = 0
                        tail_bytes = 0
                    speech_bytes += len(data)
                    tail_bytes = 0
                    buf.extend(data)
                    speech_s = speech_bytes / (SAMPLE_RATE * SAMPLE_WIDTH)
                    open_s = now - open_since
                    if speech_s >= MAX_SEGMENT_S or open_s >= MAX_OPEN_S:
                        flush = True
                elif in_speech:
                    buf.extend(data)
                    tail_bytes += len(data)
                    if tail_bytes >= tail_need:
                        in_speech = False
                        flush = True
                else:
                    if len(buf) > CHUNK_BYTES:
                        del buf[: len(buf) - CHUNK_BYTES]

                if flush and speech_bytes >= min_speech:
                    pcm = bytes(buf)
                    buf.clear()
                    speech_bytes = 0
                    tail_bytes = 0
                    in_speech = False
                    open_since = 0.0
                    self._process_segment(pcm, now)

        except Exception as exc:
            LOG.exception("voice listener crashed")
            with self._lock:
                self._error = str(exc)
                self._state = "error"
        finally:
            if mic is not None:
                try:
                    mic.close()
                except Exception:
                    pass
            with self._lock:
                self._running = False
                if self._state != "error":
                    self._state = "idle"
                self._level = 0.0
                self._thread = None

    def _process_segment(self, pcm: bytes, started_at: float) -> None:
        with self._lock:
            if not self._running:
                return
            self._state = "thinking"

        path: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix="closet-voice-", suffix=".wav")
            os.close(fd)
            path = Path(name)
            with wave.open(str(path), "w") as w:
                w.setnchannels(CHANNELS)
                w.setsampwidth(SAMPLE_WIDTH)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm)
            text = self._transcribe(path).strip()
            LOG.info("voice asr text=%r", text)
        except Exception as exc:
            LOG.exception("voice ASR failed")
            with self._lock:
                self._error = f"ASR failed: {exc}"
                self._state = "listening" if self._running else "idle"
            return
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

        with self._lock:
            if not self._running:
                return
            if text:
                self._heard = text
            self._state = "listening"
            if not text:
                return
            command = match_command(text)
            now = self._clock()
            if not command:
                self._emit(None, text, "heard, but not a capture command")
                return
            if (
                command == self._last_command
                and self._last_command_at
                and (now - self._last_command_at) < COMMAND_COOLDOWN_S
            ):
                self._emit(None, text, f"{command} cooling down")
                return
            self._emit(command, text, "ok")


# ---------------------------------------------------------------------------
# Module-level singleton used by the FastAPI routes
# ---------------------------------------------------------------------------

_listener: VoiceListener | None = None
_listener_lock = threading.Lock()


def get_listener() -> VoiceListener:
    global _listener
    with _listener_lock:
        if _listener is None:
            _listener = VoiceListener()
        return _listener


def set_listener(listener: VoiceListener | None) -> None:
    global _listener
    with _listener_lock:
        if _listener is not None and _listener is not listener:
            try:
                _listener.stop()
            except Exception:
                pass
        _listener = listener


def start_listening() -> dict[str, Any]:
    return get_listener().start()


def stop_listening() -> dict[str, Any]:
    return get_listener().stop()


def listening_status() -> dict[str, Any]:
    return get_listener().status()


def pop_command() -> dict[str, Any]:
    return get_listener().pop_command()


def reset_runtime_state() -> None:
    set_listener(None)
