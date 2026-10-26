"""Read-only local evidence for the Machine Investigation page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

import bin_contents
import incident_records
import machine_investigation as evidence
import piece_image_store
import piece_predictions
import piece_records
import stepper_telemetry
from server import shared_state

router = APIRouter(prefix="/api/machine-investigation")


@router.get("/status")
def status() -> dict[str, Any]:
    return evidence.status()


@router.get("/segments")
def segments(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {"segments": evidence.list_segments(limit)}


@router.get("/segments/{segment_id}/frames")
def frames(segment_id: str, at: float | None = None,
           span: float = Query(30.0, ge=1, le=120)) -> dict[str, Any]:
    return {"segment_id": segment_id, "frames": evidence.list_frames(segment_id, at=at, span=span)}


@router.get("/segments/{segment_id}/frames/{seq}")
def frame(segment_id: str, seq: int) -> Response:
    jpeg = evidence.frame_bytes(segment_id, seq)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="Recording frame unavailable.")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@router.get("/seek")
def seek(at: float) -> dict[str, Any]:
    return {"segment": evidence.find_segment(at)}


@router.get("/events")
def events(piece_uuid: str | None = None, run_id: str | None = None,
           kind: str | None = None, since: float | None = None,
           limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    return {"events": evidence.list_events(piece_uuid=piece_uuid, run_id=run_id,
                                           kind=kind, since=since, limit=limit)}


@router.get("/search")
def search(piece_uuid: str | None = None, part_id: str | None = None, color_id: str | None = None,
           category_id: str | None = None, run_id: str | None = None,
           bin: str | None = None, incident_kind: str | None = None,
           refused_motor_command: bool = False, stall: bool = False,
           classification_error: bool = False, recording_gap: bool = False) -> dict[str, Any]:
    clauses, args = [], []
    for column, value in (("uuid", piece_uuid), ("part_id", part_id), ("color_id", color_id),
                          ("category_id", category_id), ("run_id", run_id)):
        if value:
            clauses.append(f"p.{column}=?")
            args.append(value)
    if bin:
        try:
            layer, section, index = (int(value.strip()) for value in bin.split(","))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Bin must be layer,section,bin (zero-based).")
        clauses.extend(("p.bin_x=?", "p.bin_y=?", "p.bin_z=?"))
        args.extend((layer, section, index))
    if incident_kind:
        clauses.append("EXISTS (SELECT 1 FROM incidents i WHERE i.piece_uuid=p.uuid AND i.kind=?)")
        args.append(incident_kind)
    if refused_motor_command:
        clauses.append("EXISTS (SELECT 1 FROM investigation_events e WHERE e.piece_uuid=p.uuid "
                       "AND e.kind IN ('chute_move_refused','motor_command') "
                       "AND (e.kind='chute_move_refused' OR e.data_json LIKE '%\"success\":false%'))")
    if stall:
        clauses.append("EXISTS (SELECT 1 FROM incidents i WHERE i.piece_uuid=p.uuid AND i.kind LIKE '%stall%')")
    if classification_error:
        clauses.append("(p.classification_status IN ('error','unknown') OR EXISTS "
                       "(SELECT 1 FROM investigation_requests r WHERE r.piece_uuid=p.uuid AND r.error IS NOT NULL))")
    if recording_gap:
        clauses.append("EXISTS (SELECT 1 FROM investigation_events e WHERE e.kind='recording_gap' "
                       "AND e.at BETWEEN p.seen_at - 2 AND COALESCE(p.recorded_at,p.seen_at + 60) + 2)")
    # All feature tables share the local-state database. Prepare their schema
    # before this cross-table search on a fresh installation.
    with incident_records._connection(), evidence._connection():
        pass
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with piece_records._connection() as conn:
        rows = conn.execute(
            "SELECT p.uuid,p.run_id,p.seen_at,p.recorded_at,p.part_id,p.color_id,p.category_id,"
            "p.bin_x,p.bin_y,p.bin_z FROM piece_records p" + where +
            " ORDER BY p.id DESC LIMIT 200", args,
        ).fetchall()
    return {"pieces": [dict(row) for row in rows]}


@router.get("/bins/{layer_index}/{section_index}/{bin_index}/pieces")
def bin_pieces(layer_index: int, section_index: int, bin_index: int,
               part_id: str | None = None, color_id: str | None = None) -> dict[str, Any]:
    rows = [row for row in bin_contents.get_current_bin_pieces()
            if (row["layer_index"], row["section_index"], row["bin_index"])
            == (layer_index, section_index, bin_index)
            and (part_id is None or row["part_id"] == part_id)
            and (color_id is None or str(row["color_id"]) == color_id)]
    return {"pieces": sorted(rows, key=lambda row: row["distributed_at"], reverse=True)}


@router.get("/pieces/{piece_uuid}")
def piece(piece_uuid: str) -> dict[str, Any]:
    gc = shared_state.gc_ref
    summary = piece_records.getPieceSummaryByUuid(gc, piece_uuid)
    requests = evidence.list_requests(piece_uuid)
    events_for_piece = evidence.list_events(piece_uuid=piece_uuid, limit=500)
    images = piece_image_store.listPieceImages(piece_uuid)
    predictions = piece_predictions.listForPiece(piece_uuid)
    with incident_records._connection() as conn:
        incidents = [dict(row) for row in conn.execute(
            "SELECT id,kind,severity,status,triggered_at,resolved_at,reason,details_json "
            "FROM incidents WHERE piece_uuid=? ORDER BY triggered_at", (piece_uuid,)).fetchall()]
    if summary is None and not requests and not events_for_piece and not images:
        raise HTTPException(status_code=404, detail="Piece evidence not found.")
    seen_at = summary.get("seen_at") if summary else None
    recorded_at = summary.get("recorded_at") if summary else None
    telemetry: list[dict[str, Any]] = []
    if isinstance(seen_at, (int, float)):
        until = float(recorded_at) if isinstance(recorded_at, (int, float)) else float(seen_at) + 60
        with stepper_telemetry._connection() as conn:
            telemetry = [dict(row) for row in conn.execute(
                "SELECT recorded_at,stepper_name,channel,sg_result,cs_actual,tstep,drv_status_raw,"
                "commanded_speed,irun,microsteps,loaded,acceleration,pwm_scale,ioin "
                "FROM stepper_telemetry_samples WHERE stepper_name LIKE '%chute%' "
                "AND recorded_at BETWEEN ? AND ? ORDER BY recorded_at LIMIT 500",
                (float(seen_at) - 2, until + 2)).fetchall()]
    segment = evidence.find_segment(float(seen_at)) if isinstance(seen_at, (int, float)) else None
    return {"summary": summary, "requests": requests, "events": events_for_piece,
            "incidents": incidents,
            "chute_driver_telemetry": telemetry,
            "images": images, "predictions": predictions, "video_segment": segment,
            "physical_position": None, "physical_position_source": "No independent chute position sensor"}


@router.get("/requests/{request_id}/images/{image_index}")
def request_image(request_id: str, image_index: int) -> Response:
    if image_index < 0 or image_index > 7:
        raise HTTPException(status_code=404, detail="Request image unavailable.")
    from machine_investigation import request_image_bytes

    jpeg = request_image_bytes(request_id, image_index)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="Request image unavailable.")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@router.get("/requests/{request_id}/response")
def request_response(request_id: str) -> Response:
    body = evidence.request_response_bytes(request_id)
    if body is None:
        raise HTTPException(status_code=404, detail="Raw Brickognize response unavailable.")
    return Response(body, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=60"})
