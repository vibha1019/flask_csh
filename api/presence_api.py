"""
Presence API — one input-agnostic endpoint that every presence signal
(RFID tap, camera result) posts to, plus the period logic that turns those
events into attendance.

Devices authenticate with the shared X-API-Key (RFID_API_KEY), the same
device-to-server pattern as the original /api/rfid/scan, which is now a
thin wrapper around record_event() below. Teacher/admin actions use the
normal OCS login.

RFID tap body:
    {
      "event_id": "<uuid from the device>",     # retries with the same id are not logged twice
      "source": "rfid",
      "classroom_id": 1,
      "occurred_at": "2026-10-05T18:42:07.123Z",  # must include Z or an offset
      "tag_uid": "...",
      "device_id": "crowpi-room1"               # optional
    }

The server derives the tap type: 'enter'/'exit' toggle per user,
classroom, and period (so it resets every period instead of carrying
over), or 'ignored' if occurred_at falls outside every period window.

Camera result body (the camera's answer for one tap, see Jarvis3000#17):
    {
      "event_id": "<uuid for this result>",
      "source": "camera",
      "classroom_id": 1,
      "tap_event_id": "<event_id of the tap it answers>",
      "occurred_at": "<when the picture was taken>",
      "result": "match" | "no_match" | "no_face" | "not_enrolled",
      "uid": "...",                             # who was recognized; required for match
      "confidence": 0.93,                       # optional, 0 to 1
      "device_id": "door-cam"                   # optional
    }

The camera finds taps to answer by polling
GET /api/presence/classrooms/<id>/taps/pending.
"""

import os
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, request, jsonify
from sqlalchemy.exc import IntegrityError

from __init__ import db
from api.authorize import token_required
from model import bell_schedule
from model.attendance import RfidTag
from model.classroom import Classroom
from model.presence import CameraCheck, PresenceEvent, PresencePeriod, to_iso_z, utc_now
from model.user import User

presence_api = Blueprint('presence_api', __name__, url_prefix='/api/presence')

DEVICE_API_KEY = os.environ.get("RFID_API_KEY", "")
SOURCES = {"rfid", "camera"}
SUPPORTED_SOURCES = {"rfid"}  # taps; camera results go through record_camera_result()
# A device clock running ahead of the server by more than this is treated
# as wrong, and the server's receive time is used instead.
MAX_FUTURE_SKEW = timedelta(minutes=2)
# How long after a tap the camera can still pick it up, and how long the
# dashboard shows a tap as being checked before calling it unverified.
CAMERA_WINDOW = timedelta(seconds=15)


