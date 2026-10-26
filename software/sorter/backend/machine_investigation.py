"""Local, bounded evidence for investigating a sorting run.

The video writer subscribes to the existing classification camera capture. It
resizes and compresses frames on its own thread. An indexed MJPEG frame stream
is used instead of a fixed-frame-rate MP4: a frame's capture time remains exact
even when the camera stalls or the recorder drops frames.
"""

from __future__ import annotations

import contextvars
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import db

_piece_uuid: contextvars.ContextVar[str | None] = contextvars.ContextVar("investigation_piece", default=None)
_recorder: "InvestigationRecorder | None" = None
_MAX_BYTES_DEFAULT = 8 * 1024**3
_SEGMENT_SECONDS = 5 * 60
_SEGMENT_BYTES = 512 * 1024**2
_VIDEO_MAX_WIDTH = 960
_VIDEO_MAX_HEIGHT = 540
_VIDEO_JPEG_QUALITY = 65


def investigation_dir() -> Path:
    return db.local_state_db_path().parent / "machine_investigation"


def _tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_segments ("
        "id TEXT PRIMARY KEY, run_id TEXT, role TEXT NOT NULL, started_at REAL NOT NULL, "
        "ended_at REAL, file_path TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, "
        "frames INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0, "
        "status TEXT NOT NULL DEFAULT 'recording')"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investigation_segments_time ON investigation_segments(started_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_frames ("
        "segment_id TEXT NOT NULL, seq INTEGER NOT NULL, captured_at REAL NOT NULL, "
        "offset_bytes INTEGER NOT NULL, length_bytes INTEGER NOT NULL, "
        "PRIMARY KEY(segment_id, seq))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investigation_frames_time ON investigation_frames(captured_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, piece_uuid TEXT, kind TEXT NOT NULL, "
        "at REAL NOT NULL, mono REAL, data_json TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investigation_events_time ON investigation_events(at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investigation_events_piece ON investigation_events(piece_uuid, at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_requests ("
        "id TEXT PRIMARY KEY, piece_uuid TEXT NOT NULL, run_id TEXT, label TEXT, "
        "started_at REAL NOT NULL, ended_at REAL NOT NULL, status_code INTEGER, "
        "error TEXT, images_json TEXT NOT NULL, response_path TEXT, response_json TEXT, "
        "bytes INTEGER NOT NULL DEFAULT 0, evicted_at REAL)"
    )
    # Early installations of this table predate storage accounting. CREATE
    # TABLE IF NOT EXISTS does not add columns to those databases, so even the
    # recording-health endpoint would fail before a new request was written.
    previous_columns = {row[1] for row in conn.execute("PRAGMA table_info(investigation_requests)")}
    db.add_columns(conn, "investigation_requests", {
        "bytes": "INTEGER NOT NULL DEFAULT 0",
        "evicted_at": "REAL",
    })
    if "bytes" not in previous_columns:
        root = investigation_dir()
        rows = conn.execute("SELECT id,images_json,response_path FROM investigation_requests").fetchall()
        for row in rows:
            try:
                images = json.loads(row["images_json"])
            except (TypeError, ValueError):
                images = []
            paths = [item.get("path") if isinstance(item, dict) else item for item in images]
            paths.append(row["response_path"])
            used = 0
            for relative in paths:
                if not isinstance(relative, str):
                    continue
                try:
                    used += (root / relative).stat().st_size
                except OSError:
                    pass
            conn.execute("UPDATE investigation_requests SET bytes=? WHERE id=?", (used, row["id"]))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_investigation_requests_piece ON investigation_requests(piece_uuid, started_at)")


def _connection():
    return db.connect(_tables)


def _run_id() -> str | None:
    try:
        from server import shared_state

        return getattr(shared_state.gc_ref, "run_id", None)
    except Exception:
        return None


@contextmanager
def piece_scope(piece_uuid: str | None) -> Iterator[None]:
    token = _piece_uuid.set(piece_uuid)
    try:
        yield
    finally:
        _piece_uuid.reset(token)


def record_event(kind: str, data: dict[str, Any], *, piece_uuid: str | None = None,
                 run_id: str | None = None, at: float | None = None) -> None:
    """Queue a small structured event; never wait for SQLite on a control path."""
    event = (
        run_id or _run_id(), piece_uuid if piece_uuid is not None else _piece_uuid.get(),
        kind, float(at if at is not None else time.time()),
        time.monotonic() if at is None else None,
        json.dumps(data, default=str, separators=(",", ":")),
    )

    def write() -> None:
        with _connection() as conn:
            conn.execute(
                "INSERT INTO investigation_events(run_id,piece_uuid,kind,at,mono,data_json) "
                "VALUES(?,?,?,?,?,?)", event,
            )
            conn.commit()

    db.defer(f"investigation {kind}", write)


def record_motor_command(data: dict[str, Any]) -> None:
    # Chute commands and commands made in a piece scope are relevant to the
    # investigation. The existing control-data store still captures all axes.
    if _piece_uuid.get() or "chute" in str(data.get("stepper", "")):
        record_event("motor_command", data)


def enqueue_brickognize(*, piece_uuid: str | None, label: str | None,
                        started_at: float, ended_at: float, status_code: int | None,
                        error: str | None, images: list[bytes], response: bytes | None,
                        source_images: list[dict] | None = None) -> None:
    if not piece_uuid:
        return
    payload = (uuid.uuid4().hex, piece_uuid, _run_id(), label, started_at, ended_at,
               status_code, error, images, response, source_images or [])
    try:
        _request_queue.put_nowait(payload)
    except queue.Full:
        record_event("evidence_gap", {"reason": "Brickognize evidence queue full", "label": label},
                     piece_uuid=piece_uuid)
        return
    record_event("brickognize_request", {"request_id": payload[0], "label": label,
                                         "image_count": len(images)},
                 piece_uuid=piece_uuid, at=started_at)
    record_event("brickognize_response", {"request_id": payload[0], "label": label,
                                          "status_code": status_code, "error": error},
                 piece_uuid=piece_uuid, at=ended_at)
    _start_request_writer()


_request_queue: "queue.Queue[tuple[Any, ...]]" = queue.Queue(maxsize=32)
_request_lock = threading.Lock()
_request_thread: threading.Thread | None = None
_request_last_error: str | None = None


def request_max_bytes() -> int:
    try:
        return max(1, int(float(os.getenv("SORTER_INVESTIGATION_REQUEST_MAX_MB", "1024")) * 1024**2))
    except ValueError:
        return 1024**3


def _retire_old_requests() -> None:
    cutoff = time.time() - max(1, int(os.getenv("SORTER_INVESTIGATION_RETENTION_DAYS", "30"))) * 86400
    limit = request_max_bytes()
    with _connection() as conn:
        rows = conn.execute("SELECT id,images_json,response_path,bytes,started_at FROM investigation_requests "
                            "WHERE bytes>0 ORDER BY started_at").fetchall()
        total = sum(int(row["bytes"]) for row in rows)
        for row in rows:
            if total <= limit and float(row["started_at"]) >= cutoff:
                continue
            paths = [entry["path"] if isinstance(entry, dict) else entry
                     for entry in json.loads(row["images_json"])]
            if row["response_path"]:
                paths.append(row["response_path"])
            for relative in paths:
                (investigation_dir() / relative).unlink(missing_ok=True)
            conn.execute("UPDATE investigation_requests SET images_json='[]', response_path=NULL, "
                         "bytes=0, evicted_at=? WHERE id=?", (time.time(), row["id"]))
            total -= int(row["bytes"])
        conn.execute("DELETE FROM investigation_requests WHERE started_at < ? AND evicted_at IS NOT NULL",
                     (cutoff,))
        conn.commit()


def _start_request_writer() -> None:
    global _request_thread
    with _request_lock:
        if _request_thread is None or not _request_thread.is_alive():
            _request_thread = threading.Thread(target=_write_requests, name="investigation-requests", daemon=True)
            _request_thread.start()


def _write_requests() -> None:
    global _request_last_error
    while True:
        request_id, piece_uuid, run_id, label, started, ended, status, error, images, response, sources = _request_queue.get()
        try:
            root = investigation_dir() / "requests" / piece_uuid
            root.mkdir(parents=True, exist_ok=True)
            paths = []
            stored_bytes = 0
            for index, image in enumerate(images):
                name = f"{request_id}-{index}.jpg"
                (root / name).write_bytes(image)
                stored_bytes += len(image)
                source = sources[index] if index < len(sources) and isinstance(sources[index], dict) else {}
                paths.append({"path": f"requests/{piece_uuid}/{name}",
                              "captured_at": source.get("captured_at"),
                              "channel": source.get("channel"),
                              "source": source.get("source")})
            response_path = None
            parsed = None
            if response is not None:
                response_path = f"requests/{piece_uuid}/{request_id}.json"
                (investigation_dir() / response_path).write_bytes(response)
                stored_bytes += len(response)
                try:
                    parsed = json.dumps(json.loads(response), separators=(",", ":"))
                except (ValueError, UnicodeDecodeError):
                    pass
            with _connection() as conn:
                conn.execute(
                    "INSERT INTO investigation_requests "
                    "(id,piece_uuid,run_id,label,started_at,ended_at,status_code,error,images_json,response_path,response_json,bytes) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, piece_uuid, run_id, label, started, ended, status, error,
                     json.dumps(paths), response_path, parsed, stored_bytes),
                )
                conn.commit()
            _retire_old_requests()
        except Exception as exc:
            _request_last_error = str(exc)
            db.report_failure("investigation Brickognize evidence", exc)
        finally:
            _request_queue.task_done()


class InvestigationRecorder:
    def __init__(self, capture: Any, run_id: str, logger: Any, *, max_bytes: int = _MAX_BYTES_DEFAULT,
                 video_max_width: int = _VIDEO_MAX_WIDTH,
                 video_max_height: int = _VIDEO_MAX_HEIGHT,
                 video_jpeg_quality: int = _VIDEO_JPEG_QUALITY):
        self.capture = capture
        self.run_id = run_id
        self.logger = logger
        self.max_bytes = max(0, int(max_bytes))
        self.video_max_width = max(1, int(video_max_width))
        self.video_max_height = max(1, int(video_max_height))
        self.video_jpeg_quality = min(100, max(1, int(video_jpeg_quality)))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.subscription: Any = None
        self.last_frame_at: float | None = None
        self.last_error: str | None = None
        self.dropped = 0
        self.segment_id: str | None = None
        self._file: Any = None
        self._started_at = 0.0
        self._bytes = 0
        self._frames = 0
        self._segment_dropped = 0
        self._pending: list[tuple[str, int, float, int, int]] = []
        self._last_flush = 0.0
        self._last_seq: int | None = None
        self._dropped_since_event = 0
        self._last_gap_emit_mono = 0.0
        self._camera_gap_reported = False

    def start(self) -> None:
        if self.thread is not None:
            return
        investigation_dir().mkdir(parents=True, exist_ok=True)
        with _connection() as conn:
            conn.execute("UPDATE investigation_segments SET ended_at = COALESCE(ended_at, ?), status = 'interrupted' "
                         "WHERE ended_at IS NULL", (time.time(),))
            conn.commit()
        self.subscription = self.capture.subscribe_raw(maxsize=64)
        self.thread = threading.Thread(target=self._loop, name="investigation-video", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
        self.capture.unsubscribe_raw(self.subscription)

    def _sorting(self) -> bool:
        try:
            from defs.sorter_controller import SorterLifecycle
            from server import shared_state

            controller = shared_state.controller_ref
            return controller is not None and controller.state == SorterLifecycle.RUNNING
        except Exception:
            return False

    def _open(self, timestamp: float) -> None:
        segment_id = uuid.uuid4().hex
        investigation_dir().mkdir(parents=True, exist_ok=True)
        path = investigation_dir() / f"{segment_id}.mjpg"
        self._file = path.open("wb")
        self.segment_id = segment_id
        self._started_at = timestamp
        self._bytes = self._frames = 0
        self._segment_dropped = 0
        self._last_seq = None
        with _connection() as conn:
            conn.execute(
                "INSERT INTO investigation_segments(id,run_id,role,started_at,file_path) VALUES(?,?,?,?,?)",
                (segment_id, self.run_id, "classification_channel", timestamp, path.name),
            )
            conn.commit()

    def _flush(self) -> None:
        if not self._pending:
            return
        rows, self._pending = self._pending, []
        self._file.flush()
        with _connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO investigation_frames(segment_id,seq,captured_at,offset_bytes,length_bytes) "
                "VALUES(?,?,?,?,?)", rows,
            )
            conn.execute("UPDATE investigation_segments SET bytes=?, frames=?, dropped=? WHERE id=?",
                         (self._bytes, self._frames, self._segment_dropped, self.segment_id))
            conn.commit()
        self._last_flush = time.monotonic()

    def _close(self, status: str = "complete") -> None:
        if self._file is None:
            return
        self._emit_dropped_gap()
        self._flush()
        self._file.close()
        self._file = None
        with _connection() as conn:
            conn.execute("UPDATE investigation_segments SET ended_at=?, bytes=?, frames=?, dropped=?, status=? "
                         "WHERE id=?", (time.time(), self._bytes, self._frames, self._segment_dropped, status, self.segment_id))
            conn.commit()
        self.segment_id = None
        self._retire_old_segments()

    def _retire_old_segments(self) -> None:
        if self.max_bytes <= 0:
            return
        with _connection() as conn:
            cutoff = time.time() - max(1, int(os.getenv("SORTER_INVESTIGATION_RETENTION_DAYS", "30"))) * 86400
            rows = conn.execute("SELECT id,file_path,bytes,started_at FROM investigation_segments "
                                "WHERE ended_at IS NOT NULL AND status != 'evicted' ORDER BY started_at").fetchall()
            used = sum(int(row["bytes"]) for row in rows)
            for row in rows:
                if used <= self.max_bytes and float(row["started_at"]) >= cutoff:
                    continue
                (investigation_dir() / row["file_path"]).unlink(missing_ok=True)
                conn.execute("DELETE FROM investigation_frames WHERE segment_id=?", (row["id"],))
                conn.execute("UPDATE investigation_segments SET status='evicted' WHERE id=?", (row["id"],))
                used -= int(row["bytes"])
            conn.execute("DELETE FROM investigation_events WHERE at < ?", (cutoff,))
            conn.commit()

    def _write_frame(self, jpeg: bytes, timestamp: float, seq: int) -> None:
        if self._file is None:
            self._open(timestamp)
        if self._bytes >= _SEGMENT_BYTES or timestamp - self._started_at >= _SEGMENT_SECONDS:
            self._close()
            self._open(timestamp)
        if self._last_seq is not None and seq > self._last_seq + 1:
            lost = seq - self._last_seq - 1
            self.dropped += lost
            self._segment_dropped += lost
            self._dropped_since_event += lost
            if time.monotonic() - self._last_gap_emit_mono >= 1.0:
                self._emit_dropped_gap(timestamp)
        if self.last_frame_at is not None and timestamp - self.last_frame_at > 1.0:
            record_event("recording_gap", {"reason": "camera or recorder gap",
                                          "duration_s": round(timestamp - self.last_frame_at, 3)},
                         run_id=self.run_id, at=timestamp)
        self._last_seq = seq
        self.last_frame_at = timestamp
        self._camera_gap_reported = False
        offset = self._bytes
        self._file.write(jpeg)
        self._bytes += len(jpeg)
        self._frames += 1
        self._pending.append((self.segment_id, seq, timestamp, offset, len(jpeg)))
        if len(self._pending) >= 60 or time.monotonic() - self._last_flush > 1.0:
            self._flush()

    def _emit_dropped_gap(self, at: float | None = None) -> None:
        if not self._dropped_since_event:
            return
        record_event("recording_gap", {"reason": "frames dropped", "count": self._dropped_since_event},
                     run_id=self.run_id, at=at)
        self._dropped_since_event = 0
        self._last_gap_emit_mono = time.monotonic()

    def _encode_video_frame(self, jpeg: bytes | None = None, frame: Any = None) -> bytes:
        """Compress only the investigation copy; source and API images stay intact."""
        import cv2
        import numpy as np

        image = frame if frame is not None else cv2.imdecode(
            np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if image is None:
            raise ValueError("classification camera returned an invalid JPEG")
        height, width = image.shape[:2]
        scale = min(1.0, self.video_max_width / width, self.video_max_height / height)
        if scale < 1.0:
            image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))),
                               interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.video_jpeg_quality])
        if not ok:
            raise ValueError("could not encode investigation video frame")
        return encoded.tobytes()

    def _loop(self) -> None:
        fallback_seq = 1_000_000_000
        last_fallback_ts = 0.0
        while not self.stop_event.is_set():
            try:
                try:
                    jpeg, ts, seq = self.subscription.queue.get(timeout=0.2)
                except queue.Empty:
                    jpeg = None
                    ts = seq = None
                if not self._sorting():
                    if self._file is not None:
                        self._close()
                        self.last_frame_at = None
                        self._camera_gap_reported = False
                    for _ in range(64):
                        try:
                            self.subscription.queue.get_nowait()
                        except queue.Empty:
                            break
                    continue
                if jpeg is None and not self.capture.raw_bytes_available:
                    frame = self.capture.latest_frame
                    if frame is not None and frame.timestamp > last_fallback_ts:
                        last_fallback_ts = frame.timestamp
                        jpeg, ts, seq = self._encode_video_frame(frame=frame.raw), frame.timestamp, fallback_seq
                        fallback_seq += 1
                if jpeg is None and self._file is not None and self.last_frame_at is not None:
                    if time.time() - self.last_frame_at > 2.0 and not self._camera_gap_reported:
                        self._camera_gap_reported = True
                        record_event("recording_gap", {"reason": "camera frames stopped",
                                                       "last_frame_at": self.last_frame_at}, run_id=self.run_id)
                if jpeg is not None and ts is not None and seq is not None:
                    if self.capture.raw_bytes_available:
                        jpeg = self._encode_video_frame(jpeg=jpeg)
                    self._write_frame(jpeg, float(ts), int(seq))
                elif self._file is not None and time.monotonic() - self._last_flush > 1.0:
                    self._flush()
            except Exception as exc:
                self.last_error = str(exc)
                self.logger.warning(f"Investigation recorder: {exc}")
                try:
                    self._close("error")
                except Exception:
                    # A full disk can also prevent the index update. Release
                    # the file descriptor so sorting can continue unaffected.
                    if self._file is not None:
                        try:
                            self._file.close()
                        except Exception:
                            pass
                    self._file = None
                    self.segment_id = None
                    self._pending.clear()
                self.stop_event.wait(1.0)
        self._close()


