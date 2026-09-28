#!/usr/bin/env python3

"""
db_migrate-prod2sqlite.py
Migrates ALL data from the production database to the local SQLite database.

Responsibility: authenticate → export from prod → save JSON backup →
                reset local schema → load data into local DB.

Steps:
  1. Warn + backup the existing local database
  2. Authenticate to the production server
  3. Export all data from production (with pagination)
  4. Save a JSON backup to instance/data.json
  5. Drop + recreate local schema, seed default data
  6. Load exported data into the local database

Usage:
  python scripts/db_migrate-prod2sqlite.py
  ./scripts/db_migrate-prod2sqlite.py
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from main import app, db, initUsers
from db_utils import (
    authenticate, filter_default_data, fetch_remote_counts, local_counts,
    report_reconciliation, BASE_URL, UID, PASSWORD,
)

# ── Configuration ──────────────────────────────────────────────────────────────

PERSISTENCE_PREFIX = "instance"
JSON_DATA = f"{PERSISTENCE_PREFIX}/data.json"

# Export endpoints (one per data type).
# Every table in model/ must appear here -- production runs db_init.py (which
# does a drop_all) before the data is pushed back, so a table missing from this
# map is destroyed on production and never restored.
EXPORT_ENDPOINTS = {
    'sections':               '/api/export/sections',
    'users':                  '/api/export/users',
    'topics':                 '/api/export/topics',
    'microblogs':             '/api/export/microblogs',
    'posts':                  '/api/export/posts',
    'classrooms':             '/api/export/classrooms',
    'feedback':               '/api/export/feedback',
    'study':                  '/api/export/study',
    'personas':               '/api/export/personas',
    'user_personas':          '/api/export/user_personas',
    'leaderboard':            '/api/export/leaderboard',
    'elementary_leaderboard': '/api/export/elementary_leaderboard',
    'skill_snapshots':        '/api/export/skill_snapshots',
}

# Data types that require paginated fetching
PAGINATED_TYPES = {
    'users', 'microblogs', 'posts', 'topics', 'personas', 'user_personas',
    'leaderboard', 'elementary_leaderboard', 'skill_snapshots',
}

# ── Target safety guard ───────────────────────────────────────────────────────

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def sqlite_path(uri):
    """Absolute on-disk path for a sqlite:/// URI, anchored to the repo root.

    PERSISTENCE_PREFIX ("instance") is injected here because Flask-SQLAlchemy
    resolves a relative sqlite:/// URI relative to app.instance_path, not the
    repo root or CWD -- confirmed against the actual on-disk db file, which
    really does live at instance/volumes/user_management.db even though the
    configured URI is sqlite:///volumes/user_management.db. A version of this
    function that stripped sqlite:/// without re-adding "instance/" would
    point at a path that doesn't exist.
    """
    return os.path.join(ROOT, uri.replace('sqlite:///', f"{PERSISTENCE_PREFIX}/"))


def assert_target_is_sqlite():
    """Refuse to run unless the app is bound to a local SQLite file.

    This script calls db.drop_all() on whatever database `db` is bound to, and
    __init__.py selects MySQL whenever DB_ENDPOINT / DB_USERNAME / DB_PASSWORD
    are set. A production .env sitting in the working directory therefore turns
    "pull prod down to sqlite" into "wipe prod", so check the target first.
    """
    db_string = app.config['SQLALCHEMY_DATABASE_STRING']
    if db_string.startswith('sqlite'):
        return

    print("ERROR: refusing to run - the target database is not SQLite.")
    print(f"  engine: {db_string.split(':', 1)[0] or 'unknown'}")
    print(f"  host:   {app.config['DB_ENDPOINT']}")
    print(f"  schema: {app.config['SQLALCHEMY_DATABASE_NAME']}")
    print()
    print("This script drops and recreates every table in the target, so running")
    print("it against that database would destroy it rather than populate a local")
    print("SQLite file.")
    print()
    print("DB_ENDPOINT / DB_USERNAME / DB_PASSWORD are being read from .env, and")
    print("__init__.py picks MySQL whenever all three are non-empty. Blank them for")
    print("this run so it falls back to sqlite:///volumes/ — note that `env -u` does")
    print("NOT work here, because load_dotenv() refills unset vars straight from .env:")
    print("  DB_ENDPOINT= DB_USERNAME= DB_PASSWORD= \\")
    print("      python scripts/db_migrate-prod2sqlite.py")
    sys.exit(1)


# ── Database backup / creation helpers ────────────────────────────────────────

BACKUP_DIR = os.path.join(ROOT, PERSISTENCE_PREFIX, 'backups')


def backup_database(db_uri, backup_uri, db_string):
    """Copy the local SQLite file aside.

    Returns True when a rollback point exists (or when there is nothing to lose
    yet). The caller must not drop tables when this returns False.
    """
    if not db_string.startswith('sqlite'):
        # assert_target_is_sqlite() runs first, so this is unreachable via main().
        print("ERROR: backups are only supported for SQLite targets.")
        return False

    db_path = sqlite_path(db_uri)
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        print(f"No existing data at {db_path}; nothing to back up.")
        return True

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    dump_path = os.path.join(BACKUP_DIR, f"{os.path.basename(db_path)}.{timestamp}.bak")

    try:
        shutil.copyfile(db_path, dump_path)
        if backup_uri:
            # Keep the configured *_bak.db convenience copy up to date too.
            shutil.copyfile(db_path, sqlite_path(backup_uri))
    except OSError as e:
        print(f"ERROR: could not back up {db_path}: {e}")
        return False

    size = os.path.getsize(dump_path)
    if size != os.path.getsize(db_path):
        print(f"ERROR: backup {dump_path} is truncated ({size} bytes).")
        return False

    print(f"SQLite database backed up to {dump_path} ({size} bytes)")
    print(f"Roll back with: cp {dump_path} {db_path}")
    return True


def create_database_if_missing(engine, db_name):
    """Create a MySQL database if it does not already exist."""
    with engine.connect() as connection:
        result = connection.execute(text(f"SHOW DATABASES LIKE '{db_name}'"))
        if not result.fetchone():
            connection.execute(text(f"CREATE DATABASE {db_name}"))
            print(f"Database '{db_name}' created successfully.")
        else:
            print(f"Database '{db_name}' already exists.")

# ── Production data extractor ──────────────────────────────────────────────────

def _fetch_paginated(url, data_type, headers, cookies):
    """Fetch all pages for *data_type* and return (records, error_entry_or_None)."""
    all_records = []
    page = 1
    per_page = 50
    max_retries = 3

    while True:
        paginated_url = f"{url}?page={page}&per_page={per_page}"
        retry_count = 0
        success = False

        while retry_count < max_retries and not success:
            try:
                response = requests.get(paginated_url, headers=headers, cookies=cookies, timeout=180)

                if response.status_code not in [200, 201]:
                    error_msg = f"HTTP {response.status_code}"
                    try:
                        error_data = response.json()
                        if 'message' in error_data:
                            error_msg += f": {error_data['message']}"
                    except Exception:
                        error_msg += f": {response.text[:100]}"

                    if response.status_code == 504:
                        retry_count += 1
                        if retry_count < max_retries:
                            print("R", end="", flush=True)
                            time.sleep(2)
                            continue

                    print(f"\nFAILED on page {page} ({error_msg})")
                    return all_records, (data_type, error_msg)

                result = response.json()
                page_records = result.get(data_type, [])
                if not page_records:
                    break

                all_records.extend(page_records)
                success = True
                print(".", end="", flush=True)

                if not result.get('has_next', False):
                    break

            except requests.Timeout:
                retry_count += 1
                if retry_count < max_retries:
                    print("T", end="", flush=True)
                    time.sleep(2)
                    continue
                print(f"\nTIMEOUT on page {page} after {max_retries} retries")
                return all_records, (data_type, "Request timed out")

            except requests.RequestException as e:
                print(f"\nERROR on page {page}: {str(e)[:50]}")
                return all_records, (data_type, str(e))

        if not success:
            break
        page += 1

    return all_records, None


def extract_all_data(cookies):
    """Export all data from production using chunked endpoints.

    Returns (all_data, error_or_None).
    """
    print("  Using paginated export endpoints (50 records per page)...")
    headers = {"Content-Type": "application/json", "X-Origin": "client"}

    all_data = {}
    total_records = 0
    failed_endpoints = []

    for data_type, endpoint in EXPORT_ENDPOINTS.items():
        url = BASE_URL + endpoint
        print(f"  Fetching {data_type}...", end=" ", flush=True)

        try:
            if data_type in PAGINATED_TYPES:
                records, failure = _fetch_paginated(url, data_type, headers, cookies)
                if failure:
                    failed_endpoints.append(failure)
                all_data[data_type] = records
                total_records += len(records)
                print(f" {len(records)} records")

            else:
                response = requests.get(url, headers=headers, cookies=cookies, timeout=120)

                if response.status_code not in [200, 201]:
                    error_msg = f"HTTP {response.status_code}"
                    try:
                        error_data = response.json()
                        if 'message' in error_data:
                            error_msg += f": {error_data['message']}"
                    except Exception:
                        error_msg += f": {response.text[:100]}"
                    print(f"FAILED ({error_msg})")
                    failed_endpoints.append((data_type, error_msg))
                    continue

                result = response.json()
                if data_type in result:
                    records = result[data_type]
                    all_data[data_type] = records
                    count = len(records) if isinstance(records, list) else 1
                    total_records += count
                    print(f"{count} records")
                else:
                    print("no data found in response")
                    all_data[data_type] = []

        except requests.Timeout:
            print("TIMEOUT")
            failed_endpoints.append((data_type, "Request timed out"))
        except requests.RequestException as e:
            print(f"ERROR ({str(e)[:50]})")
            failed_endpoints.append((data_type, str(e)))

    all_data['_metadata'] = {
        'total_records':    total_records,
        'tables':           list(EXPORT_ENDPOINTS.keys()),
        'failed_endpoints': failed_endpoints,
    }

    print(f"\n  Total records extracted: {total_records}")

    if failed_endpoints:
        print(f"  WARNING: {len(failed_endpoints)} endpoint(s) failed:")
        for dt, err in failed_endpoints:
            print(f"    - {dt}: {err}")

        # Production is wiped by db_init.py before this data is pushed back, so a
        # partial export is a partial restore. Every endpoint is critical.
        # Set ALLOW_PARTIAL_EXPORT=true only to inspect a broken endpoint.
        if os.environ.get('ALLOW_PARTIAL_EXPORT') != 'true':
            return None, {
                'message': f'Export incomplete: {", ".join(dt for dt, _ in failed_endpoints)}',
                'code': 500,
                'error': 'Refusing to continue with a partial export. '
                         'Fix the endpoint, or set ALLOW_PARTIAL_EXPORT=true to override.',
            }
        print("  ALLOW_PARTIAL_EXPORT=true - continuing with an INCOMPLETE export")

    return all_data, None

# ── JSON persistence ───────────────────────────────────────────────────────────

def write_data_to_json(data, json_file):
    """Write *data* to *json_file*, creating a timestamped backup if it exists."""
    if os.path.exists(json_file):
        timestamp   = datetime.now().strftime('%Y%m%d%H%M%S')
        backup_file = f"{json_file}.{timestamp}.bak"
        shutil.copyfile(json_file, backup_file)
        print(f"Existing JSON data backed up to {backup_file}")

    with open(json_file, 'w') as f:
        json.dump(data, f, indent=4)
    print(f"Data written to {json_file}")


def read_data_from_json(json_file):
    """Read data from *json_file* and return (data, error)."""
    if os.path.exists(json_file):
        with open(json_file, 'r') as f:
            return json.load(f), None
    return None, {'message': 'JSON data file not found', 'code': 404, 'error': 'File not found'}

# ── Local database loaders ─────────────────────────────────────────────────────

def load_sections(sections_data):
    from model.user import Section

    loaded = 0
    for section_data in sections_data:
        try:
            if Section.query.filter_by(_abbreviation=section_data.get('abbreviation')).first():
                print(f"  Section '{section_data.get('abbreviation')}' already exists, skipping.")
                continue
            Section(
                name=section_data.get('name'),
                abbreviation=section_data.get('abbreviation'),
            ).create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading section {section_data.get('abbreviation')}: {e}")
    print(f"  Loaded {loaded} sections.")


def load_users(users_data):
    from model.user import User, Section

    loaded = skipped = 0
    for user_data in users_data:
        try:
            uid = user_data.get('uid')
            if User.query.filter_by(_uid=uid).first():
                skipped += 1
                continue

            user = User(
                name=user_data.get('name'),
                uid=uid,
                password=user_data.get('password', ''),
                sid=user_data.get('sid'),
                role=user_data.get('role', 'User'),
                pfp=user_data.get('pfp'),
                kasm_server_needed=user_data.get('kasm_server_needed', False),
                grade_data=user_data.get('grade_data') or user_data.get('gradeData'),
                ap_exam=user_data.get('ap_exam') or user_data.get('apExam'),
                school=user_data.get('school'),
                classes=user_data.get('class') or user_data.get('_class'),
                game_profile=user_data.get('game_profile') or user_data.get('gameProfile'),
            )
            if user_data.get('email'):
                user.email = user_data['email']

            for section_data in user_data.get('sections', []):
                abbrev = section_data.get('abbreviation')
                if abbrev:
                    section = Section.query.filter_by(_abbreviation=abbrev).first()
                    if section:
                        user.sections.append(section)

            user.create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading user {user_data.get('uid')}: {e}")

    if skipped:
        print(f"  Skipped {skipped} users (already exist)")
    print(f"  Loaded {loaded} users.")


def load_topics(topics_data):
    from model.microblog import Topic

    loaded = 0
    for topic_data in topics_data:
        try:
            page_path = topic_data.get('pagePath') or topic_data.get('page_path')
            if not page_path:
                print("  Skipping topic - no page_path")
                continue
            if Topic.query.filter_by(_page_path=page_path).first():
                print(f"  Topic '{page_path}' already exists, skipping.")
                continue
            Topic(
                page_path=page_path,
                page_title=topic_data.get('pageTitle') or topic_data.get('page_title'),
                page_description=topic_data.get('pageDescription') or topic_data.get('page_description'),
                display_name=topic_data.get('displayName') or topic_data.get('display_name'),
                color=topic_data.get('color', '#007bff'),
                icon=topic_data.get('icon'),
                allow_anonymous=topic_data.get('allowAnonymous') or topic_data.get('allow_anonymous', False),
                moderated=topic_data.get('moderated', False),
                max_posts_per_user=topic_data.get('maxPostsPerUser') or topic_data.get('max_posts_per_user', 10),
                settings=topic_data.get('settings', {}),
            ).create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading topic: {e}")
    print(f"  Loaded {loaded} topics.")


def load_microblogs(microblogs_data, user_uid_map=None):
    from model.microblog import MicroBlog, Topic
    from model.user import User

    loaded = skipped = 0
    for mb_data in microblogs_data:
        try:
            user_uid   = mb_data.get('userUid') or mb_data.get('user', {}).get('uid')
            old_user_id = mb_data.get('userId') or mb_data.get('user_id')

            user = User.query.filter_by(_uid=user_uid).first() if user_uid else None
            if not user and old_user_id and user_uid_map:
                mapped_uid = user_uid_map.get(old_user_id)
                if mapped_uid:
                    user = User.query.filter_by(_uid=mapped_uid).first()
            if not user:
                skipped += 1
                continue

            topic_id = None
            topic_path = mb_data.get('topicPath') or mb_data.get('topicKey') or mb_data.get('topic', {}).get('page_path')
            if topic_path:
                topic = Topic.query.filter_by(_page_path=topic_path).first()
                if topic:
                    topic_id = topic.id

            content = mb_data.get('content')
            if not content:
                skipped += 1
                continue

            MicroBlog(
                user_id=user.id,
                content=content,
                topic_id=topic_id,
                data=mb_data.get('data', {}),
            ).create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading microblog: {e}")
            skipped += 1

    if skipped:
        print(f"  Skipped {skipped} microblogs (user not found or invalid)")
    print(f"  Loaded {loaded} microblogs.")


def load_posts(posts_data, user_uid_map=None):
    from model.post import Post
    from model.user import User

    id_mapping = {}
    loaded = skipped = 0

    top_level = [p for p in posts_data if not (p.get('parent_id') or p.get('parentId'))]
    replies   = [p for p in posts_data if p.get('parent_id') or p.get('parentId')]

    def _find_user(post_data):
        old_user_id  = post_data.get('userId') or post_data.get('user_id')
        student_name = post_data.get('studentName')
        user = None
        if old_user_id and user_uid_map:
            mapped_uid = user_uid_map.get(old_user_id)
            if mapped_uid:
                user = User.query.filter_by(_uid=mapped_uid).first()
        if not user and student_name and student_name != 'Unknown':
            user = User.query.filter_by(_name=student_name).first()
        return user

    for post_data in top_level:
        try:
            user = _find_user(post_data)
            if not user:
                skipped += 1
                continue
            post = Post(
                user_id=user.id,
                content=post_data.get('content'),
                grade_received=post_data.get('gradeReceived') or post_data.get('grade_received'),
                page_url=post_data.get('pageUrl') or post_data.get('page_url'),
                page_title=post_data.get('pageTitle') or post_data.get('page_title'),
            )
            created = post.create()
            if created:
                old_id = post_data.get('id')
                if old_id:
                    id_mapping[old_id] = created.id
                loaded += 1
        except Exception as e:
            print(f"  Error loading post: {e}")
            skipped += 1

    for reply_data in replies:
        try:
            user = _find_user(reply_data)
            if not user:
                skipped += 1
                continue
            old_parent_id = reply_data.get('parentId') or reply_data.get('parent_id')
            new_parent_id = id_mapping.get(old_parent_id)
            if not new_parent_id:
                skipped += 1
                continue
            Post(
                user_id=user.id,
                content=reply_data.get('content'),
                parent_id=new_parent_id,
            ).create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading reply: {e}")
            skipped += 1

    if skipped:
        print(f"  Skipped {skipped} posts/replies (user not found or invalid parent)")
    print(f"  Loaded {loaded} posts/replies.")


def load_classrooms(classrooms_data, user_uid_map=None):
    from model.classroom import Classroom
    from model.user import User

    loaded = skipped = 0
    for classroom_data in classrooms_data:
        try:
            owner_uid = classroom_data.get('ownerUid')
            owner = User.query.filter_by(_uid=owner_uid).first() if owner_uid else None
            if not owner:
                skipped += 1
                continue

            classroom = Classroom(
                name=classroom_data.get('name'),
                school_name=classroom_data.get('school_name') or classroom_data.get('schoolName'),
                owner_teacher_id=owner.id,
                status=classroom_data.get('status', 'active'),
            )
            classroom.create()

            for student_uid in classroom_data.get('studentUids', []):
                student = User.query.filter_by(_uid=student_uid).first()
                if student:
                    classroom.students.append(student)

            db.session.commit()
            loaded += 1
        except Exception as e:
            print(f"  Error loading classroom: {e}")
            skipped += 1

    if skipped:
        print(f"  Skipped {skipped} classrooms (owner not found)")
    print(f"  Loaded {loaded} classrooms.")


def load_feedback(feedback_data):
    from model.feedback import Feedback

    loaded = 0
    for fb_data in feedback_data:
        try:
            feedback = Feedback(
                title=fb_data.get('title'),
                body=fb_data.get('body'),
                type=fb_data.get('type', 'Other'),
                github_username=fb_data.get('github_username'),
            )
            feedback.github_issue_url = fb_data.get('github_issue_url')
            feedback.create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading feedback: {e}")
    print(f"  Loaded {loaded} feedback records.")


def load_study(study_data, user_uid_map=None):
    from model.study import Study
    from model.user import User

    loaded = skipped = 0
    for study_record in study_data:
        try:
            user_uid = study_record.get('userUid')
            user = User.query.filter_by(_uid=user_uid).first() if user_uid else None

            Study(
                user_id=user.id if user else None,
                topic=study_record.get('topic'),
                subtopic=study_record.get('subtopic'),
                studied=study_record.get('studied', False),
                timestamp=study_record.get('timestamp'),
            ).create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading study record: {e}")
            skipped += 1

    if skipped:
        print(f"  Skipped {skipped} study records")
    print(f"  Loaded {loaded} study records.")


def load_personas(personas_data):
    from model.persona import Persona

    loaded = 0
    for persona_data in personas_data:
        try:
            if Persona.query.filter_by(_alias=persona_data.get('alias')).first():
                continue
            Persona(
                _alias=persona_data.get('alias'),
                _category=persona_data.get('category'),
                _bio_map=persona_data.get('bio_map') or persona_data.get('bioMap'),
                _empathy_map=persona_data.get('empathy_map') or persona_data.get('empathyMap'),
            ).create()
            loaded += 1
        except Exception as e:
            print(f"  Error loading persona: {e}")
    print(f"  Loaded {loaded} personas.")


def load_user_personas(user_personas_data):
    from model.persona import Persona, UserPersona
    from model.user import User

    loaded = skipped = 0
    for up_data in user_personas_data:
        try:
            user_uid      = up_data.get('userUid')
            persona_alias = up_data.get('personaAlias')
            user   = User.query.filter_by(_uid=user_uid).first()    if user_uid      else None
            persona = Persona.query.filter_by(_alias=persona_alias).first() if persona_alias else None

            if not user or not persona:
                skipped += 1
                continue
            if UserPersona.query.filter_by(user_id=user.id, persona_id=persona.id).first():
                continue

            db.session.add(UserPersona(user=user, persona=persona, weight=up_data.get('weight', 1)))
            db.session.commit()
            loaded += 1
        except Exception as e:
            print(f"  Error loading user-persona association: {e}")
            skipped += 1

    if skipped:
        print(f"  Skipped {skipped} user-persona associations")
    print(f"  Loaded {loaded} user-persona associations.")


def _parse_dt(value):
    """Parse an ISO-8601 timestamp from an export payload; None if unusable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def load_events(events_data, model, label):
    """Load leaderboard events, preserving their original timestamps."""
    from model.user import User

    loaded = skipped = 0
    for event_data in events_data:
        try:
            user_uid = event_data.get('userUid')
            user = User.query.filter_by(_uid=user_uid).first() if user_uid else None

            # user_id is nullable: an event whose author is gone is still history.
            event = model(
                payload=event_data.get('payload') or {},
                user_id=user.id if user else None,
            )
            timestamp = _parse_dt(event_data.get('timestamp'))
            if timestamp:
                event._timestamp = timestamp

            db.session.add(event)
            db.session.commit()
            loaded += 1
        except Exception as e:
            db.session.rollback()
            print(f"  Error loading {label} event: {e}")
            skipped += 1

    if skipped:
        print(f"  Skipped {skipped} {label} events")
    print(f"  Loaded {loaded} {label} events.")


