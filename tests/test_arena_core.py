"""Gate tests for arena.py outside the Antigravity sync. Deterministic, local, no network, no LLM calls.

Run from the project folder:

    python3 -m unittest discover -s tests -v

What they pin down (each was a defect found in review):
  - A missing working folder is reported as a missing folder, not as "tool not installed", and a follow-up
    into a deleted folder is refused before anything starts.
  - /api/rate, /api/winner and /api/history answer bad input with JSON instead of dropping the connection.
  - The temp folder used to read a tool's model list is removed afterwards.
  - A model ID that already names its reasoning level is not combined with a different effort flag.
  - arena.html and the copy embedded in arena.py stay identical, and the page script parses.
"""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_antigravity_sync import ROOT, ArenaCase, arena  # shares the hermetic setup: fake home, temp arena.db, no network


class CoreCase(ArenaCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"ARENA_WORKSPACE": str(self.tmp / "workspace")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), arena.H)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        patcher = mock.patch.object(arena, "PORT", port)
        patcher.start()
        self.addCleanup(patcher.stop)
        return port

    def call(self, port, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        data = None if body is None else (body if isinstance(body, str) else json.dumps(body))
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", "X-Arena-Token": arena.TOKEN})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        return resp.status, payload


class CodexWriterConflict(CoreCase):
    def test_locked_resume_forks_once_and_keeps_the_followup(self):
        sid = "11111111-2222-3333-4444-555555555555"
        child = "22222222-2222-3333-4444-555555555555"
        cli = self.tmp / "codex"
        cli.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
with Path("calls").open("a") as f:
    print(json.dumps(args), file=f)
if "resume" in args:
    print(f"Error: thread/resume: thread/resume failed: thread {args[-2]} already has an active writer (code -32600)", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"type":"thread.started", "thread_id":"22222222-2222-3333-4444-555555555555"}))