def start(capture: Any, run_id: str, logger: Any) -> None:
    global _recorder
    if _recorder is not None or capture is None:
        return
    try:
        limit_gb = float(os.getenv("SORTER_INVESTIGATION_MAX_GB", "8"))
        _recorder = InvestigationRecorder(capture, run_id, logger,
                                          max_bytes=int(max(0.1, limit_gb) * 1024**3),
                                          video_max_width=int(os.getenv("SORTER_INVESTIGATION_VIDEO_MAX_WIDTH", "960")),
                                          video_max_height=int(os.getenv("SORTER_INVESTIGATION_VIDEO_MAX_HEIGHT", "540")),
                                          video_jpeg_quality=int(os.getenv("SORTER_INVESTIGATION_VIDEO_JPEG_QUALITY", "65")))
        _recorder.start()
    except Exception as exc:
        logger.warning(f"Investigation recorder could not start: {exc}")
        _recorder = None


def stop() -> None:
    global _recorder
    recorder, _recorder = _recorder, None
    if recorder is not None:
        recorder.stop()


def status() -> dict[str, Any]:
    recorder = _recorder
    live = (recorder is not None and recorder.segment_id is not None
            and recorder.last_frame_at is not None and time.time() - recorder.last_frame_at <= 2.0)
    with _connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS segments, COALESCE(SUM(bytes),0) AS bytes "
                           "FROM investigation_segments WHERE status != 'evicted'").fetchone()
        request_row = conn.execute("SELECT COALESCE(SUM(bytes),0) AS bytes FROM investigation_requests").fetchone()
    return {"recording": live,
            "camera_available": recorder is not None,
            "last_frame_at": recorder.last_frame_at if recorder else None,
            "last_error": recorder.last_error if recorder else None,
            "dropped": recorder.dropped if recorder else 0,
            "segments": int(row["segments"]), "bytes": int(row["bytes"]),
            "request_bytes": int(request_row["bytes"]),
            "request_max_bytes": request_max_bytes(),
            "request_last_error": _request_last_error,
            "max_bytes": recorder.max_bytes if recorder else None,
            "video_max_width": recorder.video_max_width if recorder else _VIDEO_MAX_WIDTH,
            "video_max_height": recorder.video_max_height if recorder else _VIDEO_MAX_HEIGHT,
            "video_jpeg_quality": recorder.video_jpeg_quality if recorder else _VIDEO_JPEG_QUALITY,
            "directory": str(investigation_dir())}


