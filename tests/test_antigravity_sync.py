"""Gate tests for the Antigravity desktop sync in arena.py. Deterministic, local, no network, no LLM calls.

Run from the project folder:

    python3 -m unittest discover -s tests -v

Each test builds throwaway "CLI" and "desktop" Antigravity folders and a throwaway arena.db in a temp
directory. Nothing under ~/.gemini and no real arena.db is touched.

What they pin down (each was a real defect found in review):
  1. The desktop copy of a conversation database is written through SQLite, in place. The file is never
     swapped underneath an app that has it open in WAL mode, and a leftover -wal file cannot be replayed
     onto the new content.
  2. Turns added in the desktop app are never overwritten by a later sync.
  3. A project file with an unexpected shape cannot raise out of ensure_antigravity_project, and a tool
     thread that fails before starting still ends the round.
  4. Writing a title keeps the other fields of an existing annotation file.
  5. /api/continue answers with JSON when opening the app times out.
  6. Two rounds starting together create one project for a folder, not two.
"""
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_IMPORT_TMP = tempfile.mkdtemp(prefix="arena-test-import-")
os.environ.update(ARENA_DB=os.path.join(_IMPORT_TMP, "arena.db"), ARENA_CONFIG=os.path.join(_IMPORT_TMP, "cfg.json"),
                  ARENA_ATTACH=os.path.join(_IMPORT_TMP, "attachments"), ARENA_MODELS=os.path.join(_IMPORT_TMP, "models.json"))
sys.path.insert(0, str(ROOT))
with mock.patch("shutil.which", return_value=None):  # skip the PATH probing (`npm prefix -g`) done at import time
    import arena  # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


