"""The evidence path keeps exact bytes and timestamps without controlling hardware."""

from __future__ import annotations

import queue
import sqlite3
import time
from types import SimpleNamespace

import cv2
import numpy as np

import db
import machine_investigation as evidence
from classification.brickognize import _classifyImages


class FakeCapture:
    def __init__(self):
        self.subscription = SimpleNamespace(queue=queue.Queue(), dropped=0)
        self.raw_bytes_available = True
        self.latest_frame = None

    def subscribe_raw(self, maxsize=8):
        return self.subscription

    def unsubscribe_raw(self, sub):
        assert sub is self.subscription


def test_video_frames_seek_and_report_camera_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    capture = FakeCapture()
    recorder = evidence.InvestigationRecorder(
        capture, "run-1", SimpleNamespace(warning=lambda message: None), max_bytes=1024**2
    )
    monkeypatch.setattr(recorder, "_sorting", lambda: True)
    recorder.start()
    now = time.time() - 3
    first = cv2.imencode(".jpg", np.full((12, 20, 3), 40, np.uint8))[1].tobytes()
    second = cv2.imencode(".jpg", np.full((12, 20, 3), 180, np.uint8))[1].tobytes()
    capture.subscription.queue.put((first, now, 1))
    capture.subscription.queue.put((second, now + 2, 3))
    deadline = time.monotonic() + 3
    while recorder._frames < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    recorder.stop()
    assert db.drain(2.0)
    assert recorder._frames == 2

    segment = evidence.list_segments()[0]
    assert segment["run_id"] == "run-1"
    assert segment["dropped"] == 1
    assert evidence.find_segment(now + 1)["id"] == segment["id"]
    frames = evidence.list_frames(segment["id"], at=now + 1, span=5)
    assert [frame["captured_at"] for frame in frames] == [now, now + 2]
    frame_one = cv2.imdecode(np.frombuffer(evidence.frame_bytes(segment["id"], 1), np.uint8), cv2.IMREAD_COLOR)
    frame_three = cv2.imdecode(np.frombuffer(evidence.frame_bytes(segment["id"], 3), np.uint8), cv2.IMREAD_COLOR)
    assert frame_one.shape[:2] == frame_three.shape[:2] == (12, 20)
    assert frame_one.mean() < frame_three.mean()
    assert evidence.frame_bytes(segment["id"], 2) is None
    assert evidence.list_events(kind="recording_gap")