def list_segments(limit: int = 100) -> list[dict[str, Any]]:
    with _connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id,run_id,role,started_at,ended_at,bytes,frames,dropped,status "
            "FROM investigation_segments ORDER BY started_at DESC LIMIT ?", (max(1, min(limit, 500)),))]


def list_frames(segment_id: str, *, at: float | None = None, span: float = 30.0) -> list[dict[str, Any]]:
    with _connection() as conn:
        segment = conn.execute("SELECT id,started_at,ended_at,status FROM investigation_segments WHERE id=?",
                               (segment_id,)).fetchone()
        if segment is None or segment["status"] == "evicted":
            return []
        center = float(at if at is not None else segment["started_at"])
        return [dict(row) for row in conn.execute(
            "SELECT seq,captured_at FROM investigation_frames WHERE segment_id=? "
            "AND captured_at BETWEEN ? AND ? ORDER BY captured_at LIMIT 1500",
            (segment_id, center - min(span, 120) / 2, center + min(span, 120) / 2))]


def find_segment(at: float) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute("SELECT id,started_at,ended_at,status FROM investigation_segments "
                           "WHERE started_at <= ? AND COALESCE(ended_at, ?) >= ? AND status != 'evicted' "
                           "ORDER BY started_at DESC LIMIT 1", (at, time.time(), at)).fetchone()
        return dict(row) if row else None