def make_conversation(path, steps):
    """A conversation database shaped like Antigravity's: WAL mode, a steps table that grows, one metadata row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE steps(idx integer PRIMARY KEY, data blob)")
    conn.execute("CREATE TABLE trajectory_meta(trajectory_id text PRIMARY KEY, cascade_id text, trajectory_type integer, source integer)")
    conn.execute("CREATE TABLE trajectory_metadata_blob(id text PRIMARY KEY, data blob)")
    conn.execute("INSERT INTO trajectory_meta VALUES(?, ?, 1, 1)", (SID, SID))
    conn.execute("INSERT INTO trajectory_metadata_blob VALUES('main', 'meta')")
    conn.executemany("INSERT INTO steps(data) VALUES(?)", [(s,) for s in steps])
    conn.commit()
    conn.close()


def add_step(path, text):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO steps(data) VALUES(?)", (text,))
    conn.execute("UPDATE trajectory_metadata_blob SET data=? WHERE id='main'", ("meta after " + text,))
    conn.commit()
    conn.close()


def read_steps(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT data FROM steps ORDER BY idx")]
    finally:
        conn.close()


def integrity(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


class ArenaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arena-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A fake home directory: every Path.home() lookup in arena.py lands in the temp folder, so no helper,
        # present or future, can reach the real ~/.gemini from a test. Network calls are refused outright
        # (the sync can talk to a running Antigravity Desktop over local HTTP).
        home = self.tmp / "home"
        gemini = home / ".gemini"
        self.cli, self.desk, self.projects = gemini / "antigravity-cli", gemini / "antigravity", gemini / "config" / "projects"
        db = self.tmp / "arena.db"
        conn = sqlite3.connect(db)
        conn.executescript(arena.SCHEMA)
        conn.close()
        for patcher in (mock.patch("pathlib.Path.home", return_value=home),
                        mock.patch("urllib.request.urlopen", side_effect=OSError("network is off in tests")),
                        # the title retry sleeps while waiting for a desktop app that is not there in tests
                        mock.patch.object(arena, "_antigravity_hub_set_title", return_value=True, create=True),
                        mock.patch.object(arena, "DB_PATH", db)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(arena._antigravity_dirs(), (self.cli, self.desk, self.projects))
        self.src = self.cli / "conversations" / f"{SID}.db"
        self.dst = self.desk / "conversations" / f"{SID}.db"
        self.folder = self.tmp / "work"
        self.folder.mkdir()

    def sync(self, **kw):
        return arena.sync_antigravity_session(SID, folder=str(self.folder), title="A question", **kw)


class SyncWritesThroughSqlite(ArenaCase):
    def test_first_sync_creates_the_desktop_copy(self):
        make_conversation(self.src, ["q1", "a1"])
        ok, info, pid, conflict = self.sync()
        self.assertTrue(ok, info)
        self.assertFalse(conflict)
        self.assertTrue(pid)
        self.assertEqual(read_steps(self.dst), ["q1", "a1"])
        self.assertEqual(integrity(self.dst), "ok")
        self.assertEqual(self.dst.stat().st_mode & 0o777, 0o600)
        self.assertIn(SID, arena.get_setting("antigravity_sync_state", {}))

    def test_desktop_app_holding_the_database_open_sees_the_update_in_the_same_file(self):
        make_conversation(self.src, ["q1", "a1"])
        self.assertTrue(self.sync()[0])
        desktop = sqlite3.connect(self.dst)  # the desktop app: an open WAL connection that has read the conversation
        self.addCleanup(desktop.close)
        self.assertEqual(desktop.execute("SELECT count(*) FROM steps").fetchone()[0], 2)
        inode = self.dst.stat().st_ino
        add_step(self.src, "q2")
        ok, info, _, conflict = self.sync()
        self.assertTrue(ok, info)
        self.assertFalse(conflict)
        self.assertEqual(self.dst.stat().st_ino, inode, "the database file was swapped under an open connection")
        self.assertEqual([r[0] for r in desktop.execute("SELECT data FROM steps ORDER BY idx")], ["q1", "a1", "q2"])
        self.assertEqual(read_steps(self.dst), ["q1", "a1", "q2"])
        self.assertEqual(integrity(self.dst), "ok")

    def test_leftover_wal_of_another_database_is_not_replayed_onto_the_copy(self):
        make_conversation(self.src, ["q1", "a1"])
        other = self.tmp / "other.db"  # an unrelated WAL database with committed frames that were never checkpointed
        conn = sqlite3.connect(other)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE steps(idx integer PRIMARY KEY, data blob)")
        conn.executemany("INSERT INTO steps(data) VALUES(?)", [("STALE",)] * 50)
        conn.commit()
        self.dst.parent.mkdir(parents=True)
        for ext in ("-wal", "-shm"):  # the state found on disk during review: sidecar files with no main database
            shutil.copy(str(other) + ext, str(self.dst) + ext)
        conn.close()
        self.assertGreater(os.path.getsize(str(self.dst) + "-wal"), 0)
        ok, info, _, _ = self.sync()
        self.assertTrue(ok, info)
        self.assertEqual(read_steps(self.dst), ["q1", "a1"])
        self.assertEqual(integrity(self.dst), "ok")

    def test_locked_desktop_copy_reports_failure_instead_of_hanging(self):
        make_conversation(self.src, ["q1"])
        self.assertTrue(self.sync()[0])
        add_step(self.src, "q2")
        holder = sqlite3.connect(self.dst, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")  # another program is in the middle of a write
        with mock.patch.object(arena, "_sqlite_backup_into",
                               lambda s, d: arena.__dict__["_sqlite_backup_into_real"](s, d, wait=0)):
            ok, info, _, conflict = self.sync()
        self.assertFalse(ok)
        self.assertFalse(conflict)
        self.assertIn("locked", info)
        holder.execute("ROLLBACK")
        self.assertEqual(read_steps(self.dst), ["q1"])


arena._sqlite_backup_into_real = arena._sqlite_backup_into  # the unpatched function, for the lock test above


class DesktopChangesAreKept(ArenaCase):
    def test_turns_added_in_the_desktop_app_are_not_overwritten(self):
        make_conversation(self.src, ["q1", "a1"])
        self.assertTrue(self.sync()[0])
        add_step(self.dst, "asked in the desktop app")
        add_step(self.src, "asked in arena")
        (self.cli / "conversations" / f"{SID}.pb").write_bytes(b"cli pb")
        ok, info, _, conflict = self.sync(lang="en")
        self.assertFalse(ok)
        self.assertTrue(conflict)
        self.assertEqual(info, arena.M("en", "antigravity_desktop_conflict"))
        self.assertEqual(read_steps(self.dst), ["q1", "a1", "asked in the desktop app"])
        self.assertFalse((self.desk / "conversations" / f"{SID}.pb").exists(), "the .pb was copied despite the conflict")

    def test_one_new_turn_on_each_side_is_a_conflict_even_though_row_counts_match(self):
        make_conversation(self.src, ["q1"])
        self.assertTrue(self.sync()[0])
        add_step(self.dst, "desktop turn")
        add_step(self.src, "arena turn")
        self.assertNotEqual(arena._db_fingerprint(self.src), arena._db_fingerprint(self.dst))
        self.assertEqual(arena._antigravity_db_action(SID, self.src, self.dst), "conflict")

    def test_conflict_still_opens_the_app_when_asked_to(self):
        make_conversation(self.src, ["q1"])
        self.assertTrue(self.sync()[0])
        add_step(self.dst, "desktop")
        add_step(self.src, "arena")
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(arena.sys, "platform", "darwin"), mock.patch.object(arena.subprocess, "run", return_value=done) as run:
            ok, _, _, conflict = self.sync(open_app=True)
        self.assertTrue(ok)
        self.assertTrue(conflict)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(read_steps(self.dst), ["q1", "desktop"])

    def test_unchanged_desktop_copy_keeps_following_the_cli(self):
        make_conversation(self.src, ["q1"])
        self.assertTrue(self.sync()[0])
        for text in ("q2", "q3"):
            add_step(self.src, text)
            ok, info, _, conflict = self.sync()
            self.assertTrue(ok, info)
            self.assertFalse(conflict)
        self.assertEqual(read_steps(self.dst), ["q1", "q2", "q3"])

    def test_copy_made_before_this_check_existed_is_updated_while_it_is_behind(self):
        make_conversation(self.src, ["q1"])
        make_conversation(self.dst, ["q1"])  # synced by an older version: Arena has no record of it
        add_step(self.src, "q2")
        ok, info, _, conflict = self.sync()
        self.assertTrue(ok, info)
        self.assertFalse(conflict)
        self.assertEqual(read_steps(self.dst), ["q1", "q2"])

    def test_unrecorded_copy_that_is_ahead_of_the_cli_is_a_conflict(self):
        make_conversation(self.src, ["q1"])
        make_conversation(self.dst, ["q1", "desktop 1", "desktop 2"])
        ok, _, _, conflict = self.sync()
        self.assertFalse(ok)
        self.assertTrue(conflict)
        self.assertEqual(read_steps(self.dst), ["q1", "desktop 1", "desktop 2"])

    def test_identical_copies_are_recorded_without_rewriting(self):
        make_conversation(self.src, ["q1"])
        make_conversation(self.dst, ["q1"])
        before = self.dst.stat().st_mtime_ns
        ok, _, _, conflict = self.sync()
        self.assertTrue(ok)
        self.assertFalse(conflict)
        self.assertEqual(self.dst.stat().st_mtime_ns, before)
        self.assertIn(SID, arena.get_setting("antigravity_sync_state", {}))

    def test_fingerprint(self):
        make_conversation(self.src, ["a", "b"])
        fp = arena._db_fingerprint(self.src)
        self.assertRegex(fp, r"^steps:2:2:[0-9a-f]{16}\|trajectory_meta:1:1:[0-9a-f]{16}\|trajectory_metadata_blob:1:1:[0-9a-f]{16}$")
        self.assertEqual(fp, arena._db_fingerprint(self.src), "the fingerprint must be stable")
        self.assertEqual(arena._db_fingerprint(self.tmp / "missing.db"), "")
        junk = self.tmp / "junk.db"
        junk.write_bytes(b"this is not a database, " * 200)
        self.assertIsNone(arena._db_fingerprint(junk))
        self.assertTrue(arena._fingerprint_not_ahead("steps:2:2:aa", "steps:3:3:bb|x:1:1:cc"))
        self.assertFalse(arena._fingerprint_not_ahead("steps:4:4:aa", "steps:3:3:bb"))
        self.assertFalse(arena._fingerprint_not_ahead("steps:2:2:aa|extra:1:1:cc", "steps:3:3:bb"))
        self.assertFalse(arena._fingerprint_not_ahead("steps:2:2:aa", None))


class OpeningTheApp(ArenaCase):
    def test_open_timeout_is_reported_not_raised(self):
        make_conversation(self.src, ["q1"])
        boom = subprocess.TimeoutExpired("open", 15)
        with mock.patch.object(arena.sys, "platform", "darwin"), mock.patch.object(arena.subprocess, "run", side_effect=boom):
            ok, info, _, _ = self.sync(open_app=True)
        self.assertFalse(ok)
        self.assertIn("timed out", info)
        self.assertEqual(read_steps(self.dst), ["q1"])  # the copy itself had already succeeded

    def test_api_continue_answers_with_json_when_continue_session_raises(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), arena.H)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        boom = subprocess.TimeoutExpired("open", 15)
        with mock.patch.object(arena, "PORT", port), mock.patch.object(arena, "continue_session", side_effect=boom):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/continue", body=json.dumps({"run": "r", "tool": "antigravity", "action": "app"}),
                         headers={"Content-Type": "application/json", "X-Arena-Token": arena.TOKEN, "X-Arena-Lang": "en"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
        self.assertEqual(resp.status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("timed out", body["error"])


class Projects(ArenaCase):
    def write_project(self, name, data):
        self.projects.mkdir(parents=True, exist_ok=True)
        (self.projects / name).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def uri(self):
        return "file://" + arena.urllib.parse.quote(str(self.folder.resolve()), safe="/")

    def test_unexpected_project_files_do_not_raise(self):
        self.write_project("a-list.json", [1, 2, 3])
        self.write_project("b-resources-not-list.json", {"id": "p1", "projectResources": {"resources": "nope"}})
        self.write_project("c-resource-not-dict.json", {"id": "p2", "projectResources": {"resources": ["x", 7, None]}})
        self.write_project("d-uri-not-str.json", {"id": "p3", "projectResources": {"resources": [{"folderUri": 42}]}})
        self.write_project("e-block-is-list.json", {"id": "p4", "projectResources": [{"folderUri": self.uri()}]})
        self.write_project("f-git-not-dict.json", {"id": "p5", "projectResources": {"resources": [{"gitFolder": "x"}]}})
        self.write_project("g-broken.json", "{not json")
        pid, name = arena.ensure_antigravity_project(str(self.folder))
        self.assertTrue(arena.UUID_RE.fullmatch(pid))
        self.assertEqual(name, "work")

    def test_existing_project_is_found_by_either_uri_form(self):
        self.write_project("one.json", {"id": "plain", "name": "Plain", "projectResources": {"resources": [{"folderUri": self.uri()}]}})
        self.assertEqual(arena.ensure_antigravity_project(str(self.folder)), ("plain", "Plain"))
        (self.projects / "one.json").unlink()
        self.write_project("two.json", {"id": "git", "name": "Git",
                                        "projectResources": {"resources": [{"gitFolder": {"folderUri": self.uri()}}]}})
        self.assertEqual(arena.ensure_antigravity_project(str(self.folder)), ("git", "Git"))

    def test_lookup_failure_returns_empty_instead_of_raising(self):
        with mock.patch.object(arena, "_find_or_create_antigravity_project", side_effect=RuntimeError("boom")):
            self.assertEqual(arena.ensure_antigravity_project(str(self.folder)), ("", ""))

    def test_concurrent_rounds_create_one_project(self):
        gate, found = threading.Barrier(8), []

        def work():
            gate.wait()
            found.append(arena.ensure_antigravity_project(str(self.folder))[0])

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(found)), 1)
        self.assertEqual(len(list(self.projects.glob("*.json"))), 1)


class Titles(ArenaCase):
    def annotation(self):
        return self.desk / "annotations" / f"{SID}.pbtxt"

    def test_existing_fields_are_kept_when_a_title_is_added(self):
        self.annotation().parent.mkdir(parents=True)
        self.annotation().write_text("last_user_view_time:{seconds:1795609247 nanos:916000000}", encoding="utf-8")
        arena._write_antigravity_title(self.desk, SID, 'He said "hi"')
        self.assertEqual(self.annotation().read_text(encoding="utf-8"),
                         'title:"He said \\"hi\\"" last_user_view_time:{seconds:1795609247 nanos:916000000}')

    def test_existing_title_is_left_alone(self):
        self.annotation().parent.mkdir(parents=True)
        original = 'title:"Mine" last_user_view_time:{seconds:1 nanos:2}'
        self.annotation().write_text(original, encoding="utf-8")
        arena._write_antigravity_title(self.desk, SID, "Other")
        self.assertEqual(self.annotation().read_text(encoding="utf-8"), original)

    def test_new_annotation_gets_just_the_title(self):
        arena._write_antigravity_title(self.desk, SID, "Fresh")
        self.assertEqual(self.annotation().read_text(encoding="utf-8"), 'title:"Fresh"\n')


class RoundAlwaysEnds(ArenaCase):
    def test_tool_that_fails_before_starting_still_ends_the_round(self):
        run = arena.Run("r-test", ["antigravity", "claude"])
        with mock.patch.object(arena, "exec_tool", side_effect=AttributeError("'list' object has no attribute 'get'")):
            arena.run_tool(run, "antigravity")
            self.assertEqual(run.pending, {"claude"})
            self.assertIsNone(run.finished_at)
            arena.run_tool(run, "claude")
        self.assertEqual(run.pending, set())
        self.assertIsNotNone(run.finished_at)
        self.assertEqual([e for e in run.events if e.get("type") == "end"], [{"type": "end"}])
        done = [e for e in run.events if e.get("type") == "done"]
        self.assertEqual([(e["tool"], e["status"]) for e in done], [("antigravity", "start_failed"), ("claude", "start_failed")])
        rows = arena.q("SELECT tool, status, log FROM outputs WHERE run_id='r-test' ORDER BY tool")
        self.assertEqual([(r["tool"], r["status"]) for r in rows], [("antigravity", "start_failed"), ("claude", "start_failed")])
        self.assertIn("AttributeError", rows[0]["log"])

    def test_stored_result_is_kept_if_the_failure_comes_after_it(self):
        run = arena.Run("r-late", ["claude"])

        def stored_then_failed(run, name, *args):
            arena.q("INSERT INTO outputs(run_id, tool, status, seconds, output, log, cmd) VALUES(?,?,?,?,?,?,?)",
                    (run.id, name, "ok", 1.5, "the answer", "", "claude -p"))
            raise RuntimeError("late failure")

        with mock.patch.object(arena, "exec_tool", side_effect=stored_then_failed):
            arena.run_tool(run, "claude")
        row = arena.q("SELECT status, output FROM outputs WHERE run_id='r-late'", one=True)
        self.assertEqual((row["status"], row["output"]), ("ok", "the answer"))
        self.assertIsNotNone(run.finished_at)

    def test_normal_run_is_untouched_by_the_wrapper(self):
        run = arena.Run("r-ok", ["claude"])
        with mock.patch.object(arena, "exec_tool", side_effect=lambda run, name, *a: arena._finish_tool(run, name)):
            arena.run_tool(run, "claude")
        self.assertEqual([e.get("type") for e in run.events], ["end"])


if __name__ == "__main__":
    unittest.main()
