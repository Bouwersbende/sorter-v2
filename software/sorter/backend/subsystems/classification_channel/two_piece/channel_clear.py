import time
from dataclasses import dataclass
from typing import Any, Optional

from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter

from .constants import C4_TRAVEL_SIGN, LOG_TAG

# Shared "advance the carousel forward until the channel is empty" routine. Both
# the spoke-home pre-purge and the C4 stuck-incident auto-resolve drive it, so
# they clear the channel the same way and agree on when it is actually clear.
# Forward == the same travel direction the normal classification flow uses to
# push a piece to the fall-off (discharge applies C4_TRAVEL_SIGN), so a piece is
# carried OUT the exit, never back toward the entry.
_CLEAR_STEP_OUTPUT_DEG = 72.0  # one 5-sector spoke per increment
_CLEAR_MAX_OUTPUT_DEG = 720.0  # two full revolutions before giving up


@dataclass(frozen=True)
class ChannelClearResult:
    cleared: bool
    occupied_at_start: bool
    output_deg_moved: float
    reason: str
    destination_bin: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class DiscardAimResult:
    destination_bin: tuple[int, int, int] | None
    reason: str


def _carouselStepper(irl: Any) -> Any:
    # These three attributes are aliases for the same physical motor (see
    # irl/config.py); try each so the helper works regardless of which name a
    # given setup exposes.
    return (
        getattr(irl, "carousel_stepper", None)
        or getattr(irl, "classification_channel_rotor_stepper", None)
        or getattr(irl, "c_channel_4_rotor_stepper", None)
    )


def _rev01RotateSpeed() -> int:
    try:
        from toml_config import getClassificationChannelRev01Config

        from .rev01_config import configFromDict

        return int(configFromDict(getClassificationChannelRev01Config()).rotate_speed_usteps_per_s)
    except Exception:
        return 5000


def channelOccupied(gc: Any) -> Optional[bool]:
    # Unknown occupancy keeps the pre-home purge within its blind rotation budget.
    perception_service = getattr(gc, "perception_service", None)
    if perception_service is not None:
        try:
            return int(perception_service.read_state(4).n_pieces) > 0
        except Exception:
            pass
    return None