def frame_bytes(segment_id: str, seq: int) -> bytes | None:
    with _connection() as conn:
        row = conn.execute("SELECT s.file_path,f.offset_bytes,f.length_bytes FROM investigation_frames f "
                           "JOIN investigation_segments s ON s.id=f.segment_id "
                           "WHERE f.segment_id=? AND f.seq=? AND s.status != 'evicted'",
                           (segment_id, seq)).fetchone()
    if row is None:
        return None
    try:
        with (investigation_dir() / row["file_path"]).open("rb") as stream:
            stream.seek(int(row["offset_bytes"]))
            return stream.read(int(row["length_bytes"]))
    except OSError:
        return None


def list_events(*, piece_uuid: str | None = None, run_id: str | None = None,
                kind: str | None = None, since: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
    where, args = [], []
    for column, value in (("piece_uuid", piece_uuid), ("run_id", run_id), ("kind", kind)):
        if value:
            where.append(f"{column}=?")
            args.append(value)
    if since is not None:
        where.append("at>=?")
        args.append(float(since))
    clause = " WHERE " + " AND ".join(where) if where else ""
    with _connection() as conn:
        rows = conn.execute("SELECT id,run_id,piece_uuid,kind,at,mono,data_json FROM investigation_events" + clause +
                            " ORDER BY at DESC LIMIT ?", (*args, max(1, min(limit, 1000)))).fetchall()
    return [{**dict(row), "data": json.loads(row["data_json"])} for row in rows]


def list_requests(piece_uuid: str) -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute("SELECT * FROM investigation_requests WHERE piece_uuid=? ORDER BY started_at",
                            (piece_uuid,)).fetchall()
    return [{**dict(row), "images": json.loads(row["images_json"]),
             "response": json.loads(row["response_json"]) if row["response_json"] else None}
            for row in rows]


def request_image_bytes(request_id: str, image_index: int) -> bytes | None:
    # Only files referenced by a saved request are served. Browser input is
    # never concatenated into a filesystem path.
    with _connection() as conn:
        row = conn.execute("SELECT images_json FROM investigation_requests WHERE id=?",
                           (request_id,)).fetchone()
    if row is None:
        return None
    paths = json.loads(row["images_json"])
    if image_index < 0 or image_index >= len(paths):
        return None
    try:
        path = paths[image_index]
        return (investigation_dir() / (path["path"] if isinstance(path, dict) else path)).read_bytes()
    except OSError:
        return None


def request_response_bytes(request_id: str) -> bytes | None:
    with _connection() as conn:
        row = conn.execute("SELECT response_path FROM investigation_requests WHERE id=?",
                           (request_id,)).fetchone()
    if row is None or not row["response_path"]:
        return None
    try:
        return (investigation_dir() / row["response_path"]).read_bytes()
    except OSError:
        return None