def require_device_api_key(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not DEVICE_API_KEY:
            return jsonify({"message": "RFID_API_KEY not configured on server"}), 500
        if request.headers.get('X-API-Key', '') != DEVICE_API_KEY:
            return jsonify({"message": "Invalid or missing API key"}), 401
        return func(*args, **kwargs)
    return wrapper


class PresenceError(Exception):
    def __init__(self, message, status_code=400, **extra):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra

    def response(self):
        return jsonify({"message": self.message, **self.extra}), self.status_code


def require_event_id(data):
    event_id = str(data.get("event_id") or "").strip()
    if not event_id or len(event_id) > 64:
        raise PresenceError("event_id required (max 64 chars)")
    return event_id


def require_classroom(data):
    classroom_id = data.get("classroom_id")
    if not isinstance(classroom_id, int) or not Classroom.query.get(classroom_id):
        raise PresenceError(f"no classroom {classroom_id}", 404)
    return classroom_id


def parse_occurred_at(value):
    """ISO 8601 with a timezone -> naive UTC. Naive timestamps are rejected
    since there is no way to know what zone the device meant."""
    if not isinstance(value, str) or not value:
        raise PresenceError("occurred_at required (ISO 8601 with Z or an offset)")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PresenceError(f"occurred_at is not ISO 8601: {value!r}")
    if dt.tzinfo is None:
        raise PresenceError("occurred_at must include a timezone (Z or an offset)")
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def period_at(dt):
    """The period whose window contains dt (naive UTC): the bell schedule
    first, then a manually started period. Bell periods are stored the
    first time they are needed so events can reference them."""
    bell = bell_schedule.get_bell_period(dt)
    if bell:
        name, start_utc, end_utc = bell
        period = PresencePeriod.query.filter_by(_source="bell", _name=name, _start_time=start_utc).first()
        if not period:
            period = PresencePeriod(
                name=name,
                start_time=start_utc,
                duration_seconds=int((end_utc - start_utc).total_seconds()),
                grace_seconds=bell_schedule.DEFAULT_GRACE_SECONDS,
                source="bell",
            )
            db.session.add(period)
            db.session.commit()
        return period

    manual = (
        PresencePeriod.query.filter(PresencePeriod._source == "manual", PresencePeriod._start_time <= dt)
        .order_by(PresencePeriod._start_time.desc())
        .first()
    )
    if manual and manual.contains(dt):
        return manual
    return None


def current_period(now):
    """The period a dashboard should show right now: the live bell period
    if school is in session, otherwise the most recently started manual
    period (kept after it ends so final states stay visible)."""
    if bell_schedule.get_bell_period(now):
        return period_at(now)
    return (
        PresencePeriod.query.filter_by(_source="manual", _active=True)
        .order_by(PresencePeriod.id.desc())
        .first()
    )


def resolve_user(source, data):
    if source == "rfid":
        tag_uid = str(data.get("tag_uid") or "").strip()
        if not tag_uid:
            raise PresenceError("tag_uid required for source rfid")
        tag = RfidTag.query.filter_by(_tag_uid=tag_uid).first()
        if not tag:
            raise PresenceError(f"tag {tag_uid} is not registered", 404, status="unregistered")
        return tag.user, tag_uid

    uid = str(data.get("uid") or "").strip()
    if not uid:
        raise PresenceError(f"uid required for source {source}")
    user = User.query.filter_by(_uid=uid).first()
    if not user:
        raise PresenceError(f"no user with uid {uid}", 404, status="unregistered")
    return user, None


def record_event(data):
    """Validates, de-duplicates, and stores one presence event. Returns
    (response_dict, http_status). Raises PresenceError on bad input."""
    received_at = utc_now()
    event_id = require_event_id(data)

    existing = PresenceEvent.query.filter_by(_event_id=event_id).first()
    if existing:
        return {"status": "accepted", "duplicate": True, "event": existing.to_dict()}, 200

    source = data.get("source")
    if source not in SOURCES:
        raise PresenceError(f"source must be one of {sorted(SOURCES)}")
    if source not in SUPPORTED_SOURCES:
        raise PresenceError(f"source {source} is not supported yet")

    classroom_id = require_classroom(data)

    occurred_at = parse_occurred_at(data.get("occurred_at"))
    adjusted = False
    if occurred_at > received_at + MAX_FUTURE_SKEW:
        occurred_at = received_at
        adjusted = True

    user, tag_uid = resolve_user(source, data)

    period = period_at(occurred_at)
    if period:
        last_event = (
            PresenceEvent.query.filter(
                PresenceEvent._user_id == user.id,
                PresenceEvent._classroom_id == classroom_id,
                PresenceEvent._period_id == period.id,
                PresenceEvent._type.in_(["enter", "exit"]),
                PresenceEvent._occurred_at <= occurred_at,
            )
            .order_by(PresenceEvent._occurred_at.desc(), PresenceEvent.id.desc())
            .first()
        )
        event_type = "exit" if last_event and last_event.type == "enter" else "enter"
    else:
        event_type = "ignored"

    device_id = data.get("device_id")
    event = PresenceEvent(
        event_id=event_id,
        source=source,
        user_id=user.id,
        classroom_id=classroom_id,
        period_id=period.id if period else None,
        type=event_type,
        occurred_at=occurred_at,
        received_at=received_at,
        tag_uid=tag_uid,
        device_id=str(device_id)[:64] if device_id else None,
    )
    try:
        db.session.add(event)
        db.session.commit()
    except IntegrityError:
        # Same event_id arrived concurrently; the other request won.
        db.session.rollback()
        existing = PresenceEvent.query.filter_by(_event_id=event_id).first()
        return {"status": "accepted", "duplicate": True, "event": existing.to_dict()}, 200

    response = {"status": "accepted", "duplicate": False, "event": event.to_dict()}
    if adjusted:
        response["occurred_at_adjusted"] = True
    return response, 200


def record_camera_result(data):
    """Stores the camera's answer for one tap. One result per tap: a retry
    with the same event_id is a duplicate, a second result with a new
    event_id is rejected with 409."""
    received_at = utc_now()
    event_id = require_event_id(data)

    existing = CameraCheck.query.filter_by(_event_id=event_id).first()
    if existing:
        return {"status": "accepted", "duplicate": True, "check": existing.to_dict()}, 200

    classroom_id = require_classroom(data)

    tap_event_id = str(data.get("tap_event_id") or "").strip()
    if not tap_event_id:
        raise PresenceError("tap_event_id required for source camera")
    tap = PresenceEvent.query.filter_by(_event_id=tap_event_id, _source="rfid").first()
    if not tap:
        raise PresenceError(f"no tap with event_id {tap_event_id}", 404)
    if tap._classroom_id != classroom_id:
        raise PresenceError(f"tap {tap_event_id} is not in classroom {classroom_id}")
    if CameraCheck.query.filter_by(_tap_id=tap.id).first():
        raise PresenceError(f"tap {tap_event_id} already has a camera result", 409, status="already_checked")

    result = data.get("result")
    if result not in CameraCheck.RESULTS:
        raise PresenceError(f"result must be one of {list(CameraCheck.RESULTS)}")

    recognized_user = None
    if result == "match":
        uid = str(data.get("uid") or "").strip()
        if not uid:
            raise PresenceError("uid required when result is match")
        recognized_user = User.query.filter_by(_uid=uid).first()
        if not recognized_user:
            raise PresenceError(f"no user with uid {uid}", 404, status="unregistered")

    confidence = data.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise PresenceError("confidence must be a number from 0 to 1")
        confidence = float(confidence)

    occurred_at = parse_occurred_at(data.get("occurred_at"))
    if occurred_at > received_at + MAX_FUTURE_SKEW:
        occurred_at = received_at

    device_id = data.get("device_id")
    check = CameraCheck(
        event_id=event_id,
        tap_id=tap.id,
        result=result,
        recognized_user_id=recognized_user.id if recognized_user else None,
        confidence=confidence,
        occurred_at=occurred_at,
        received_at=received_at,
        device_id=str(device_id)[:64] if device_id else None,
    )
    try:
        db.session.add(check)
        db.session.commit()
    except IntegrityError:
        # A concurrent request stored this event_id or a result for this tap first.
        db.session.rollback()
        existing = CameraCheck.query.filter_by(_event_id=event_id).first()
        if existing:
            return {"status": "accepted", "duplicate": True, "check": existing.to_dict()}, 200
        raise PresenceError(f"tap {tap_event_id} already has a camera result", 409, status="already_checked")

    return {"status": "accepted", "duplicate": False, "check": check.to_dict()}, 200


@presence_api.route('/event', methods=['POST'])
@require_device_api_key
def post_event():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"message": "JSON object body required"}), 400
    try:
        if data.get("source") == "camera":
            body, status = record_camera_result(data)
        else:
            body, status = record_event(data)
    except PresenceError as e:
        return e.response()
    return jsonify(body), status