def load_leaderboard(events_data):
    from model.leaderboard import ScoreCounterEvent
    load_events(events_data, ScoreCounterEvent, 'leaderboard')


def load_elementary_leaderboard(events_data):
    from model.leaderboard import ElementaryLeaderboardEvent
    load_events(events_data, ElementaryLeaderboardEvent, 'elementary leaderboard')


def load_skill_snapshots(snapshots_data):
    from model.skill_snapshot import SkillSnapshot
    from model.user import User

    loaded = skipped = 0
    for snapshot_data in snapshots_data:
        try:
            user_uid = snapshot_data.get('userUid')
            user = User.query.filter_by(_uid=user_uid).first() if user_uid else None
            if not user:
                # user_id is NOT NULL, so an orphan snapshot cannot be stored.
                print(f"  Skipping skill snapshot - no user for uid {user_uid!r}")
                skipped += 1
                continue

            snapshot = SkillSnapshot(
                user_id=user.id,
                project_name=snapshot_data.get('project_name'),
                coding_ability=snapshot_data.get('coding_ability'),
                collaboration=snapshot_data.get('collaboration'),
                problem_solving=snapshot_data.get('problem_solving'),
                initiative=snapshot_data.get('initiative'),
            )
            snapshot_date = _parse_dt(snapshot_data.get('snapshot_date'))
            if snapshot_date:
                snapshot.snapshot_date = snapshot_date

            db.session.add(snapshot)
            db.session.commit()
            loaded += 1
        except Exception as e:
            db.session.rollback()
            print(f"  Error loading skill snapshot: {e}")
            skipped += 1

    if skipped:
        print(f"  Skipped {skipped} skill snapshots")
    print(f"  Loaded {loaded} skill snapshots.")