def aimPhysicalDiscard(gc: Any, irl: Any, label: str = LOG_TAG, settle_timeout_s: float = 2.0) -> DiscardAimResult:
    """Aim and set doors for the assigned physical discard bin before sweeping.

    Failure leaves the channel stationary. Never sweep into the bucket or a
    previously selected customer's bin when the discard route is unavailable.
    """
    from sorting_profile import MISC_CATEGORY
    from subsystems.distribution.chute import BinAddress
    from bin_contents import get_current_bin_piece_counts

    chute = getattr(irl, "chute", None)
    layout = getattr(irl, "distribution_layout", None)
    if chute is None or layout is None or not getattr(chute, "homed", False):
        gc.logger.warning(f"{label} channel clear: physical discard chute is not homed")
        return DiscardAimResult(None, "chute_not_homed")
    if getattr(gc, "disable_chute", False):
        gc.logger.warning(f"{label} channel clear: chute disabled")
        return DiscardAimResult(None, "motion_disabled")
    fixed_flap = bool(getattr(gc, "fixed_closed_single_layer", False))
    if getattr(gc, "disable_servos", False) and not fixed_flap:
        gc.logger.warning(f"{label} channel clear: servos disabled without fixed closed flap mode")
        return DiscardAimResult(None, "motion_disabled")
    if fixed_flap:
        from irl.bin_layout import fixedClosedSingleLayerReady

        if not fixedClosedSingleLayerReady(layout):
            gc.logger.warning(f"{label} channel clear: fixed closed flap requires only layer 1 enabled")
            return DiscardAimResult(None, "fixed_flap_layout_invalid")
    try:
        counts = get_current_bin_piece_counts()
    except Exception as exc:
        gc.logger.warning(f"{label} channel clear: cannot read discard capacity: {exc}")
        return DiscardAimResult(None, "discard_capacity_unavailable")
    selected = None
    for layer_index, layer in enumerate(layout.layers):
        if not getattr(layer, "enabled", True):
            continue
        for section_index, section in enumerate(layer.sections):
            if not getattr(section, "enabled", True):
                continue
            for bin_index, bin_ in enumerate(section.bins):
                address = BinAddress(layer_index, section_index, bin_index)
                limit = getattr(layer, "max_pieces_per_bin", None)
                if (
                    MISC_CATEGORY in bin_.category_ids
                    and (limit is None or counts.get((layer_index, section_index, bin_index), 0) < limit)
                    and chute.isBinReachable(address)
                ):
                    selected = address
                    break
            if selected is not None:
                break
        if selected is not None:
            break
    if selected is None:
        gc.logger.warning(f"{label} channel clear: no usable physical discard bin")
        return DiscardAimResult(None, "no_usable_discard_bin")

    if fixed_flap:
        if selected.layer_index != 0:
            return DiscardAimResult(None, "fixed_flap_target_invalid")
        gc.logger.info(f"{label} channel clear: layer 1 flap is fixed closed; no servo command")
        routed_servos = []
    else:
        servos = list(getattr(irl, "servos", []) or [])
        if selected.layer_index >= len(servos):
            gc.logger.warning(f"{label} channel clear: discard layer has no servo")
            return DiscardAimResult(None, "discard_servo_missing")
        # Only doors above the target can intercept a falling piece. A disabled
        # layer below discard may have no calibration, and moving it is unnecessary.
        routed_servos = servos[: selected.layer_index + 1]
    deadline = time.monotonic() + max(0.0, settle_timeout_s)
    for i, servo in enumerate(routed_servos):
        try:
            if not getattr(servo, "is_calibrated", True):
                gc.logger.warning(f"{label} channel clear: layer {i + 1} door servo is not calibrated")
                return DiscardAimResult(None, f"layer_{i + 1}_servo_uncalibrated")
            while not servo.stopped and time.monotonic() < deadline:
                time.sleep(0.02)
            if not servo.stopped or not getattr(servo, "available", True):
                gc.logger.warning(f"{label} channel clear: layer {i} servo unavailable")
                return DiscardAimResult(None, f"layer_{i + 1}_servo_unavailable")
            if i == selected.layer_index:
                if hasattr(servo, "apply_close_speed"):
                    servo.apply_close_speed()
                accepted = servo.close()
            else:
                if hasattr(servo, "apply_open_speed"):
                    servo.apply_open_speed()
                accepted = servo.open()
            if accepted is False:
                gc.logger.warning(f"{label} channel clear: layer {i} door refused command")
                return DiscardAimResult(None, f"layer_{i + 1}_door_refused_command")
        except Exception as exc:
            gc.logger.warning(f"{label} channel clear: layer {i} door failed: {exc}")
            return DiscardAimResult(None, f"layer_{i + 1}_door_failed")
    while time.monotonic() < deadline:
        try:
            if all(bool(getattr(servo, "stopped", True)) for servo in routed_servos):
                break
        except Exception:
            return DiscardAimResult(None, "door_state_unavailable")
        time.sleep(0.02)
    if not all(bool(getattr(servo, "stopped", False)) for servo in routed_servos):
        gc.logger.warning(f"{label} channel clear: layer doors did not settle")
        return DiscardAimResult(None, "door_settle_timeout")
    try:
        while not chute.stepper.stopped and time.monotonic() < deadline:
            time.sleep(0.02)
        if not chute.stepper.stopped:
            gc.logger.warning(f"{label} channel clear: chute did not settle")
            return DiscardAimResult(None, "chute_settle_timeout")
        angle = chute.getAngleForBin(selected)
        if angle is None or chute.moveToAngleBlocking(angle, timeout_buffer_ms=1500) is None:
            gc.logger.warning(f"{label} channel clear: chute could not aim at physical discard")
            return DiscardAimResult(None, "chute_aim_failed")
    except Exception as exc:
        gc.logger.warning(f"{label} channel clear: discard aim failed: {exc}")
        return DiscardAimResult(None, "chute_aim_failed")
    gc.logger.info(f"{label} channel clear: aimed at discard bin {selected}")
    return DiscardAimResult((selected.layer_index, selected.section_index, selected.bin_index), "aimed")