@presence_api.route('/classrooms/<int:classroom_id>/taps/pending', methods=['GET'])
@require_device_api_key
def pending_taps(classroom_id):
    """Taps from the last CAMERA_WINDOW that the camera has not answered
    yet. The camera polls this about once a second, takes a picture for
    each, and posts a camera result back."""
    if not Classroom.query.get(classroom_id):
        return jsonify({"message": f"no classroom {classroom_id}"}), 404

    taps = (
        PresenceEvent.query.outerjoin(CameraCheck, CameraCheck._tap_id == PresenceEvent.id)
        .filter(
            CameraCheck.id.is_(None),
            PresenceEvent._classroom_id == classroom_id,
            PresenceEvent._source == "rfid",
            PresenceEvent._type.in_(["enter", "exit"]),
            PresenceEvent._occurred_at >= utc_now() - CAMERA_WINDOW,
        )
        .order_by(PresenceEvent._occurred_at)
        .all()
    )
    return jsonify([
        {
            "tap_event_id": tap.event_id,
            "uid": tap.user.uid if tap.user else None,
            "occurred_at": to_iso_z(tap.occurred_at),
            "type": tap.type,
        }
        for tap in taps
    ])


@presence_api.route('/periods/start', methods=['POST'])
@token_required(["Admin", "Teacher"])
def start_period():
    """Starts a manual period now, for demos/testing outside school hours.
    Bell-schedule periods always take precedence while school is in session."""
    data = request.get_json(silent=True) or {}
    try:
        duration_seconds = int(data.get("duration_seconds", 120))
        grace_seconds = int(data.get("grace_seconds", 15))
    except (TypeError, ValueError):
        return jsonify({"message": "duration_seconds and grace_seconds must be integers"}), 400
    if duration_seconds <= 0 or grace_seconds < 0:
        return jsonify({"message": "duration_seconds must be > 0 and grace_seconds >= 0"}), 400

    PresencePeriod.query.filter_by(_source="manual", _active=True).update({"_active": False})
    period = PresencePeriod(
        name=str(data.get("name") or "Demo Period")[:80],
        # Whole seconds, so a tap stamped (to the millisecond) right as the
        # period starts never sorts before it.
        start_time=utc_now().replace(microsecond=0),
        duration_seconds=duration_seconds,
        grace_seconds=grace_seconds,
        source="manual",
        active=True,
    )
    db.session.add(period)
    db.session.commit()
    return jsonify(period.to_dict(now=utc_now())), 201


