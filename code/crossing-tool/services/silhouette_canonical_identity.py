"""Research-only persistent silhouette registration; no production publication.

Initial ordering is frame, legacy ordinal, field, exact label, legacy reference,
original path. Subsequent registration appends numbers, never reranks bindings.
Callers must retain registration_key/instance_id across classification/path edits.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from data.media_id import instance_identifier, label_slug


RESEARCH = Path("outputs/tests/silhouette-canonical-identity")
POLICY = "frame-ordinal-field-label-reference-path-v1"


def research_path(project, path):
    project = Path(project).resolve()
    path = Path(path).resolve()
    if not path.is_relative_to(project / RESEARCH):
        raise ValueError("Registry and reports must stay in canonical-identity research outputs")
    return path


def proposed_artifact_directory(kind, media_type, field, slug, instance_id):
    """Historical research proposal only; use data.resource_locations for new destinations."""
    if kind not in ("silhouettes", "engravings") or media_type not in ("movie", "gameplay"):
        raise ValueError("Invalid artifact kind/media")
    shot, separator, number = instance_id.rpartition("#")
    if not separator or not number.isascii() or not number.isdigit():
        raise ValueError("Invalid instance identifier")
    if instance_identifier(shot, int(number)) != instance_id:
        raise ValueError("Noncanonical instance identifier")
    for component in (field, slug):
        if (not component or component in (".", "..")
                or any(character in component for character in '/\\\x00<>:"|?*')
                or any(ord(character) < 32 for character in component)
                or component.endswith((".", " "))):
            raise ValueError("Unsafe classification directory")
    path = Path("data") / kind / media_type / field / slug / instance_id
    if any(len(component.encode("utf-8")) > 255 for component in path.parts):
        raise ValueError("Destination component exceeds 255 bytes")
    return path.as_posix()


def prepare_registration(record, project):
    from data.media_id import build_shot_id, parse_shot_id
    from services.silhouette_catalog import assignment_is_active, catalog_object_reference
    if not assignment_is_active(record):
        raise ValueError("Only active silhouettes enter initial registration")
    required = ("media_type", "media_id", "shot_id", "field", "label", "filename_stem", "path")
    if any(not isinstance(record.get(key), (str, Path)) or not str(record[key]).strip() for key in required):
        raise ValueError("Missing identity or classification components")
    shot = record["shot_id"]
    media, start, end = parse_shot_id(shot)
    if media != record["media_id"] or build_shot_id(media, start, end) != shot:
        raise ValueError("Shot/media identity mismatch")
    instance_identifier(shot, 1)
    if record["media_type"] not in ("movie", "gameplay"):
        raise ValueError("Unknown media type")
    frame = record.get("frame")
    if type(frame) is not int or not start <= frame <= end:
        raise ValueError("Missing or out-of-shot source frame")
    path = Path(record["path"])
    project = Path(project).resolve()
    if path.is_absolute():
        path = path.relative_to(project)
    if not path.as_posix().startswith("data/silhouettes/catalog/") or ".." in path.parts:
        raise ValueError("Initial binding requires original catalog reference")
    reference = catalog_object_reference(record, path)
    ordinal = re.fullmatch(r"object_(\d+)", reference["object_id"])
    if ordinal is None:
        raise ValueError("Unrecognized legacy object ordinal")
    legacy = json.dumps(reference, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    key = hashlib.sha256(json.dumps([legacy, path.as_posix()], ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()
    return {
        "registration_key": key, "shot_id": shot, "media_type": record["media_type"],
        "media_id": media, "legacy_identity": legacy, "legacy_json": path.as_posix(),
        "legacy_png": path.with_suffix(".png").as_posix(), "frame": frame,
        "ordinal": int(ordinal[1]), "field": record["field"], "label": record["label"],
    }


def initial_order(binding):
    return tuple(binding[key] for key in (
        "shot_id", "frame", "ordinal", "field", "label", "legacy_identity", "legacy_json",
    ))


class InstanceRegistry:
    """Persist immutable record bindings and non-recycling shot high-water marks."""

    def __init__(self, project, path):
        self.path = research_path(project, path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.database = sqlite3.connect(self.path, timeout=300)
        self.database.execute("PRAGMA synchronous=FULL")
        self.database.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS shots(shot_id TEXT PRIMARY KEY,high_water INTEGER NOT NULL CHECK(high_water>=0));
        CREATE TABLE IF NOT EXISTS instances(
          registration_key TEXT PRIMARY KEY,instance_id TEXT NOT NULL UNIQUE,
          shot_id TEXT NOT NULL,instance_number INTEGER NOT NULL CHECK(instance_number>0),
          media_type TEXT NOT NULL,media_id TEXT NOT NULL,legacy_identity TEXT NOT NULL,
          legacy_json TEXT NOT NULL UNIQUE,legacy_png TEXT NOT NULL,
          registration_state TEXT NOT NULL DEFAULT 'registered' CHECK(registration_state IN ('registered','retired')),
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          UNIQUE(shot_id,instance_number),FOREIGN KEY(shot_id) REFERENCES shots(shot_id));
        CREATE TRIGGER IF NOT EXISTS preserve_instances BEFORE DELETE ON instances
          BEGIN SELECT RAISE(ABORT,'Registered identities cannot be deleted'); END;
        CREATE TRIGGER IF NOT EXISTS preserve_binding BEFORE UPDATE OF registration_key,instance_id,shot_id,instance_number,media_type,media_id,legacy_identity,legacy_json,legacy_png ON instances
          BEGIN SELECT RAISE(ABORT,'Original identity bindings are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS preserve_shots BEFORE DELETE ON shots
          BEGIN SELECT RAISE(ABORT,'Shot reservations cannot be deleted'); END;
        CREATE TRIGGER IF NOT EXISTS preserve_high_water BEFORE UPDATE OF high_water ON shots
          WHEN NEW.high_water<OLD.high_water BEGIN SELECT RAISE(ABORT,'Ordinals cannot be recycled'); END;
        """)
        self.database.execute("PRAGMA foreign_keys=ON")
        existing = dict(self.database.execute("SELECT key,value FROM meta"))
        expected = {"schema_version": "1", "policy": POLICY, "project": str(Path(project).resolve())}
        if existing and existing != expected:
            self.database.close()
            raise ValueError("Registry version, policy or project mismatch")
        self.database.executemany("INSERT OR IGNORE INTO meta VALUES(?,?)", expected.items())
        self.database.commit()
        self.transactions = 0

    def close(self):
        self.database.close()

    def register(self, bindings, *, ordered=False, interrupt=None):
        """One transaction; unchanged bindings return their prior IDs.

        `ordered` is for a deterministic externally sorted stream. Ordinary
        small callers may pass unordered inputs. An explicit registration key
        is the binding token for later edits/moves, not their current path.
        """
        bindings = bindings if ordered else sorted(bindings, key=initial_order)
        result = []
        self.database.execute("BEGIN IMMEDIATE")
        try:
            for binding in bindings:
                from data.media_id import parse_shot_id
                media, _, _ = parse_shot_id(binding["shot_id"])
                if (media != binding["media_id"] or binding["media_type"] not in ("movie", "gameplay")
                        or re.fullmatch(r"[0-9a-f]{64}", binding["registration_key"]) is None):
                    raise ValueError("Invalid persistent registration components")
                instance_identifier(binding["shot_id"], 1)
                key = binding["registration_key"]
                row = self.database.execute(
                    "SELECT instance_id,shot_id,media_type,media_id FROM instances WHERE registration_key=?", (key,),
                ).fetchone()
                if row:
                    if row[1:] != (binding["shot_id"], binding["media_type"], binding["media_id"]):
                        raise ValueError("Registration key cannot change source shot/media")
                    instance = row[0]
                    new = False
                else:
                    self.database.execute("INSERT OR IGNORE INTO shots VALUES(?,0)", (binding["shot_id"],))
                    self.database.execute("UPDATE shots SET high_water=high_water+1 WHERE shot_id=?", (binding["shot_id"],))
                    number = self.database.execute("SELECT high_water FROM shots WHERE shot_id=?", (binding["shot_id"],)).fetchone()[0]
                    instance = instance_identifier(binding["shot_id"], number)
                    self.database.execute(
                        "INSERT INTO instances(registration_key,instance_id,shot_id,instance_number,media_type,media_id,legacy_identity,legacy_json,legacy_png) VALUES(?,?,?,?,?,?,?,?,?)",
                        (key, instance, binding["shot_id"], number, binding["media_type"], binding["media_id"],
                         binding["legacy_identity"], binding["legacy_json"], binding["legacy_png"]),
                    )
                    new = True
                result.append((key, instance, new))
                if interrupt is not None:
                    interrupt(len(result))
            self.database.commit()
            self.transactions += 1
        except BaseException:
            self.database.rollback()
            raise
        return result

    def retire(self, registration_key):
        with self.database:
            cursor = self.database.execute("UPDATE instances SET registration_state='retired' WHERE registration_key=?", (registration_key,))
            if cursor.rowcount != 1:
                raise ValueError("Unknown registration key")

    def lookup(self, registration_key):
        return self.database.execute("SELECT instance_id FROM instances WHERE registration_key=?", (registration_key,)).fetchone()