def _advanceOneStep(stepper: Any, step_microsteps: int, speed_usteps_per_s: int) -> bool:
    # Blocking so the occupancy re-check happens only after the carousel has
    # actually settled, never mid-move.
    estimate_ms = 2000
    try:
        estimate_ms = int(stepper.estimateMoveStepsMs(step_microsteps, max(16, speed_usteps_per_s)))
    except Exception:
        pass
    timeout_ms = max(2000, estimate_ms * 2 + 1000)
    blocking = getattr(stepper, "move_steps_blocking", None)
    if callable(blocking):
        return bool(blocking(int(step_microsteps), timeout_ms=timeout_ms))
    if not bool(stepper.move_steps(int(step_microsteps))):
        return False
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if bool(getattr(stepper, "stopped", True)):
            return True
        time.sleep(0.01)
    return True


def clearChannelByAdvancing(
    gc: Any,
    irl: Any,
    irl_config: Any,
    *,
    speed_usteps_per_s: Optional[int] = None,
    step_output_deg: float = _CLEAR_STEP_OUTPUT_DEG,
    max_output_deg: float = _CLEAR_MAX_OUTPUT_DEG,
    label: str = LOG_TAG,
) -> ChannelClearResult:
    occupied = channelOccupied(gc)
    if occupied is False:
        gc.logger.info(f"{label} channel clear: already empty, nothing to advance")
        return ChannelClearResult(True, False, 0.0, "already_clear")

    stepper = _carouselStepper(irl)
    if stepper is None:
        gc.logger.warning(f"{label} channel clear: carousel stepper unavailable")
        return ChannelClearResult(False, bool(occupied), 0.0, "no_stepper")

    if speed_usteps_per_s is None:
        speed_usteps_per_s = _rev01RotateSpeed()
    try:
        stepper.set_speed_limits(16, max(16, int(speed_usteps_per_s)))
    except Exception as exc:
        gc.logger.warning(f"{label} channel clear: set_speed_limits failed: {exc}")

    platter = C4FiveSectorPlatter.from_irl_config(irl_config)
    step_microsteps = platter.output_degrees_to_motor_microsteps(
        C4_TRAVEL_SIGN * abs(step_output_deg)
    )

    aim = aimPhysicalDiscard(gc, irl, label)
    if aim.destination_bin is None:
        return ChannelClearResult(False, bool(occupied), 0.0, f"discard_unavailable:{aim.reason}")
    destination_bin = aim.destination_bin

    if occupied is None:
        gc.logger.info(
            f"{label} channel clear: occupancy unknown — advancing up to "
            f"{abs(max_output_deg):.0f}° blind"
        )
    else:
        gc.logger.info(
            f"{label} channel clear: piece on channel — advancing forward in "
            f"{abs(step_output_deg):.0f}° steps until clear (max {abs(max_output_deg):.0f}°)"
        )

    moved_output_deg = 0.0
    while moved_output_deg < abs(max_output_deg):
        if not _advanceOneStep(stepper, step_microsteps, int(speed_usteps_per_s)):
            gc.logger.warning(f"{label} channel clear: advance move not acknowledged — aborting")
            return ChannelClearResult(False, True, moved_output_deg, "move_failed", destination_bin)
        moved_output_deg += abs(step_output_deg)
        occupied = channelOccupied(gc)
        if occupied is False:
            gc.logger.info(
                f"{label} channel clear: channel empty after advancing {moved_output_deg:.0f}°"
            )
            return ChannelClearResult(True, True, moved_output_deg, "cleared", destination_bin)

    cleared = channelOccupied(gc) is False
    gc.logger.info(
        f"{label} channel clear: advanced full budget {moved_output_deg:.0f}° (cleared={cleared})"
    )
    return ChannelClearResult(
        cleared, True, moved_output_deg, "cleared" if cleared else "budget_exhausted", destination_bin
    )
