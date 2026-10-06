from datetime import datetime, timedelta, timezone
from __init__ import db


def utc_now():
    """Naive UTC now. All presence timestamps are stored as naive UTC since
    neither SQLite nor MySQL DATETIME keeps a timezone; they are converted
    to UTC on the way in and serialized with a trailing Z on the way out."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso_z(dt):
    if dt is None:
        return None
    return dt.isoformat(timespec="milliseconds") + "Z"


class PresencePeriod(db.Model):
    """A class period that attendance events are counted against. "bell"
    periods are derived from the school bell schedule on demand; "manual"
    periods are started by a teacher/admin for demos or testing outside
    school hours. Periods are school-wide; events carry the classroom."""
    __tablename__ = 'presence_periods'

    id = db.Column(db.Integer, primary_key=True)
    _name = db.Column(db.String(80), nullable=False)
    _start_time = db.Column(db.DateTime, nullable=False)
    _duration_seconds = db.Column(db.Integer, nullable=False)
    _grace_seconds = db.Column(db.Integer, nullable=False)
    _source = db.Column(db.String(10), nullable=False)  # 'bell' or 'manual'
    # Only meaningful for manual periods: the most recently started one stays
    # current (even after it ends, so final states remain visible) until
    # another is started.
    _active = db.Column(db.Boolean, nullable=False, default=False)

    def __init__(self, name, start_time, duration_seconds, grace_seconds, source, active=False):
        self._name = name
        self._start_time = start_time
        self._duration_seconds = duration_seconds
        self._grace_seconds = grace_seconds
        self._source = source
        self._active = active

    @property
    def name(self): return self._name

    @property
    def start_time(self): return self._start_time

    @property
    def grace_seconds(self): return self._grace_seconds

    @property
    def source(self): return self._source

    @property
    def end_time(self):
        return self._start_time + timedelta(seconds=self._duration_seconds)

    def contains(self, dt):
        return self._start_time <= dt < self.end_time

    def to_dict(self, now=None):
        data = {
            'id': self.id,
            'name': self.name,
            'start_time': to_iso_z(self.start_time),
            'end_time': to_iso_z(self.end_time),
            'grace_seconds': self.grace_seconds,
            'source': self.source,
        }
        if now is not None:
            data['seconds_remaining'] = max(0, int((self.end_time - now).total_seconds()))
        return data


class PresenceEvent(db.Model):
    """One presence signal from any input (RFID tap, later camera),
    resolved to a user and a classroom. occurred_at is when it happened on
    the device; received_at is when the server got it. type is derived by
    the server: 'enter'/'exit' toggle within a period, or 'ignored' when the
    event falls outside every period window (logged, not counted)."""
    __tablename__ = 'presence_events'

    id = db.Column(db.Integer, primary_key=True)
    _event_id = db.Column(db.String(64), unique=True, nullable=False)
    _source = db.Column(db.String(10), nullable=False)  # 'rfid', later 'camera'
    _user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    _classroom_id = db.Column(db.Integer, db.ForeignKey('classrooms.id'), nullable=False)
    _period_id = db.Column(db.Integer, db.ForeignKey('presence_periods.id'), nullable=True)
    _type = db.Column(db.String(10), nullable=False)  # 'enter', 'exit', 'ignored'
    _occurred_at = db.Column(db.DateTime, nullable=False, index=True)
    _received_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    _tag_uid = db.Column(db.String(64), nullable=True)
    _device_id = db.Column(db.String(64), nullable=True)

    user = db.relationship('User')
    classroom = db.relationship('Classroom')
    period = db.relationship('PresencePeriod')

    def __init__(self, event_id, source, user_id, classroom_id, period_id, type,
                 occurred_at, received_at, tag_uid=None, device_id=None):
        self._event_id = event_id
        self._source = source
        self._user_id = user_id
        self._classroom_id = classroom_id
        self._period_id = period_id
        self._type = type
        self._occurred_at = occurred_at
        self._received_at = received_at
        self._tag_uid = tag_uid
        self._device_id = device_id

    @property
    def event_id(self): return self._event_id

    @property
    def user_id(self): return self._user_id

    @property
    def type(self): return self._type

    @property
    def occurred_at(self): return self._occurred_at

    def to_dict(self):
        return {
            'id': self.id,
            'event_id': self._event_id,
            'source': self._source,
            'uid': self.user.uid if self.user else None,
            'user_name': self.user.name if self.user else None,
            'classroom_id': self._classroom_id,
            'type': self._type,
            'period': self.period.name if self.period else None,
            'occurred_at': to_iso_z(self._occurred_at),
            'received_at': to_iso_z(self._received_at),
            'device_id': self._device_id,
        }


class CameraCheck(db.Model):
    """The camera's answer for one RFID tap: it took a picture at the tap
    station and reports whose face it saw. One check per tap; no images are
    stored, only the result."""
    __tablename__ = 'camera_checks'

    RESULTS = ('match', 'no_match', 'no_face', 'not_enrolled')

    id = db.Column(db.Integer, primary_key=True)
    _event_id = db.Column(db.String(64), unique=True, nullable=False)
    _tap_id = db.Column(db.Integer, db.ForeignKey('presence_events.id'), unique=True, nullable=False)
    _result = db.Column(db.String(16), nullable=False)
    _recognized_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    _confidence = db.Column(db.Float, nullable=True)
    _occurred_at = db.Column(db.DateTime, nullable=False)
    _received_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    _device_id = db.Column(db.String(64), nullable=True)

    tap = db.relationship('PresenceEvent')
    recognized_user = db.relationship('User')

    def __init__(self, event_id, tap_id, result, recognized_user_id, confidence,
                 occurred_at, received_at, device_id=None):
        self._event_id = event_id
        self._tap_id = tap_id
        self._result = result
        self._recognized_user_id = recognized_user_id
        self._confidence = confidence
        self._occurred_at = occurred_at
        self._received_at = received_at
        self._device_id = device_id

    @property
    def tap_id(self): return self._tap_id

    @property
    def verification(self):
        """VERIFIED: the face is the tag's owner. MISMATCH: someone else or an
        unknown face (possible proxy tap). NO_FACE: no face in the picture.
        TAP_ONLY: the student opted out of face scanning (not flagged)."""
        if self._result == 'match':
            return 'VERIFIED' if self._recognized_user_id == self.tap.user_id else 'MISMATCH'
        return {'no_match': 'MISMATCH', 'no_face': 'NO_FACE', 'not_enrolled': 'TAP_ONLY'}[self._result]

    def to_dict(self):
        return {
            'id': self.id,
            'event_id': self._event_id,
            'tap_event_id': self.tap.event_id if self.tap else None,
            'result': self._result,
            'recognized_uid': self.recognized_user.uid if self.recognized_user else None,
            'confidence': self._confidence,
            'verification': self.verification,
            'occurred_at': to_iso_z(self._occurred_at),
            'received_at': to_iso_z(self._received_at),
            'device_id': self._device_id,
        }