def tap_verification(tap, check, now):
    """Camera verification for one tap, as shown on the dashboard."""
    if tap._source != "rfid":
        return None
    if check:
        return check.verification
    return "PENDING" if now - tap.occurred_at <= CAMERA_WINDOW else "UNVERIFIED"


def compute_state(events, period, now):
    """State for one student in one period, from that student's counted
    (enter/exit) events in occurred_at order. Ported from the standalone
    prototype."""
    window_open = now < period.end_time

    if not events:
        return "NOT_YET_ARRIVED" if window_open else "ABSENT"

    first_in = next((e for e in events if e.type == "enter"), None)
    late = first_in is not None and first_in.occurred_at > period.start_time + timedelta(
        seconds=period.grace_seconds
    )
    currently_in = events[-1].type == "enter"

    if currently_in:
        return "TARDY" if late else "PRESENT"

    return "TEMP_OUT" if window_open else "LEFT_EARLY"


@presence_api.route('/classrooms/<int:classroom_id>/status', methods=['GET'])
@token_required(["Admin", "Teacher"])
def classroom_status(classroom_id):
    """Each student's current state for the current period. The roster is
    everyone enrolled in the classroom plus anyone who has ever produced a
    presence event in it (so tagged students show up before enrollment is
    set up). Restricted to teachers/admins since it shows the whole class."""
    classroom = Classroom.query.get(classroom_id)
    if not classroom:
        return jsonify({"message": f"no classroom {classroom_id}"}), 404

    now = utc_now()
    period = current_period(now)
    body = {
        "classroom": {"id": classroom.id, "name": classroom.name},
        "generated_at": to_iso_z(now),
        "period": period.to_dict(now=now) if period else None,
        "students": [],
    }
    if not period:
        return jsonify(body)

    enrolled = {u.id: u for u in classroom.students}
    seen_ids = {
        row[0]
        for row in db.session.query(PresenceEvent._user_id)
        .filter(PresenceEvent._classroom_id == classroom_id)
        .distinct()
    }
    roster = dict(enrolled)
    missing = seen_ids - roster.keys()
    if missing:
        roster.update({u.id: u for u in User.query.filter(User.id.in_(missing))})

    events_by_user = {}
    period_events = (
        PresenceEvent.query.filter(
            PresenceEvent._classroom_id == classroom_id,
            PresenceEvent._period_id == period.id,
            PresenceEvent._type.in_(["enter", "exit"]),
        )
        .order_by(PresenceEvent._occurred_at, PresenceEvent.id)
        .all()
    )
    for event in period_events:
        events_by_user.setdefault(event.user_id, []).append(event)
    checks_by_tap = {}
    if period_events:
        checks = CameraCheck.query.filter(CameraCheck._tap_id.in_([e.id for e in period_events])).all()
        checks_by_tap = {c.tap_id: c for c in checks}

    for user in sorted(roster.values(), key=lambda u: (u.name or "").lower()):
        events = events_by_user.get(user.id, [])
        last = events[-1] if events else None
        body["students"].append({
            "uid": user.uid,
            "name": user.name,
            "state": compute_state(events, period, now),
            "since": to_iso_z(last.occurred_at) if last else None,
            "last_source": last._source if last else None,
            # Camera verification of the student's most recent tap.
            "verification": tap_verification(last, checks_by_tap.get(last.id), now) if last else None,
            "enrolled": user.id in enrolled,
        })
    return jsonify(body)