def test_video_copy_is_downscaled_without_changing_request_image(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    recorder = evidence.InvestigationRecorder(
        FakeCapture(), "run-small-video", SimpleNamespace(warning=lambda message: None), max_bytes=1024**2
    )
    random = np.random.default_rng(123)
    source = random.integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    original = cv2.imencode(".jpg", source, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
    timestamp = time.time()
    video_copy = recorder._encode_video_frame(jpeg=original)
    recorder._write_frame(video_copy, timestamp, 1)
    recorder._close()
    segment = evidence.list_segments()[0]
    saved = evidence.frame_bytes(segment["id"], 1)
    assert saved == video_copy
    assert evidence.list_frames(segment["id"], at=timestamp, span=1)[0]["captured_at"] == timestamp
    assert cv2.imdecode(np.frombuffer(saved, np.uint8), cv2.IMREAD_COLOR).shape[:2] == (540, 960)
    assert len(saved) < len(original)


def test_brickognize_evidence_keeps_exact_uploaded_jpeg_and_unfiltered_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("SORTER_INVESTIGATION_REQUEST_MAX_MB", "10")
    raw_reply = b'{"items":[{"id":"first","category":"primo"},{"id":"second","category":"lego"}],"colors":[]}'
    sent = []

    class Response:
        status_code = 200
        content = raw_reply

        def raise_for_status(self):
            pass

        def json(self):
            import json

            return json.loads(self.content)

    def post(_url, *, files, **_kwargs):
        sent.extend(part[1][1].getvalue() for part in files)
        return Response()

    monkeypatch.setattr("classification.brickognize.requests.post", post)
    image = np.zeros((16, 16, 3), np.uint8)
    result = _classifyImages(None, [image], piece_uuid="piece-one", dump_label="combined",
                             source_images=[{"captured_at": 42.0, "channel": 4, "source": "c4_burst"}])
    evidence._request_queue.join()

    requests = evidence.list_requests("piece-one")
    assert len(requests) == 1
    request = requests[0]
    assert request["response"]["items"][0]["category"] == "primo"
    assert result["items"][0]["category"] == "lego"
    assert request["images"][0]["captured_at"] == 42.0
    assert evidence.request_image_bytes(request["id"], 0) == sent[0]
    decoded = cv2.imdecode(np.frombuffer(sent[0], np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[:2] == (16, 16)
    assert db.drain(2.0)
    events = evidence.list_events(piece_uuid="piece-one")
    assert {event["kind"] for event in events} >= {"brickognize_request", "brickognize_response"}


def test_video_and_request_retention_keep_metadata_but_remove_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    capture = FakeCapture()
    recorder = evidence.InvestigationRecorder(
        capture, "run-retention", SimpleNamespace(warning=lambda message: None), max_bytes=8
    )
    recorder._write_frame(b"12345678", time.time() - 10, 1)
    recorder._close()
    recorder._write_frame(b"abcdefgh", time.time() - 5, 2)
    recorder._close()
    segments = evidence.list_segments()
    assert [segment["status"] for segment in segments] == ["complete", "evicted"]
    assert evidence.frame_bytes(segments[1]["id"], 1) is None
    assert evidence.frame_bytes(segments[0]["id"], 2) == b"abcdefgh"

    monkeypatch.setenv("SORTER_INVESTIGATION_REQUEST_MAX_MB", "0.000001")
    evidence.enqueue_brickognize(piece_uuid="retained-piece", label="retry",
                                 started_at=time.time() - 2, ended_at=time.time() - 1,
                                 status_code=500, error="HTTP 500", images=[b"JPEG bytes"],
                                 response=b'{"items":[]}')
    evidence._request_queue.join()
    request = evidence.list_requests("retained-piece")[0]
    assert request["status_code"] == 500
    assert request["error"] == "HTTP 500"
    assert request["evicted_at"] is not None
    assert evidence.request_image_bytes(request["id"], 0) is None
    assert evidence.request_response_bytes(request["id"]) is None


def test_complete_simulated_piece_trace_and_search(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    from server.routers import machine_investigation as router
    import piece_records

    now = time.time() - 5
    recorder = evidence.InvestigationRecorder(
        FakeCapture(), "run-trace", SimpleNamespace(warning=lambda message: None), max_bytes=1024
    )
    recorder._write_frame(b"camera-frame", now, 1)
    recorder._close()
    with piece_records._connection() as conn:
        conn.execute("INSERT INTO piece_records(uuid,run_id,seen_at,recorded_at,part_id,color_id,category_id,bin_x,bin_y,bin_z) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", ("piece-trace", "run-trace", now, now + 2,
                                                       "3001", "5", "zakje-1", 0, 0, 1))
        conn.commit()
    evidence.record_event("track_assigned", {"track_id": 7}, piece_uuid="piece-trace", run_id="run-trace", at=now)
    evidence.record_event("routing_decision", {"destination_bin": [0, 0, 1]},
                          piece_uuid="piece-trace", run_id="run-trace", at=now + 1)
    evidence.record_event("chute_move_refused", {"target_angle_deg": 25},
                          piece_uuid="piece-trace", run_id="run-trace", at=now + 1.1)
    evidence.record_event("release_recorded", {"physical_confirmation": None},
                          piece_uuid="piece-trace", run_id="run-trace", at=now + 2)
    assert db.drain(2.0)
    monkeypatch.setattr(router.piece_records, "getPieceSummaryByUuid", lambda _gc, _uuid: {
        "uuid": "piece-trace", "seen_at": now, "recorded_at": now + 2,
        "part_id": "3001", "color_id": "5", "category_id": "zakje-1",
        "bin": {"x": 0, "y": 0, "z": 1},
    })
    trace = router.piece("piece-trace")
    assert trace["video_segment"]["id"] == evidence.find_segment(now)["id"]
    assert {event["kind"] for event in trace["events"]} >= {
        "track_assigned", "routing_decision", "chute_move_refused", "release_recorded"
    }
    assert trace["physical_position"] is None
    assert [row["uuid"] for row in router.search(refused_motor_command=True)["pieces"]] == ["piece-trace"]


def test_existing_request_table_gets_storage_columns_and_preserves_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STATE_DB_PATH", str(tmp_path / "state.sqlite"))
    image = tmp_path / "machine_investigation" / "requests" / "legacy-piece" / "image.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"legacy-jpeg")
    with sqlite3.connect(tmp_path / "state.sqlite") as conn:
        conn.execute(
            "CREATE TABLE investigation_requests (id TEXT PRIMARY KEY, piece_uuid TEXT NOT NULL, "
            "run_id TEXT, label TEXT, started_at REAL NOT NULL, ended_at REAL NOT NULL, "
            "status_code INTEGER, error TEXT, images_json TEXT NOT NULL, response_path TEXT, response_json TEXT)"
        )
        conn.execute(
            "INSERT INTO investigation_requests "
            "(id,piece_uuid,started_at,ended_at,images_json) VALUES(?,?,?,?,?)",
            ("legacy-request", "legacy-piece", time.time(), time.time(),
             '[{"path":"requests/legacy-piece/image.jpg"}]'),
        )
    assert evidence.status()["request_bytes"] == len(b"legacy-jpeg")
    assert evidence.request_image_bytes("legacy-request", 0) == b"legacy-jpeg"
    with evidence._connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(investigation_requests)")}
    assert {"bytes", "evicted_at"} <= columns
