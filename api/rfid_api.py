"""
RFID Attendance API — the RFID reader (a headless CrowPi, not a logged-in
user) phones a tap event home here. OCS resolves the tag, applies the
enter/exit toggle, and is the source of truth for the resulting attendance
record. Admin-facing endpoints (registering a tag, listing events) use the
normal user auth; the scan endpoint itself uses a shared API key instead,
the same pattern already used for the snapshot automator in
api/snapshot_proxy.py, since the device has no OCS login of its own.
"""

import os
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, g
from __init__ import db
from model.attendance import RfidTag, AttendanceEvent
from model.classroom import Classroom
from model.user import User
from api.authorize import token_required

rfid_api = Blueprint('rfid_api', __name__, url_prefix='/api/rfid')

RFID_API_KEY = os.environ.get("RFID_API_KEY", "")


def require_rfid_api_key(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not RFID_API_KEY:
            return jsonify({"message": "RFID_API_KEY not configured on server"}), 500
        key = request.headers.get('X-API-Key', '')
        if key != RFID_API_KEY:
            return jsonify({"message": "Invalid or missing API key"}), 401
        return func(*args, **kwargs)
    return wrapper


@rfid_api.route('/scan', methods=['POST'])
@require_rfid_api_key
def scan():
    """The reader calls this on every tap. No user login involved, this is
    device-to-server, authenticated by the API key above."""
    data = request.get_json(force=True)
    tag_uid = str(data.get('tag_uid', '')).strip()
    classroom_id = data.get('classroom_id')

    if not tag_uid or not classroom_id:
        return jsonify({"message": "tag_uid and classroom_id required"}), 400

    tag = RfidTag.query.filter_by(_tag_uid=tag_uid).first()
    if not tag:
        return jsonify({"message": f"tag {tag_uid} is not registered", "status": "unregistered"}), 404

    classroom = Classroom.query.get(classroom_id)
    if not classroom:
        return jsonify({"message": f"no classroom {classroom_id}"}), 404

    last_event = (
        AttendanceEvent.query.filter_by(_user_id=tag.user_id, _classroom_id=classroom_id)
        .order_by(AttendanceEvent._timestamp.desc())
        .first()
    )
    next_type = "exit" if last_event and last_event.type == "enter" else "enter"

    event = AttendanceEvent(user_id=tag.user_id, classroom_id=classroom_id, type=next_type)
    event.create()

    return jsonify({"status": "accepted", "event": event.to_dict()}), 200


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
        AttendanceEvent.query.filter_by(_classroom_id=classroom_id)
        .order_by(AttendanceEvent._timestamp.desc())
        .limit(200)
        .all()
    )
    return jsonify([e.to_dict() for e in events])