print(json.dumps({"type":"item.completed", "item":{"type":"agent_message","text":"answer"}}))
""")
        cli.chmod(0o700)
        spec = dict(arena.DEFAULT_CFG["tools"]["codex"], cmd=[str(cli), "exec", "--json", "{prompt}"], continue_url="")
        run = arena.Run("conflict", ["codex"])
        arena.exec_tool(run, "codex", spec, "question", str(self.folder), 10, "model", "low", sid,
                        chat=True, peers=[("peer", "peer answer")], attach="attachment text")
        row = arena.q("SELECT * FROM outputs WHERE run_id=?", (run.id,), one=True)
        self.assertEqual((row["status"], row["session"], row["output"]), ("ok", child, "answer"))
        calls = [json.loads(line) for line in (self.folder / "calls").read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-3:-1], ["resume", sid])
        self.assertEqual(calls[1], calls[0][:-3] + ["fork", sid, calls[0][-1]])
        for fragment in ("question", "peer answer", "attachment text"):
            self.assertIn(fragment, calls[1][-1])
        self.assertEqual(sum(ev["type"] == "done" for ev in run.events), 1)


class WorkingFolder(CoreCase):
    SPEC = {"cmd": ["echo", "{prompt}"]}

    def run_echo(self, cwd):
        run = arena.Run("r-echo", ["echo"])
        arena.exec_tool(run, "echo", self.SPEC, "hello", cwd, 10)
        return run, arena.q("SELECT status, output, log FROM outputs WHERE run_id='r-echo' AND tool='echo'", one=True)

    def test_tool_runs_in_an_existing_folder(self):
        run, row = self.run_echo(str(self.folder))
        self.assertEqual((row["status"], row["output"]), ("ok", "hello\n"))
        self.assertIsNotNone(run.finished_at)

    def test_missing_folder_is_not_reported_as_not_installed(self):
        gone = str(self.tmp / "deleted-folder")
        run, row = self.run_echo(gone)
        self.assertEqual(row["status"], "start_failed")
        self.assertIn(gone, row["log"])
        self.assertNotIn(arena.M("en", "cmd_missing", exe="echo", cfg=arena.CFG_PATH.name).strip(), row["log"])
        self.assertIsNotNone(run.finished_at)

    def test_missing_command_is_still_not_installed(self):
        run = arena.Run("r-none", ["nope"])
        arena.exec_tool(run, "nope", {"cmd": ["arena-no-such-command", "{prompt}"]}, "hi", str(self.folder), 10)
        row = arena.q("SELECT status FROM outputs WHERE run_id='r-none'", one=True)
        self.assertEqual(row["status"], "not_installed")

    def test_follow_up_into_a_deleted_folder_is_refused_before_anything_starts(self):
        gone = str(self.tmp / "deleted-folder")
        arena.q("INSERT INTO runs(id, created_at, prompt, cwd, tag, tools, winner, thread, workdir) VALUES(?,?,?,?,?,?,?,?,?)",
                ("parent-1", 1.0, "first question", gone, "", '["claude"]', "", "parent-1", gone))
        with mock.patch.object(arena, "run_tool") as run_tool:
            with self.assertRaises(ValueError) as caught:
                arena.start_run("follow-up", [], None, "", parent="parent-1", lang="en")
        self.assertIn(gone, str(caught.exception))
        run_tool.assert_not_called()
        self.assertEqual(arena.q("SELECT count(*) n FROM runs", one=True)["n"], 1)


class EffortInModelId(CoreCase):
    SPEC = {"effort_args": ["--effort", "{effort}"], "efforts": ["low", "medium", "high", "xhigh", "max"]}

    def test_model_sets_effort(self):
        self.assertTrue(arena.model_sets_effort("gemini-4-neon-high", self.SPEC))
        self.assertTrue(arena.model_sets_effort("gemini-4-neon-xhigh", self.SPEC))
        self.assertFalse(arena.model_sets_effort("gemini-4-neon", self.SPEC))
        self.assertFalse(arena.model_sets_effort("", self.SPEC))
        self.assertFalse(arena.model_sets_effort("gemini-4-neon-minimal", self.SPEC), "a level this tool does not offer")
        self.assertFalse(arena.model_sets_effort("some-model-high", {"efforts": ["high"]}), "the tool takes no effort flag")

    def start(self, model, effort):
        with mock.patch.object(arena, "run_tool"):
            rid, tools, models, efforts, _ = arena.start_run("q", ["antigravity"], None, "", models={"antigravity": model},
                                                             efforts={"antigravity": effort}, lang="en")
        self.addCleanup(arena.RUNS.pop, rid, None)
        stored = json.loads(arena.q("SELECT efforts FROM runs WHERE id=?", (rid,), one=True)["efforts"])
        return efforts, stored

    def test_effort_is_dropped_when_the_model_id_names_a_level(self):
        efforts, stored = self.start("gemini-4-neon-high", "low")
        self.assertEqual(efforts, {"antigravity": ""})
        self.assertEqual(stored, {"antigravity": ""})

    def test_effort_is_kept_for_a_model_id_without_a_level(self):
        efforts, stored = self.start("gemini-4-neon", "low")
        self.assertEqual(efforts, {"antigravity": "low"})
        self.assertEqual(stored, {"antigravity": "low"})


class HttpValidation(CoreCase):
    def test_rate_rejects_missing_or_bad_fields_with_json(self):
        port = self.serve()
        for body in ({}, {"run_id": "r1"}, {"run_id": "r1", "tool": "claude", "score": "abc"}, {"tool": "claude", "score": 3}):
            status, payload = self.call(port, "POST", "/api/rate", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)
        self.assertEqual(arena.q("SELECT count(*) n FROM ratings", one=True)["n"], 0)

    def test_rate_stores_a_valid_rating(self):
        port = self.serve()
        status, payload = self.call(port, "POST", "/api/rate", {"run_id": "r1", "tool": "claude", "score": "4", "note": "good"})
        self.assertEqual((status, payload), (200, {"ok": True}))
        row = arena.q("SELECT score, note FROM ratings WHERE run_id='r1' AND tool='claude'", one=True)
        self.assertEqual((row["score"], row["note"]), (4, "good"))

    def test_winner_requires_a_run_id(self):
        port = self.serve()
        self.assertEqual(self.call(port, "POST", "/api/winner", {"winner": "claude"})[0], 400)
        arena.q("INSERT INTO runs(id, created_at, prompt, winner) VALUES('r1', 1.0, 'q', '')")
        self.assertEqual(self.call(port, "POST", "/api/winner", {"run_id": "r1", "winner": "claude"})[0], 200)
        self.assertEqual(arena.q("SELECT winner FROM runs WHERE id='r1'", one=True)["winner"], "claude")

    def test_body_that_is_not_an_object_is_rejected(self):
        port = self.serve()
        for body in ("[1, 2]", '"text"', "7"):
            status, payload = self.call(port, "POST", "/api/rate", body)
            self.assertEqual((status, payload), (400, {"error": "bad json"}), body)

    def test_history_with_a_bad_limit_still_answers(self):
        port = self.serve()
        status, payload = self.call(port, "GET", "/api/history?limit=abc")
        self.assertEqual((status, payload), (200, {"runs": []}))


class TempFolders(CoreCase):
    def test_model_list_temp_folder_is_removed(self):
        seen = []

        def fake_run_streaming(exe, args, spec, cwd, timeout, on_text):
            seen.append(cwd)
            self.assertTrue(os.path.isdir(cwd))
            return 0, "gemini-4-neon-high\ngemini-4-neon-low\n", ""

        with mock.patch.object(arena, "which", return_value="/bin/echo"), \
             mock.patch.object(arena, "run_streaming", side_effect=fake_run_streaming), \
             mock.patch.object(arena, "MODELS_FILE", self.tmp / "models.json"), \
             mock.patch.dict(arena.MODEL_CACHE, {}, clear=True):
            arena.refresh_models()
            self.assertEqual([m["id"] for m in arena.MODEL_CACHE["antigravity"]], ["gemini-4-neon-high", "gemini-4-neon-low"])
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]), "the temp folder was left behind")
        self.assertFalse(arena.MODELS_LOCK.locked())

    def test_model_list_temp_folder_is_removed_when_the_command_fails(self):
        seen = []

        def boom(exe, args, spec, cwd, timeout, on_text):
            seen.append(cwd)
            raise OSError("cannot start")

        with mock.patch.object(arena, "which", return_value="/bin/echo"), mock.patch.object(arena, "run_streaming", side_effect=boom):
            arena.refresh_models()
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))


class JudgeTimeout(CoreCase):
    """The scoring model often needs 3-5 minutes. 300 s cut off about one review in six, some of them mid-answer."""

    def load_with(self, **cfg):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"tools": {}, **cfg}), encoding="utf-8")
        with mock.patch.object(arena, "CFG_PATH", path):
            loaded = arena.load_cfg()
        return loaded, json.loads(path.read_text(encoding="utf-8"))

    def test_old_default_is_raised_and_saved(self):
        loaded, saved = self.load_with(version=4, judge_timeout=300)
        self.assertEqual((loaded["judge_timeout"], loaded["version"]), (900, 5))
        self.assertEqual((saved["judge_timeout"], saved["version"]), (900, 5))

    def test_a_limit_the_user_chose_is_kept(self):
        for chosen in (120, 450, 1800):
            loaded, saved = self.load_with(version=4, judge_timeout=chosen)
            self.assertEqual((loaded["judge_timeout"], saved["judge_timeout"], saved["version"]), (chosen, chosen, 5))

    def test_migration_runs_once(self):
        loaded, _ = self.load_with(version=5, judge_timeout=300)  # set to 300 again after the migration: respected
        self.assertEqual(loaded["judge_timeout"], 300)

    def test_new_config_uses_the_new_default(self):
        self.assertEqual(arena.DEFAULT_CFG["judge_timeout"], 900)
        self.assertGreater(arena.JUDGE_GRACE, 0)

    def test_scoring_uses_the_configured_limit_with_grace_and_cleans_up(self):
        arena.q("INSERT INTO runs(id, created_at, prompt, thread) VALUES('r-judge', 1.0, 'which is better?', 'r-judge')")
        for tool in ("claude", "codex"):
            arena.q("INSERT INTO outputs(run_id, tool, status, seconds, output, log, cmd) VALUES('r-judge', ?, 'ok', 1.0, ?, '', '')",
                    (tool, f"answer from {tool}"))
        calls = []

        def fake_run_streaming(exe, args, spec, cwd, timeout, on_text, **kw):
            calls.append({"cwd": cwd, "timeout": timeout, "existed": os.path.isdir(cwd), **kw})
            return None, "", ""  # the scoring model ran out of time without printing anything

        with mock.patch.object(arena, "which", return_value="/bin/echo"), \
             mock.patch.object(arena, "run_streaming", side_effect=fake_run_streaming):
            self.assertEqual(arena.start_judge("r-judge", lang="en"), "")
            deadline = arena.time.time() + 5
            while "r-judge" in arena.JUDGING and arena.time.time() < deadline:
                arena.time.sleep(0.01)
        self.assertNotIn("r-judge", arena.JUDGING)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["timeout"], calls[0]["grace"], calls[0]["existed"]), (900, arena.JUDGE_GRACE, True))
        self.assertFalse(os.path.exists(calls[0]["cwd"]), "the scoring temp folder was left behind")
        row = arena.q("SELECT status, reason FROM judgments WHERE run_id='r-judge'", one=True)
        self.assertEqual(row["status"], "error")
        self.assertIn(arena.M("en", "judge_no_output"), row["reason"])

    def stream(self, script, timeout, **kw):
        seen = []
        started = arena.time.time()
        rc, out, _ = arena.run_streaming("/bin/sh", ["-c", script], {}, str(self.folder), timeout, seen.append, **kw)
        return rc, out, arena.time.time() - started

    def test_answer_still_streaming_at_the_limit_is_allowed_to_finish(self):
        rc, out, _ = self.stream("for i in 1 2 3 4; do echo line$i; sleep 0.05; done", 0.05, grace=5, quiet=1)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "line1\nline2\nline3\nline4\n")

    def test_same_answer_is_cut_off_without_grace(self):
        rc, out, _ = self.stream("for i in 1 2 3 4; do echo line$i; sleep 0.05; done", 0.05)
        self.assertIsNone(rc)
        self.assertNotIn("line4", out)

    def test_silent_command_is_stopped_at_the_limit_even_with_grace(self):
        rc, out, took = self.stream("sleep 5", 0.1, grace=5, quiet=1)
        self.assertIsNone(rc)
        self.assertEqual(out, "")
        self.assertLess(took, 1.0)

    def test_command_that_goes_quiet_is_stopped(self):
        rc, out, took = self.stream("echo started; sleep 5", 0.1, grace=5, quiet=0.1)
        self.assertIsNone(rc)
        self.assertEqual(out, "started\n")
        self.assertLess(took, 1.0)

    def test_grace_has_an_upper_limit(self):
        rc, _, took = self.stream("while true; do echo x; sleep 0.02; done", 0.05, grace=0.2, quiet=1)
        self.assertIsNone(rc)
        self.assertLess(took, 1.0)


class Page(unittest.TestCase):
    def html(self):
        return (ROOT / "arena.html").read_text(encoding="utf-8")

    def test_embedded_page_matches_arena_html(self):
        self.assertEqual(arena.PAGE.strip(), self.html().strip(),
                         "arena.html and the PAGE string in arena.py differ: apply every page change to both")

    def test_failed_delete_does_not_remove_the_round_from_the_page(self):
        self.assertNotIn("post('/api/delete/'+encodeURIComponent(id)).catch(toast)", self.html())
        self.assertIn("try{await post('/api/delete/'+encodeURIComponent(id))}catch(err){toast(err);return}", self.html())

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_page_scripts_parse(self):
        html, scripts, pos = self.html(), [], 0
        while True:
            start = html.find("<script", pos)
            if start < 0:
                break
            body = html.index(">", start) + 1
            end = html.index("</script>", body)
            scripts.append(html[body:end])
            pos = end
        self.assertTrue(scripts)
        with tempfile.TemporaryDirectory() as tmp:
            for i, text in enumerate(scripts):
                path = os.path.join(tmp, f"page-{i}.js")
                Path(path).write_text(text, encoding="utf-8")
                done = subprocess.run([shutil.which("node"), "--check", path], capture_output=True, text=True, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr[-600:])


if __name__ == "__main__":
    unittest.main()


class ScoringModelCase(CoreCase):
    """The scoring model is chosen on the Setup page; Grok stays the default."""

    def test_default_is_grok_and_a_choice_sticks(self):
        cfg = arena.load_cfg()
        self.assertEqual(arena.judge_tool(cfg), "grok")
        self.assertIn("claude", arena.judge_choices(cfg))
        self.assertNotIn("openai", arena.judge_choices(cfg))  # the opt-in API column cannot score
        arena.set_setting("judge_tool", "claude")
        self.assertEqual(arena.judge_tool(cfg), "claude")
        arena.set_setting("judge_tool", "no-such-tool")  # an unknown value falls back to the default
        self.assertEqual(arena.judge_tool(cfg), "grok")

    def test_stream_output_is_parsed_before_reading_the_verdict(self):
        verdict = {"scores": {"A": {"overall": 4}}, "best": "A"}
        text = "```json\n" + json.dumps(verdict) + "\n```"
        raw = "\n".join(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                    "delta": {"type": "text_delta", "text": text[i:i + 7]}}})
                        for i in range(0, len(text), 7))
        self.assertIsNone(arena.parse_judge(raw))  # the JSON is split across escaped stream events
        out = arena.parsed_output(arena.load_cfg()["tools"]["claude"], raw)
        self.assertEqual(arena.parse_judge(out)["best"], "A")