def load_all(all_data):
    """Load all data types into the local database in dependency order."""
    user_uid_map = {u.get('id'): u.get('uid') for u in all_data.get('users', []) if u.get('id') and u.get('uid')}
    print(f"\nBuilt user_uid_map with {len(user_uid_map)} entries")

    loaders = [
        ('sections',      lambda: load_sections(all_data.get('sections', []))),
        ('users',         lambda: load_users(all_data.get('users', []))),
        ('topics',        lambda: load_topics(all_data.get('topics', []))),
        ('microblogs',    lambda: load_microblogs(all_data.get('microblogs', []), user_uid_map)),
        ('posts',         lambda: load_posts(all_data.get('posts', []), user_uid_map)),
        ('personas',      lambda: load_personas(all_data.get('personas', []))),
        ('user_personas', lambda: load_user_personas(all_data.get('user_personas', []))),
        ('classrooms',    lambda: load_classrooms(all_data.get('classrooms', []), user_uid_map)),
        ('feedback',      lambda: load_feedback(all_data.get('feedback', []))),
        ('study',         lambda: load_study(all_data.get('study', []), user_uid_map)),
        ('leaderboard',   lambda: load_leaderboard(all_data.get('leaderboard', []))),
        ('elementary_leaderboard',
                          lambda: load_elementary_leaderboard(all_data.get('elementary_leaderboard', []))),
        ('skill_snapshots',
                          lambda: load_skill_snapshots(all_data.get('skill_snapshots', []))),
    ]

    for name, loader in loaders:
        data = all_data.get(name, [])
        if data:
            print(f"\nLoading {len(data)} {name}...")
            loader()

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    # Step 0: Confirm the target is local, warn, and back up existing database
    assert_target_is_sqlite()

    with app.app_context():
        try:
            inspector = db.inspect(db.engine)
            if inspector.get_table_names():
                target = sqlite_path(app.config['SQLALCHEMY_DATABASE_URI'])
                print(f"Warning: every table in {target} is about to be dropped")
                print("and rebuilt from production data.")
                if os.getenv('FORCE_YES') == 'true':
                    response = 'y'
                else:
                    print("Do you want to continue? (y/n)")
                    response = input()
                if response.lower() != 'y':
                    print("Exiting without making changes.")
                    sys.exit(0)

            backed_up = backup_database(
                app.config['SQLALCHEMY_DATABASE_URI'],
                app.config['SQLALCHEMY_BACKUP_URI'],
                app.config['SQLALCHEMY_DATABASE_STRING'],
            )
            if not backed_up and os.getenv('ALLOW_NO_BACKUP') != 'true':
                print("\nRefusing to drop the database without a rollback point.")
                print("Fix the backup above, or set ALLOW_NO_BACKUP=true to override.")
                sys.exit(1)

        except OperationalError as e:
            if "Unknown database" in str(e):
                engine = create_engine(app.config['SQLALCHEMY_DATABASE_STRING'])
                create_database_if_missing(engine, app.config['SQLALCHEMY_DATABASE_NAME'])
                with app.app_context():
                    db.create_all()
                    print("All tables created after database creation.")
            else:
                print(f"An error occurred: {e}")
                sys.exit(1)
        except Exception as e:
            print(f"An error occurred: {e}")
            sys.exit(1)

    # Step 1: Authenticate
    print("\n=== Step 1: Authenticating to production server ===")
    cookies, error = authenticate(UID, PASSWORD)
    if error:
        print(error)
        print("Using local JSON data as fallback.")
        all_data, error = read_data_from_json(JSON_DATA)
        if error or all_data is None:
            print(f"Error: {error}")
            print("\nCannot proceed: Authentication failed and no local backup data available.")
            sys.exit(1)
    else:
        # Step 2: Export from production
        print("\n=== Step 2: Extracting ALL data from production ===")
        all_data, errors = extract_all_data(cookies)
        if errors:
            print(f"Error extracting data: {errors}")
            print("Falling back to local JSON data if available...")
            all_data, error = read_data_from_json(JSON_DATA)
            if error or all_data is None:
                print(f"Error: {error}")
                print("\nCannot proceed: Export failed and no local backup data available.")
                sys.exit(1)
        else:
            write_data_to_json(all_data, JSON_DATA)

    print("\n=== Data extraction complete ===")
    if not all_data or not isinstance(all_data, dict):
        print(f"Error: No usable data was extracted (got {type(all_data).__name__})")
        sys.exit(1)

    for key, data in all_data.items():
        count = len(data) if isinstance(data, list) else 1
        print(f"  {key}: {count if data else 'No'} records")

    # Count production's own copies of the seed users before they are filtered
    # out, so the verification step can account for them.
    from db_utils import DEFAULT_DATA
    seed_uids = [uid for uid in DEFAULT_DATA['users'] if uid]
    prod_seed_users = sum(
        1 for u in (all_data.get('users') or []) if u.get('uid') in seed_uids
    )

    # Filter default seed data
    print("\n=== Filtering out default/test data ===")
    all_data = filter_default_data(all_data)

    print("\n=== Data after filtering ===")
    for key, data in all_data.items():
        count = len(data) if isinstance(data, list) else 1
        print(f"  {key}: {count if data else 'No'} records")

    # Step 3: Reset schema and load data
    print("\n=== Step 3: Building new schema and loading data ===")
    try:
        with app.app_context():
            # Re-check immediately before the destructive call.
            assert_target_is_sqlite()
            db.drop_all()
            print("All tables dropped.")
            db.create_all()
            print("All tables created.")
            initUsers()
            load_all(all_data)
    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Step 4: Verify the local database matches production table by table
    print("\n=== Step 4: Verifying the pull is complete ===")
    complete = verify_pull(cookies, prod_seed_users)

    if complete:
        print("\n=== Database initialized successfully - all tables reconciled ===")
        return

    print("\n=== WARNING: local database does NOT match production ===")
    print("Do NOT push this back to production: the missing rows would be lost.")
    print("Investigate the tables marked MISSING above, then re-run this script.")
    sys.exit(1)


def verify_pull(cookies, prod_seed_users):
    """Compare production row counts against the freshly loaded local database."""
    if cookies is None:
        print("  Skipped: data came from the local JSON fallback, not production.")
        return True

    remote, err = fetch_remote_counts(cookies)
    if err:
        print(f"  Could not fetch production counts: {err}")
        print("  (/api/export/counts ships with this change - is production running it yet?)")
        return False

    with app.app_context():
        local = local_counts()

    # filter_default_data() dropped production's seed users and initUsers()
    # recreated them locally, so that many users are legitimately "missing".
    from db_utils import DEFAULT_DATA
    seeded_locally = len({uid for uid in DEFAULT_DATA['users'] if uid})
    expected_deficit = {'users': prod_seed_users - seeded_locally}

    return report_reconciliation(remote, local, 'production', 'local', expected_deficit)


if __name__ == "__main__":
    main()
