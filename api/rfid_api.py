"""
RFID Attendance API — the RFID reader (a headless CrowPi, not a logged-in
user) phones a tap event home here. /scan is now a thin wrapper around the
generic presence API (api/presence_api.py), kept so readers that have not
switched to /api/presence/event keep working. Admin-facing endpoints
(registering a tag, listing events) use the normal user auth; the scan
endpoint uses the shared device API key, since the device has no OCS login
of its own.
"""

import uuid

from flask import Blueprint, request, jsonify, g
from __init__ import db
from model.attendance import RfidTag
from model.classroom import Classroom
from model.presence import PresenceEvent, to_iso_z, utc_now
from model.user import User
from api.authorize import token_required
from api.presence_api import PresenceError, record_event, require_device_api_key

rfid_api = Blueprint('rfid_api', __name__, url_prefix='/api/rfid')


@rfid_api.route('/scan', methods=['POST'])
@require_device_api_key
def scan():
    """The reader calls this on every tap. Forwards to the generic presence
    event handler as source=rfid. Older readers that do not send event_id
    or occurred_at get a fresh id and the server's receive time."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"message": "JSON object body required"}), 400
    if not data.get('tag_uid') or not data.get('classroom_id'):
        return jsonify({"message": "tag_uid and classroom_id required"}), 400

    event = {
        "event_id": data.get("event_id") or str(uuid.uuid4()),
        "source": "rfid",
        "classroom_id": data.get("classroom_id"),
        "occurred_at": data.get("occurred_at") or to_iso_z(utc_now()),
        "tag_uid": str(data.get("tag_uid")).strip(),
        "device_id": data.get("device_id"),
    }
    try:
        body, status = record_event(event)
    except PresenceError as e:
        return e.response()
    return jsonify(body), status


@rfid_api.route('/register', methods=['POST'])
@token_required(["Admin", "Teacher"])
def register_tag():
    """Binds a tag UID to an existing OCS user. This is the tag-registration
    step, kept separate from /scan and gated behind real OCS login since
    it's a data-changing admin action, not a device reporting a tap."""
    data = request.get_json(force=True)
    tag_uid = str(data.get('tag_uid', '')).strip()
    user_id = data.get('user_id')

    if not tag_uid or not user_id:
        return jsonify({"message": "tag_uid and user_id required"}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({"message": f"no user {user_id}"}), 404

    existing = RfidTag.query.filter_by(_tag_uid=tag_uid).first()
    if existing:
        existing.user_id = user_id
        db.session.commit()
        return jsonify(existing.to_dict())

    tag = RfidTag(tag_uid=tag_uid, user_id=user_id)
    tag.create()
    return jsonify(tag.to_dict()), 201


@rfid_api.route('/classrooms/<int:classroom_id>/events', methods=['GET'])
@token_required()
def list_events(classroom_id):
    """Where the attendance record actually becomes visible in OCS."""
    current_user = g.current_user
    classroom = Classroom.query.get_or_404(classroom_id)
    if current_user.role not in ['Admin', 'Teacher'] and current_user.school != classroom.school_name:
        return jsonify({"message": "Access denied"}), 403

    events = (
        PresenceEvent.query.filter_by(_classroom_id=classroom_id)
        .order_by(PresenceEvent._occurred_at.desc())
        .limit(200)
        .all()
    )
    return jsonify([e.to_dict() for e in events])
