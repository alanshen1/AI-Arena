#!/usr/bin/env python3
"""AI Arena — a local comparison desk: send one question to Claude / Codex / Antigravity in parallel,
stream the answers side by side, and keep history and scores.

Start:  double-click AI Arena.app (recommended), or python3 arena.py   # opens http://127.0.0.1:8765
        python3 arena.py --no-open --idle-exit                      # no browser; quits after 15 idle minutes
Environment variables: ARENA_PORT (port), ARENA_CONFIG (config path), ARENA_DB (database path)

The first run creates arena_config.json next to this script. Edit it to change each tool's command,
parser and color, or to add new tools (such as ollama or aider) without touching the code.
Data is stored in arena.db (SQLite). The interface is arena.html in the same folder
(the built-in copy is used when it is missing).
Needs only the Python 3.8+ standard library.
"""
import codecs, copy, glob, json, os, platform, random, re, zipfile, xml.etree.ElementTree as ET, secrets, shutil, tempfile, shlex, signal, sqlite3, subprocess, sys, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import mimetypes
import urllib.parse

HERE = Path(__file__).resolve().parent
CFG_PATH = Path(os.environ.get("ARENA_CONFIG", HERE / "arena_config.json"))
DB_PATH = Path(os.environ.get("ARENA_DB", HERE / "arena.db"))
ATTACH_DIR = Path(os.environ.get("ARENA_ATTACH", HERE / "attachments"))  # original attachment files and their extracted text
PORT = int(os.environ.get("ARENA_PORT", 8765))
TOKEN = secrets.token_urlsafe(24)  # regenerated on every start so other web pages cannot call the local API
IDLE_EXIT = "--idle-exit" in sys.argv  # set when started from the macOS app: quit some time after the page is closed
IDLE_SECS = int(os.environ.get("ARENA_IDLE_SECS", 900))
LAST_SEEN = time.time()
SRV = None

DEFAULT_CFG = {
    "version": 4,
    "workspace": "~/AI-Arena",  # folder the tools run in when no working folder is set; all Arena sessions are kept here
    "judge_timeout": 300,
    "timeout": 600,
    "tools": {
        "claude": {
            "cmd": ["claude", "-p", "{prompt}", "--output-format", "stream-json",
                    "--verbose", "--include-partial-messages"],
            "parser": "claude_stream", "color": "#d97757",
            "package": "@anthropic-ai/claude-code",
            "login": "claude", "hint": "Follow the prompts to sign in, then type /exit",
            "resume_args": ["--resume", "{session}"], "add_dir_args": ["--add-dir", "{dir}"],
            "strip_env": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
            "continue_app": "Claude Desktop", "continue_desktop": ["--desktop", "--resume", "{session}"],
            "continue_terminal": ["--resume", "{session}"],
            "model_flag": "--model", "default_label": "Default (account default)",
            "effort_args": ["--effort", "{effort}"], "efforts": ["low", "medium", "high", "xhigh", "max"],
            "models": [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"},
                       {"id": "haiku", "label": "Haiku"}, {"id": "fable", "label": "Fable"}]},
        "codex": {"cmd": ["codex", "exec", "--json", "--skip-git-repo-check", "{prompt}"],
                  "parser": "codex_json", "color": "#10a37f",
                  "resume_args": ["resume", "{session}"], "resume_pos": "prompt",  # codex exec [options] resume <ID> <question>
                  "continue_app": "Codex App", "continue_url": "codex://threads/{session}",
                  "continue_terminal": ["resume", "{session}"],
                  "package": "@openai/codex",
                  "login": "codex login", "hint": "A browser opens; sign in with your ChatGPT account",
                  "model_flag": "-m", "default_label": "Default (recommended)",
                  "effort_args": ["-c", "model_reasoning_effort=\"{effort}\""],
                  "efforts": ["minimal", "low", "medium", "high", "xhigh"],
                  "models": [{"id": "gpt-6.2-sol", "label": "GPT-6.2 Sol"}, {"id": "gpt-6.2-astra", "label": "GPT-6.2 Astra"},
                             {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"}, {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra"},
                             {"id": "gpt-6-luna", "label": "GPT-6 Luna"}]},
        "openai": {"label": "OpenAI GPT", "cmd": ["openai-responses", "{prompt}"],
                   "transport": "openai_responses", "default_on": False, "color": "#087e84",
                   "model_flag": "--model", "default_label": "Default (GPT-6.2 Sol)",
                   "effort_args": ["--effort", "{effort}"], "efforts": ["low", "medium", "high", "xhigh"],
                   "models": [{"id": "gpt-6.2-sol", "label": "GPT-6.2 Sol"},
                              {"id": "gpt-6.2-astra", "label": "GPT-6.2 Astra"}]},
        "antigravity": {
            "label": "Antigravity", "cmd": ["agy", "-p", "{prompt}", "--output-format", "stream-json"],
            "parser": "agy_stream", "color": "#7b61ff",
            "resume_args": ["--conversation", "{session}"], "add_dir_args": ["--add-dir", "{dir}"],
            "continue_terminal": ["--conversation", "{session}"],
            "pty": True,  # agy -p can hang without a terminal, so it runs in a pseudo-terminal
            "install_script": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
            "login": "agy", "hint": "Sign in with your Google account, then type /quit",
            "model_flag": "--model", "default_label": "Default (auto)",
            "effort_args": ["--effort", "{effort}"], "efforts": ["low", "medium", "high", "xhigh", "max"],
            "models_cmd": ["agy", "models"], "models": []},
        "grok": {"role": "judge",  # the scoring model; it does not compete
                 "label": "Grok", "cmd": ["grok", "--no-auto-update", "-p", "{prompt}", "--output-format", "plain"],
                 "color": "#475569", "install_script": "curl -fsSL https://x.ai/cli/install.sh | bash",
                 "login": "grok login", "hint": "A browser opens; sign in with your xAI / X account",
                 "model_flag": "-m", "default_label": "Default",
                 "models": [{"id": "grok-4.7", "label": "Grok 4.7"}, {"id": "grok-4.5", "label": "Grok 4.5"}]},
        "gemini": {"enabled": False,  # personal accounts stopped working on 2026-06-18; set to true if you have a paid Gemini API key
                   "cmd": ["gemini", "-p", "{prompt}"], "parser": "raw", "color": "#4285f4",
                   "package": "@google/gemini-cli",
                   "login": "gemini", "hint": "Choose Login with Google, then type /quit",
                   "model_flag": "-m", "default_label": "Default (Auto routing)",
                   "models": [{"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro"},
                              {"id": "gemini-3-pro-preview", "label": "Gemini 3 Pro"},
                              {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash"},
                              {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
                              {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"}]},
    },
}


# ---------------- Interface language ----------------
# All user-facing and model-facing text lives in locales/<code>.json (zh, en, ja, ...).
# The front end sends the language code in the X-Arena-Lang header; English is the fallback for missing keys.
LOCALE_DIR = Path(os.environ.get("ARENA_LOCALES", HERE / "locales"))
LANG_ORDER = ["zh", "en", "ja"]  # order of the language switch; other files are appended alphabetically


def load_locales():
    found = {}
    for f in sorted(LOCALE_DIR.glob("*.json")):
        try:
            found[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"[locales] skipped {f.name}: {e}", file=sys.stderr)
    if "en" not in found:
        sys.exit(f"AI Arena: {LOCALE_DIR / 'en.json'} is missing. Keep the locales folder next to arena.py.")
    return found


LOCALES = load_locales()
LANGS = [c for c in LANG_ORDER if c in LOCALES] + sorted(c for c in LOCALES if c not in LANG_ORDER)
PH_RE = re.compile(r"(?<!\{)\{(\w+)\}(?!\})")  # {name} placeholders; {{name}} is left alone


def norm_lang(value):
    v = (value or "").strip().lower()
    return next((c for c in LANGS if v == c or v.startswith(c + "-")), "en")


def loc(lang, section, key, default=None):
    for code in (lang, "en"):
        val = LOCALES.get(code, {}).get(section, {}).get(key)
        if val is not None:
            return val
    return default


def fmt(text, **kw):
    return PH_RE.sub(lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0), text)


def M(lang, key, **kw):
    """Backend message (process log, errors) in the given language."""
    return fmt(loc(lang, "msg", key, key), **kw)


def MT(lang, key, **kw):
    """Text sent to the models (instructions, attachment and history templates)."""
    return fmt(loc(lang, "model", key, ""), **kw)


def parse_words(key):
    """A parse token (e.g. the word for "Answer") from every language, for reading model output."""
    out = []
    for d in LOCALES.values():
        v = d.get("parse", {}).get(key)
        out += v if isinstance(v, list) else [v] if v else []
    return list(dict.fromkeys(out))


def tool_text(lang, name, spec, field):
    """A tool's sign-in hint or default model label: the language file first, then the config."""
    return (loc(lang, "tools", name) or {}).get(field) or spec.get(field) or ""



def load_cfg():
    if not CFG_PATH.exists():
        CFG_PATH.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    tools = cfg.setdefault("tools", {})
    if "antigravity" not in tools:  # one-time migration: Gemini CLI personal tier ended -> Antigravity CLI
        migrated = {}
        for name, spec in tools.items():
            if name == "gemini":
                migrated["antigravity"] = copy.deepcopy(DEFAULT_CFG["tools"]["antigravity"])
                spec["enabled"] = False
            migrated[name] = spec
        migrated.setdefault("antigravity", copy.deepcopy(DEFAULT_CFG["tools"]["antigravity"]))
        cfg["tools"] = tools = migrated
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if cfg.get("version", 1) < 2:  # v2: Codex / Antigravity switch to JSON output so the session ID is available for follow-ups
        old = {"codex": ["codex", "exec", "--skip-git-repo-check", "{prompt}"], "antigravity": ["agy", "-p", "{prompt}"]}
        for name, old_cmd in old.items():
            sp = tools.get(name)
            if sp and sp.get("cmd") == old_cmd:
                sp["cmd"] = list(DEFAULT_CFG["tools"][name]["cmd"])
                sp["parser"] = DEFAULT_CFG["tools"][name]["parser"]
        cfg["version"] = 2
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if cfg.get("version", 1) < 3:  # v3: add Grok as the scoring model
        tools.setdefault("grok", copy.deepcopy(DEFAULT_CFG["tools"]["grok"]))
        cfg.setdefault("judge_timeout", DEFAULT_CFG["judge_timeout"])
        cfg["version"] = 3
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if cfg.get("version", 1) < 4:  # v4: an effort hard-coded in the Codex command overrides "Default", and plain-text output has no session ID
        sp = tools.get("codex")
        if sp and sp.get("parser") == "raw" and sp.get("cmd", [])[:2] == ["codex", "exec"]:
            cmd, rest, i = sp["cmd"], [], 0
            while i < len(cmd):
                if cmd[i] in ("-c", "--config") and i + 1 < len(cmd) and cmd[i + 1].startswith("model_reasoning_effort="):
                    i += 2
                    continue
                rest.append(cmd[i])
                i += 1
            if rest == ["codex", "exec", "--skip-git-repo-check", "{prompt}"]:
                backup = CFG_PATH.with_name(CFG_PATH.stem + ".backup-v3.json")
                if not backup.exists():
                    backup.write_text(CFG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
                sp["cmd"] = list(DEFAULT_CFG["tools"]["codex"]["cmd"])
                sp["parser"] = DEFAULT_CFG["tools"]["codex"]["parser"]
        cfg["version"] = 4
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tools.setdefault("openai", copy.deepcopy(DEFAULT_CFG["tools"]["openai"]))
    for name, spec in tools.items():  # fill in new fields for older configs
        for k, v in DEFAULT_CFG["tools"].get(name, {}).items():
            spec.setdefault(k, copy.deepcopy(v))
    return cfg


def enabled_tools(cfg):  # competing tools (excluding the scoring model)
    return [n for n, sp in cfg["tools"].items() if sp.get("enabled", True) and sp.get("role") != "judge"]


def openai_ready():
    return bool(os.environ.get("ARENA_OPENAI_API_KEY", "").strip())


def judge_tool(cfg):
    return next((n for n, sp in cfg["tools"].items() if sp.get("role") == "judge" and sp.get("enabled", True)), None)


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")


class PtyText:
    """Turn pseudo-terminal output into plain text: strip ANSI codes, apply \r overwrites (progress animations), emit line by line."""

    def __init__(self):
        self.line = ""
        self.pending_cr = False

    def feed(self, txt):
        out = []
        for ch in ANSI_RE.sub("", txt):
            if ch == "\n":
                out.append(self.line + "\n")
                self.line, self.pending_cr = "", False
            elif ch == "\r":
                self.pending_cr = True
            else:
                if self.pending_cr:
                    self.line, self.pending_cr = "", False
                if ch >= " " or ch == "\t":
                    self.line += ch
        return "".join(out)

    def flush(self):
        rest, self.line = self.line, ""
        return rest


MODEL_CACHE = {}  # model lists read at runtime from models_cmd (e.g. agy models)
MODELS_FILE = Path(os.environ.get("ARENA_MODELS", HERE / "models_cache.json"))  # the last list read; used first at startup
SLUG_RE = re.compile(r"\b([a-z][a-z0-9]*(?:[.-][a-z0-9]+)+)\b")
NAME_RE = re.compile(r"^((?:Gemini|Claude|GPT|Orchestrator)\b[\w .()\-]*?)\s*$", re.I)
EFFORT_SUFFIX = re.compile(r"-(?:minimal|low|medium|high|xhigh|max)$")
MODELS_LOCK = threading.Lock()

try:
    MODEL_CACHE.update(json.loads(MODELS_FILE.read_text(encoding="utf-8")))
except (OSError, ValueError):
    pass


def parse_models(text):
    """Parse output such as `agy models`: prefer model IDs (gemini-4-neon) and fall back to display names."""
    text = ANSI_RE.sub("", text or "").replace("\r", "")
    t = text.strip()
    if t[:1] in "[{":  # JSON output
        try:
            data = json.loads(t)
            items = data if isinstance(data, list) else data.get("models") or []
            out = []
            for x in items:
                if isinstance(x, str):
                    out.append({"id": x, "label": x})
                elif isinstance(x, dict):
                    i = x.get("id") or x.get("name") or x.get("model")
                    if i:
                        out.append({"id": str(i), "label": str(x.get("display_name") or x.get("label") or x.get("title") or i)})
            return out
        except ValueError:
            pass
    models = []
    for ln in text.splitlines():
        ln = ln.strip(" \t│|*•>✓✔●○")
        if not ln or ln.lower().startswith(("available", "usage", "model ", "models", "id ", "name ")):
            continue
        m = SLUG_RE.search(ln)
        if m and "-" in m.group(1):
            rest = (ln[:m.start()] + ln[m.end():]).strip(" \t-*•:|│")
            if rest.startswith("(") and rest.endswith(")"):
                rest = rest[1:-1].strip()
            rest = re.sub(r"\s{2,}.*$", "", rest)  # drop trailing table columns (such as quota notes)
            if rest.lower().strip("()[] ") in {w.lower() for w in parse_words("default_words")}:
                rest = ""
            models.append({"id": m.group(1), "label": rest or m.group(1)})
            continue
        n = NAME_RE.match(re.split(r"\s{2,}|\t", ln)[0])
        if n:  # display name only, e.g. "Gemini 4 Neon", "Orchestrator (Neon)"
            models.append({"id": n.group(1).strip(), "label": n.group(1).strip()})
    ids = {x["id"] for x in models}
    # when a model is listed once per thinking level (gemini-4-neon-high…) and the base ID is also listed, keep only the base ID; pick the level with Effort
    models = [x for x in models if not (EFFORT_SUFFIX.search(x["id"]) and EFFORT_SUFFIX.sub("", x["id"]) in ids)]
    return list({x["id"]: x for x in models}.values())


def refresh_models():
    if not MODELS_LOCK.acquire(blocking=False):
        return
    try:
        cfg = load_cfg()
        changed = False
        for name in enabled_tools(cfg):
            spec = cfg["tools"][name]
            mc = spec.get("models_cmd")
            exe = mc and which(mc[0])
            if not exe:
                continue
            try:  # agy can hang without a terminal; use the pty setting, as for questions
                rc, out, err = run_streaming(exe, mc[1:], spec, tempfile.mkdtemp(prefix="arena-models-"), 40, lambda t: None)
            except Exception as e:
                print(f"[models] {name}: {e}", flush=True)
                continue
            models = parse_models(out)
            if models:
                print(f"[models] {name}: {len(models)} models: " + ", ".join(x["id"] for x in models), flush=True)
                if MODEL_CACHE.get(name) != models:
                    MODEL_CACHE[name], changed = models, True
            else:  # nothing parsed: write the raw output to arena.log to help diagnose the format
                raw = ANSI_RE.sub("", out + ("\n[stderr] " + err if (err or "").strip() else "")).strip()
                print(f"[models] {name}: exit={'timeout' if rc is None else rc}, nothing parsed. Raw output:\n"
                      f"{raw[:3000] or '(empty)'}", flush=True)
        if changed:
            try:
                MODELS_FILE.write_text(json.dumps(MODEL_CACHE, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
    finally:
        MODELS_LOCK.release()


# ---------------- Finding commands (PATH is short when started from Finder, so search common install locations) ----------------
def _ver_key(p):
    return [int(x) for x in re.findall(r"\d+", p)[:3]]


ARENA_HOME = Path.home() / ".ai-arena"     # one-click install location; no admin rights needed
NODE_DIR = ARENA_HOME / "node"             # standalone Node (downloaded when the system Node is < 20 or missing)
NPM_PREFIX = ARENA_HOME / "npm-global"     # CLI install folder
NODE_PIN = ARENA_HOME / "node-dir"        # folder of the chosen Node 20+; put first in PATH when running CLIs
NODE_LOCK = threading.Lock()


def search_path():
    home = str(Path.home())
    front = [str(NODE_DIR / "bin")]
    try:
        front.append(NODE_PIN.read_text().strip())
    except OSError:
        pass
    dirs = front + [str(NPM_PREFIX / "bin")] + os.environ.get("PATH", "").split(os.pathsep)
    dirs += [f"{home}/.local/bin", f"{home}/.npm-global/bin", "/opt/homebrew/bin", "/usr/local/bin",
             f"{home}/.bun/bin", f"{home}/.volta/bin", f"{home}/Library/pnpm", f"{home}/.yarn/bin",
             f"{home}/.asdf/shims", f"{home}/.local/share/mise/shims", f"{home}/.claude/local"]
    for pat in (f"{home}/.nvm/versions/node/*/bin", f"{home}/.fnm/node-versions/*/installation/bin",
                f"{home}/Library/Application Support/fnm/node-versions/*/installation/bin"):
        dirs += sorted(glob.glob(pat), key=_ver_key, reverse=True)  # newer versions first
    joined = os.pathsep.join(dict.fromkeys(d for d in dirs if d))
    npm = shutil.which("npm", path=joined)
    if npm:  # npm global install folder
        try:
            r = subprocess.run([npm, "prefix", "-g"], capture_output=True, text=True, timeout=15,
                               env={**os.environ, "PATH": joined}, stdin=subprocess.DEVNULL)
            if r.returncode == 0 and r.stdout.strip():
                joined += os.pathsep + os.path.join(r.stdout.strip(), "bin")
        except Exception:
            pass
    return os.pathsep.join(dict.fromkeys(joined.split(os.pathsep)))


PATH_CACHE = search_path()


def child_env():
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # allow starting from inside a Claude Code terminal
    env.pop("ARENA_OPENAI_API_KEY", None)
    env.update(NO_COLOR="1", FORCE_COLOR="0", PATH=PATH_CACHE)
    return env


def which(exe):
    return shutil.which(exe, path=PATH_CACHE)


JOBS = {}  # install jobs: tool -> {"running", "ok", "log"}


def doctor(lang="en"):
    global PATH_CACHE
    PATH_CACHE = search_path()
    cfg, res = load_cfg(), {}

    def check(name, spec):
        exe = spec["cmd"][0]
        if spec.get("transport") == "openai_responses":
            res[name] = {"name": name, "api": True, "ready": openai_ready(), "path": "api.openai.com",
                         "version": "Responses API"}
            return
        path, ver = which(exe), ""
        if path:
            try:
                r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20,
                                   env=child_env(), stdin=subprocess.DEVNULL)
                lines = (r.stdout.strip() or r.stderr.strip()).splitlines()
                ver = lines[0][:80] if lines else ""
            except Exception:
                ver = M(lang, "detect_timeout")
        res[name] = {"name": name, "exe": exe, "path": path, "version": ver, "role": spec.get("role", ""), "install": bool(spec.get("package") or spec.get("install_script")),
                     "login": spec.get("login") or "", "hint": tool_text(lang, name, spec, "hint")}

    names = enabled_tools(cfg) + ([judge_tool(cfg)] if judge_tool(cfg) else [])
    ths = [threading.Thread(target=check, args=(n, cfg["tools"][n])) for n in names]
    for th in ths:
        th.start()
    for th in ths:
        th.join(30)
    node = ""
    if which("node"):
        try:
            node = subprocess.run([which("node"), "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            pass
    threading.Thread(target=refresh_models, daemon=True).start()
    return {"tools": [res[n] for n in cfg["tools"] if n in res], "npm": which("npm"), "node": node,
            "path": PATH_CACHE, "jobs": JOBS}


def node_version(node):
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        return int(v.lstrip("v").split(".")[0]), v
    except Exception:
        return 0, ""


def ensure_node(log, lang="en"):
    """Return the npm to use: a downloaded standalone Node first, the system Node if it is >= 20, otherwise download Node 22."""
    if (NODE_DIR / "bin" / "npm").exists():
        return str(NODE_DIR / "bin" / "npm")
    first_old = ""
    for d in search_path().split(os.pathsep):  # first Node 20+ in PATH order (for example one installed by nvm)
        node, npm = os.path.join(d, "node"), os.path.join(d, "npm")
        if d == str(NODE_DIR / "bin") or not (os.access(node, os.X_OK) and os.path.exists(npm)):
            continue
        major, ver = node_version(node)
        if major >= 20:
            ARENA_HOME.mkdir(exist_ok=True)
            NODE_PIN.write_text(d)
            log(M(lang, "node_use", ver=ver, d=d))
            return npm
        first_old = first_old or ver
    log(M(lang, "node_none", old=M(lang, "node_old_is", v=first_old) if first_old else M(lang, "node_old_none"), dir=NODE_DIR))
    with NODE_LOCK:
        if (NODE_DIR / "bin" / "npm").exists():
            return str(NODE_DIR / "bin" / "npm")
        plat = {"darwin": "darwin", "linux": "linux"}.get(sys.platform)
        if not plat:
            raise RuntimeError(M(lang, "node_unsupported"))
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        r = subprocess.run(["curl", "-fsSL", "https://nodejs.org/dist/index.json"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError(M(lang, "node_index_fail", e=r.stderr.strip()))
        ver = next(e["version"] for e in json.loads(r.stdout) if e["version"].startswith("v22."))
        name = f"node-{ver}-{plat}-{arch}"
        url = f"https://nodejs.org/dist/{ver}/{name}.tar.gz"
        ARENA_HOME.mkdir(exist_ok=True)
        tgz = ARENA_HOME / "node.tar.gz"
        log(M(lang, "node_dl", url=url))
        r = subprocess.run(["curl", "-fL", "--silent", "--show-error", "-o", str(tgz), url],
                           capture_output=True, text=True, timeout=1200)
        if r.returncode:
            raise RuntimeError(M(lang, "node_dl_fail", e=r.stderr.strip()))
        subprocess.run(["tar", "xzf", str(tgz), "-C", str(ARENA_HOME)], check=True, timeout=300)
        if NODE_DIR.exists():
            shutil.rmtree(NODE_DIR)
        (ARENA_HOME / name).rename(NODE_DIR)
        tgz.unlink()
        log(M(lang, "node_ready", ver=ver))
    return str(NODE_DIR / "bin" / "npm")


def start_install(tool, lang="en"):
    spec = load_cfg()["tools"].get(tool) or {}
    pkg, script = spec.get("package"), spec.get("install_script")
    if not pkg and not script:
        return M(lang, "no_install")
    if JOBS.get(tool, {}).get("running"):
        return M(lang, "installing")
    job = JOBS[tool] = {"running": True, "ok": None, "log": ""}

    def log(t):
        job["log"] += t

    def work():
        global PATH_CACHE
        try:
            if script:  # official install script (e.g. Antigravity CLI, installs to ~/.local/bin)
                log(f"$ {script}\n")
                p = subprocess.Popen(["/bin/bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, env=child_env(), stdin=subprocess.DEVNULL, bufsize=1)
                for line in p.stdout:
                    log(ANSI_RE.sub("", line))
                p.wait()
                job["ok"] = p.returncode == 0
                raise StopIteration
            npm = ensure_node(log, lang)
            PATH_CACHE = search_path()
            log(f"$ npm install -g --prefix ~/.ai-arena/npm-global {pkg}\n")
            env = child_env()
            env["PATH"] = str(Path(npm).parent) + os.pathsep + PATH_CACHE  # make npm use the matching node
            p = subprocess.Popen([npm, "install", "-g", "--prefix", str(NPM_PREFIX), pkg],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 env=env, stdin=subprocess.DEVNULL, bufsize=1)
            for line in p.stdout:
                log(line)
            p.wait()
            job["ok"] = p.returncode == 0
        except StopIteration:
            pass
        except Exception as e:
            log(f"\n❌ {e}\n")
            job["ok"] = False
        PATH_CACHE = search_path()
        log(M(lang, "install_ok") if job["ok"] else M(lang, "install_fail"))
        job["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return ""


def open_login(tool, lang="en"):
    spec = load_cfg()["tools"].get(tool) or {}
    cmd = spec.get("login")
    if not cmd:
        return M(lang, "no_login")
    if sys.platform != "darwin":
        return M(lang, "login_manual", cmd=cmd)
    script = Path(tempfile.gettempdir()) / f"arena-login-{tool}.command"
    hint = tool_text(lang, tool, spec, "hint")
    title, done = M(lang, "login_title", tool=tool, hint=hint), M(lang, "login_done")
    script.write_text(f"#!/bin/bash\nexport PATH={shlex.quote(PATH_CACHE)}\necho {shlex.quote(title)}\n"
                      f"echo\n{cmd}\necho\necho {shlex.quote(done)}\n", encoding="utf-8")
    script.chmod(0o755)
    subprocess.Popen(["/usr/bin/open", str(script)])  # .command files open in Terminal; no extra permission needed
    return ""


# ---------------- Workspace and continuing a session in the tool's own app ----------------
def workspace_dir(cfg=None):
    """Folder the tools run in when no working folder is set (config "workspace", default ~/AI-Arena)."""
    cfg = cfg or load_cfg()
    d = Path(os.environ.get("ARENA_WORKSPACE") or cfg.get("workspace") or "~/AI-Arena").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def legacy_workdir(run):
    """Folder an older round ran in, before the folder was recorded: the working folder,
    else the round's attachments folder, else the folder the server was started from."""
    if run.get("workdir"):
        return run["workdir"]
    if run.get("cwd"):
        return run["cwd"]
    if json.loads(run.get("attachments") or "[]"):
        return str(ATTACH_DIR / run["id"])
    return os.getcwd()


def session_for(rid, tool):
    """(session ID, folder) of a tool's conversation as of this round."""
    run = q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    if not run:
        return "", ""
    thread = run.get("thread") or run["id"]
    rows = q("""SELECT o.session FROM runs r JOIN outputs o ON o.run_id=r.id AND o.tool=?
                WHERE (r.id=? OR r.thread=?) AND r.created_at<=? AND o.session<>'' AND o.status='ok'
                ORDER BY r.created_at""", (tool, thread, thread, run["created_at"]))
    root = q("SELECT * FROM runs WHERE id=?", (thread,), one=True) or run
    return (rows[-1]["session"] if rows else ""), legacy_workdir(run if run.get("workdir") else root)


def continue_session(rid, tool, action, lang="en"):
    """Continue a round's session outside Arena. action: app (desktop app), terminal, or command (text to copy).
    Returns (ok, message, command)."""
    spec = load_cfg()["tools"].get(tool) or {}
    label = spec.get("label") or tool.capitalize()
    sid, folder = session_for(rid, tool)
    if not sid:
        return False, M(lang, "no_session", label=label), ""
    exe = spec.get("cmd", [tool])[0]
    sub = lambda args: [a.replace("{session}", sid) for a in args]
    term = [exe] + sub(spec.get("continue_terminal") or [])
    command = f"cd {shlex.quote(folder)} && " + " ".join(shlex.quote(a) for a in term)
    if action == "command" or not spec.get("continue_terminal") and action == "terminal":
        return True, "", command
    if sys.platform != "darwin":
        return False, M(lang, "login_manual", cmd=command), command
    if action == "terminal":
        script = Path(tempfile.gettempdir()) / f"arena-continue-{tool}.command"
        script.write_text(f"#!/bin/bash\nexport PATH={shlex.quote(PATH_CACHE)}\ncd {shlex.quote(folder)} || exit 1\n"
                          + " ".join(shlex.quote(a) for a in term) + "\n", encoding="utf-8")
        script.chmod(0o755)
        subprocess.Popen(["/usr/bin/open", str(script)])  # .command files open in Terminal
        return True, M(lang, "continue_terminal", label=label), command
    app = spec.get("continue_app") or label
    if spec.get("continue_url"):
        url = spec["continue_url"].replace("{session}", sid)
        r = subprocess.run(["/usr/bin/open", url], capture_output=True, text=True)
        if r.returncode:
            return False, M(lang, "continue_fail", app=app, out=(r.stderr or r.stdout).strip()[-300:]), command
        return True, M(lang, "continue_url", app=app), command
    if spec.get("continue_desktop"):
        path = which(exe)
        if not path:
            return False, M(lang, "cmd_missing", exe=exe, cfg=CFG_PATH.name), command
        env = child_env()
        for k in spec.get("strip_env") or []:
            env.pop(k, None)
        try:
            r = subprocess.run([path] + sub(spec["continue_desktop"]), cwd=folder if Path(folder).is_dir() else None,
                               env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, M(lang, "continue_fail", app=app, out="timeout"), command
        out = ANSI_RE.sub("", (r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode:
            key = "continue_old" if re.search(r"unknown option|desktop", out, re.I) and "--desktop" in out else "continue_fail"
            return False, M(lang, key, app=app, out=out[-400:]), command
        return True, M(lang, "continue_url", app=app), command
    return False, M(lang, "continue_none", label=label), command


# ---------------- Database ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at REAL, prompt TEXT, cwd TEXT,
  tag TEXT, tools TEXT, winner TEXT, models TEXT DEFAULT '', efforts TEXT DEFAULT '',
  thread TEXT DEFAULT '', parent TEXT DEFAULT '', chat INTEGER DEFAULT 0, attachments TEXT DEFAULT '',
  peers INTEGER DEFAULT 0, workdir TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS outputs(run_id TEXT, tool TEXT, status TEXT, seconds REAL, output TEXT,
  log TEXT, cmd TEXT, model TEXT DEFAULT '', actual_model TEXT DEFAULT '', effort TEXT DEFAULT '',
  session TEXT DEFAULT '', PRIMARY KEY(run_id, tool));
CREATE TABLE IF NOT EXISTS ratings(run_id TEXT, tool TEXT, score INTEGER, note TEXT,
  PRIMARY KEY(run_id, tool));
CREATE TABLE IF NOT EXISTS judgments(run_id TEXT PRIMARY KEY, judge TEXT, model TEXT, status TEXT, best TEXT,
  reason TEXT, mapping TEXT, result TEXT, raw TEXT, seconds REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS judge_scores(run_id TEXT, tool TEXT, overall REAL, detail TEXT, comment TEXT,
  PRIMARY KEY(run_id, tool));
CREATE TABLE IF NOT EXISTS prompts(id TEXT PRIMARY KEY, title TEXT, body TEXT, category TEXT DEFAULT '',
  pinned INTEGER DEFAULT 0, uses INTEGER DEFAULT 0, created_at REAL, updated_at REAL, last_used REAL DEFAULT 0,
  seed TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""
DB_LOCK = threading.Lock()


def migrate(c):  # add missing columns to older databases
    for table, col in (("runs", "models"), ("outputs", "model"), ("outputs", "actual_model"),
                       ("runs", "efforts"), ("outputs", "effort"), ("runs", "thread"), ("runs", "parent"),
                       ("outputs", "session"), ("runs", "attachments"), ("runs", "workdir")):
        if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT ''")
    for col in ("chat", "peers"):
        if col not in [r[1] for r in c.execute("PRAGMA table_info(runs)")]:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} INTEGER DEFAULT 0")
    if "seed" not in [r[1] for r in c.execute("PRAGMA table_info(prompts)")]:
        c.execute("ALTER TABLE prompts ADD COLUMN seed TEXT DEFAULT ''")
    # Older versions stored some statuses as Chinese words; convert them to the English codes used now.
    for old, new in LEGACY_STATUS.items():
        c.execute("UPDATE outputs SET status=? WHERE status=?", (new, old))
    c.commit()


LEGACY_STATUS = {"\u672a\u5b89\u88c5": "not_installed", "\u542f\u52a8\u5931\u8d25": "start_failed",
                 "\u8d85\u65f6": "timeout", "\u5df2\u53d6\u6d88": "canceled", "\u5df2\u4e2d\u65ad": "interrupted"}


def q(sql, args=(), one=False):
    with DB_LOCK:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
            c.commit()
        finally:
            c.close()
    return (rows[0] if rows else None) if one else rows


# ---------------- Prompt library ----------------
SEED_KEYS = list(LOCALES["en"].get("prompts", {}))  # catalog order comes from locales/en.json


def seed_text(lang, key):
    """(category, title, text) of a built-in prompt in the given language."""
    return tuple(loc(lang, "prompts", key) or ("", key, ""))


SEED_BASE_TIME = 1_000_000_000  # built-in prompts sort after the user's own prompts, in catalog order


def _seeded():
    r = q("SELECT v FROM settings WHERE k='prompt_seeds'", one=True)
    return set(json.loads(r["v"])) if r else set()


def _add_seeds(keys):
    for i, k in enumerate(SEED_KEYS):
        if k in keys:
            cat, title, body = seed_text("en", k)
            q("INSERT INTO prompts(id, title, body, category, created_at, updated_at, seed) VALUES(?,?,?,?,?,?,?)",
              (uuid.uuid4().hex[:12], title, body, cat, SEED_BASE_TIME - i, SEED_BASE_TIME - i, k))


def ensure_seeds(restore=False):
    """Add built-in prompts that were never added before (deleted ones stay deleted unless "Restore built-in prompts" is used). Return how many were added."""
    with SEED_LOCK:
        seeded = _seeded()
        if not seeded and q("SELECT v FROM settings WHERE k='prompts_seeded'", one=True):
            # the 5 examples added by the previous version have no seed key: recognize them by content and tag them
            for row in q("SELECT id, title, body FROM prompts WHERE seed=''"):
                for i, k in enumerate(SEED_KEYS):
                    if (row["title"], row["body"]) in {seed_text(c, k)[1:] for c in LANGS}:
                        q("UPDATE prompts SET seed=?, created_at=?, updated_at=? WHERE id=?",
                          (k, SEED_BASE_TIME - i, SEED_BASE_TIME - i, row["id"]))
                        seeded.add(k)
            seeded |= {"explain", "compare-options", "code-review", "summarize-attachments", "fact-check"}
        present = {r["seed"] for r in q("SELECT seed FROM prompts WHERE seed<>''")}
        todo = {k for k in SEED_KEYS if k not in present and (restore or k not in seeded)}
        _add_seeds(todo)
        q("INSERT OR REPLACE INTO settings(k, v) VALUES('prompt_seeds', ?)", (json.dumps(sorted(seeded | todo)),))
        return len(todo)


def list_prompts(lang="en"):
    ensure_seeds()
    rows = q("SELECT * FROM prompts ORDER BY pinned DESC, created_at DESC")
    for r in rows:  # built-in prompts are shown in the interface language
        if r.get("seed") in SEED_KEYS:
            r["category"], r["title"], r["body"] = seed_text(lang, r["seed"])
    return rows


SEED_LOCK = threading.Lock()


def save_prompt(b):
    title, body = str(b.get("title") or "").strip()[:200], str(b.get("body") or "").strip()
    if not body:
        raise ValueError("empty")
    title = title or body.splitlines()[0][:40]
    cat, now = str(b.get("category") or "").strip()[:40], time.time()
    pid = str(b.get("id") or "")
    if pid and q("SELECT 1 FROM prompts WHERE id=?", (pid,), one=True):
        q("UPDATE prompts SET title=?, body=?, category=?, pinned=?, updated_at=?, seed='' WHERE id=?",
          (title, body, cat, int(bool(b.get("pinned"))), now, pid))
    else:
        pid = uuid.uuid4().hex[:12]
        q("INSERT INTO prompts(id, title, body, category, pinned, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
          (pid, title, body, cat, int(bool(b.get("pinned"))), now, now))
    return q("SELECT * FROM prompts WHERE id=?", (pid,), one=True)


# ---------------- Run management ----------------
class Run:
    def __init__(self, rid, tools):
        self.id, self.events, self.cond = rid, [], threading.Condition()
        self.pending, self.procs = set(tools), {}
        self.cancelled, self.finished_at = False, None
        self.judge, self.judge_model, self.lang = False, "", "en"

    def emit(self, ev):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()


RUNS = {}


class ClaudeStream:
    """Parse the JSONL from `claude -p --output-format stream-json`; non-JSON lines are passed through as text."""

    def __init__(self):
        self.buf, self.delta, self.text_seen = "", False, False

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            out += self.line(line)
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        return self.line(rest)

    def line(self, line):
        line = line.strip()
        if not line:
            return []
        try:
            o = json.loads(line)
        except ValueError:
            return [("out", line + "\n")]
        t = o.get("type")
        if t == "stream_event":
            ev = o.get("event", {})
            if ev.get("type") == "content_block_start":
                cb = ev.get("content_block", {})
                if cb.get("type") == "tool_use":
                    return [("log", f"[tool] {cb.get('name')}\n")]
                if cb.get("type") == "text" and self.delta:
                    return [("out", "\n\n")]
            if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                self.delta = True
                return [("out", ev["delta"].get("text", ""))]
            return []
        if t == "assistant":
            res = []
            for b in o.get("message", {}).get("content", []):
                if b.get("type") == "tool_use":
                    res.append(("log", f"[tool] {b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False)[:300]}\n"))
                elif b.get("type") == "text" and not self.delta:
                    res.append(("out", ("\n\n" if self.text_seen else "") + b.get("text", "")))
                    self.text_seen = True
            return res
        if t == "system" and o.get("subtype") == "init":
            return [("meta", o.get("model") or ""), ("session", o.get("session_id") or ""),
                    ("log", f"[init] model={o.get('model')}\n")]
        if t == "result":
            res = [("session", o.get("session_id") or "")]
            if not self.delta and not self.text_seen and o.get("result"):
                res.append(("out", o["result"]))
            res.append(("log", f"[result] turns={o.get('num_turns')} cost=${o.get('total_cost_usd')} "
                               f"duration={o.get('duration_ms')}ms\n"))
            return res
        return []


class LineParser:
    def __init__(self, lang="en"):
        self.buf, self.lang = "", lang

    def feed(self, chunk):
        self.buf += chunk
        out = []
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            out += self._line(line)
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        return self._line(rest)

    def _line(self, line):
        line = line.strip()
        if not line:
            return []
        try:
            o = json.loads(line)
        except ValueError:
            return [("out", line + "\n")]
        return self.event(o) if isinstance(o, dict) else []


class CodexJson(LineParser):
    """Parse the JSONL from `codex exec --json` (thread.started / item.* / turn.*)."""

    def __init__(self, lang="en"):
        super().__init__(lang)
        self.sent, self.any_out = {}, False

    def event(self, o):
        t = o.get("type", "")
        if t == "thread.started":
            return [("session", o.get("thread_id") or "")]
        if t.startswith("item."):
            it = o.get("item") or {}
            ty, iid = it.get("type"), it.get("id", "")
            if ty in ("agent_message", "assistant_message") and it.get("text"):
                txt, prev = it["text"], self.sent.get(iid, "")
                delta = txt[len(prev):] if txt.startswith(prev) else txt
                sep = "\n\n" if (not prev and self.any_out) else ""
                self.sent[iid] = txt
                if delta:
                    self.any_out = True
                    return [("out", sep + delta)]
                return []
            if t == "item.completed" and ty == "reasoning" and it.get("text"):
                return [("log", M(self.lang, "tag_think") + it["text"][:500] + "\n")]
            if t == "item.started" and ty == "command_execution":
                return [("log", M(self.lang, "tag_cmd") + f"{it.get('command', '')}\n")]
            if ty == "error":
                return [("log", M(self.lang, "tag_err") + f"{it.get('message', '')}\n")]
            return []
        if t == "turn.completed":
            return [("log", M(self.lang, "tag_usage") + f"{json.dumps(o.get('usage') or {}, ensure_ascii=False)}\n")]
        if t in ("turn.failed", "error"):
            return [("log", f"[{t}] {json.dumps(o.get('error') or o.get('message') or o, ensure_ascii=False)[:500]}\n")]
        return []


def deep_find(o, keys, depth=0):
    """Find the first non-empty string field in nested dicts / lists."""
    if depth > 6:
        return ""
    if isinstance(o, dict):
        for k in keys:
            v = o.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for v in o.values():
            r = deep_find(v, keys, depth + 1)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = deep_find(v, keys, depth + 1)
            if r:
                return r
    return ""


class AgyStream(LineParser):
    """Parse the NDJSON from `agy -p … --output-format stream-json` (init / step_update / result).
    Documented field locations are tried first, then a recursive search over the whole event; if still no text
    is found, the raw output is written to the process log."""

    def __init__(self, lang="en"):
        super().__init__(lang)
        self.got, self.raw, self.out_any = False, [], False

    def _line(self, line):
        if line.strip() and len(self.raw) < 200:
            self.raw.append(line.strip()[:1500])
        res = super()._line(line)
        if any(k == "out" and v for k, v in res):
            self.out_any = True
        return res

    def flush(self):
        res = super().flush()
        if any(k == "out" and v for k, v in res):
            self.out_any = True
        if not self.out_any and self.raw:
            res.append(("log", M(self.lang, "agy_raw") + "\n".join(self.raw[:120]) + "\n"))
        return res

    def event(self, o):
        ev = str(o.get("event") or o.get("type") or o.get("kind") or "")
        d = o.get("data") if isinstance(o.get("data"), dict) else o
        res = []
        cid = deep_find(o, ("conversation_id", "conversationId"))
        if cid:
            res.append(("session", cid))
        if ev == "init":
            model = deep_find(o, ("model", "model_name", "modelName"))
            if model:
                res.append(("meta", model))
        elif ev == "step_update":
            blob = json.dumps(o, ensure_ascii=False)
            is_answer = d.get("step_type") == "agent_response" or "agent_response" in blob or "AGENT_RESPONSE" in blob
            delta = deep_find(o, ("text_delta", "textDelta", "delta"))
            if is_answer and delta:
                self.got = True
                res.append(("out", delta))
            elif d.get("step_type") and d.get("status") == "DONE":
                res.append(("log", M(self.lang, "tag_step") + f"{d.get('step_type')}\n"))
        elif ev == "result":
            if not self.got:
                resp = deep_find(o, ("response", "text", "content", "output", "answer", "message"))
                if resp:
                    res.append(("out", resp))
            status = deep_find(o, ("status",))
            if status and status.upper() != "SUCCESS":
                res.append(("log", M(self.lang, "tag_status") + f"{status} {deep_find(o, ('error',))}\n"))
            usage = o.get("usage") or d.get("usage")
            if usage:
                res.append(("log", M(self.lang, "tag_usage") + f"{json.dumps(usage, ensure_ascii=False)}\n"))
        return res


PARSERS = {"claude_stream": lambda lang="en": ClaudeStream(), "codex_json": CodexJson, "agy_stream": AgyStream}



# ---------------- Attachments ----------------
MAX_UPLOAD_MB = 50
FILE_CHARS = 60000      # maximum characters per attachment included in the question
TOTAL_CHARS = 100000    # total for all attachments (command-line arguments have a length limit)
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".htm", ".yaml", ".yml",
            ".log", ".ini", ".toml", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp", ".h",
            ".cs", ".rb", ".php", ".sh", ".sql", ".swift", ".kt", ".css", ".scss", ".vue", ".r", ".m", ".tex", ".srt", ".vtt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff", ".svg"}
KIND = {".docx": "Word", ".doc": "Word", ".xlsx": "Excel", ".xlsm": "Excel", ".xls": "Excel", ".pptx": "PowerPoint",
        ".pdf": "PDF", ".rtf": "RTF", ".odt": "ODT", ".csv": "CSV"}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RN = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def safe_name(name):
    name = os.path.basename(str(name or "").replace("\\", "/")).strip().lstrip(".")
    name = re.sub(r'[\x00-\x1f/:*?"<>|]', "_", name)[:120]
    return name or "file"


def _docx_text(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []

    def para(p):
        parts = []
        for el in p.iter():
            if el.tag == W + "t" and el.text:
                parts.append(el.text)
            elif el.tag == W + "tab":
                parts.append("\t")
            elif el.tag in (W + "br", W + "cr"):
                parts.append("\n")
        return "".join(parts)

    body = root.find(W + "body")
    for el in (body if body is not None else []):
        if el.tag == W + "p":
            out.append(para(el))
        elif el.tag == W + "tbl":
            for tr in el.iter(W + "tr"):
                out.append(" | ".join(" ".join(para(p) for p in tc.iter(W + "p")).strip() for tc in tr.findall(W + "tc")))
            out.append("")
    return "\n".join(out)


def _xlsx_text(path, max_rows=3000):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(SS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(SS + "t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                rels[r.get("Id")] = r.get("Target", "")
        out = []
        for sh in wb.iter(SS + "sheet"):
            target = rels.get(sh.get(RN + "id"), "")
            target = target.lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            if target not in names:
                continue
            out.append(f"## {sh.get('name')}")
            n = 0
            for row in ET.fromstring(z.read(target)).iter(SS + "row"):
                cells = []
                for c in row.findall(SS + "c"):
                    t, v = c.get("t"), c.find(SS + "v")
                    if t == "s" and v is not None:
                        val = shared[int(v.text)] if v.text and v.text.isdigit() and int(v.text) < len(shared) else ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(SS + "t"))
                    else:
                        val = v.text if v is not None and v.text is not None else ""
                    if val == "" and c.find(SS + "f") is not None and c.find(SS + "f").text:
                        val = "=" + c.find(SS + "f").text  # formula without a cached result: show the formula itself
                    cells.append(val)
                while cells and cells[-1] == "":
                    cells.pop()
                if cells:
                    out.append("\t".join(cells))
                    n += 1
                    if n >= max_rows:
                        out.append("…")
                        break
            out.append("")
    return "\n".join(out)


def _pptx_text(path):
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for i, n in enumerate(slides, 1):
            root = ET.fromstring(z.read(n))
            paras = ["".join(t.text or "" for t in p.iter(A_NS + "t")) for p in root.iter(A_NS + "p")]
            out.append(f"## Slide {i}\n" + "\n".join(x for x in paras if x.strip()))
    return "\n\n".join(out)


JXA_PDF = """ObjC.import('Quartz'); function run(a){var d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(a[0]));
return d.isNil()?'':ObjC.unwrap(d.string);}"""


def _pdf_text(path):
    exe = shutil.which("pdftotext", path=PATH_CACHE)
    if exe:
        r = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.decode("utf-8", "replace"), "pdftotext"
    if sys.platform == "darwin":  # PDFKit ships with macOS; nothing to install
        r = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", JXA_PDF, str(path)],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.decode("utf-8", "replace"), "PDFKit"
    return "", ""


def _read_text_file(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gb18030", "shift_jis", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def extract_text(path):
    """Return (text, method). When nothing can be extracted the text is empty; tools can still open the original file by path."""
    ext = Path(path).suffix.lower()
    try:
        if ext in TEXT_EXT:
            return _read_text_file(path), "text"
        if ext == ".docx":
            return _docx_text(path), "docx"
        if ext in (".xlsx", ".xlsm"):
            return _xlsx_text(path), "xlsx"
        if ext == ".pptx":
            return _pptx_text(path), "pptx"
        if ext == ".pdf":
            return _pdf_text(path)
        if ext in (".doc", ".rtf", ".odt", ".rtfd", ".webarchive") and sys.platform == "darwin":
            r = subprocess.run(["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, timeout=120)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace"), "textutil"
    except Exception as e:
        return "", f"error: {e}"
    return "", ""


def save_upload(name, stream, length):
    """Write an upload to the staging area, extract its text, and return the attachment info."""
    uid = uuid.uuid4().hex[:12]
    d = ATTACH_DIR / "_staging" / uid
    d.mkdir(parents=True, exist_ok=True)
    name = safe_name(name)
    path = d / name
    left = length
    with open(path, "wb") as f:
        while left > 0:
            chunk = stream.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    text, method = extract_text(path)
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
    (d / "_text.txt").write_text(text, encoding="utf-8")
    ext = path.suffix.lower()
    meta = {"id": uid, "name": name, "size": path.stat().st_size, "ext": ext,
            "kind": "image" if ext in IMAGE_EXT else KIND.get(ext, "text" if ext in TEXT_EXT else "file"),
            "chars": len(text), "method": method if text else ""}
    (d / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def claim_attachments(ids, rid, lang):
    """Move staged attachments into this round's folder and return the list of attachment info."""
    metas = []
    for uid in [str(x) for x in ids or []][:20]:
        if not re.fullmatch(r"[0-9a-f]{12}", uid):
            continue
        src = ATTACH_DIR / "_staging" / uid
        if not (src / "_meta.json").exists():
            raise ValueError(M(lang, "attach_missing", name=uid))
        dst = ATTACH_DIR / rid / uid
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        meta = json.loads((dst / "_meta.json").read_text(encoding="utf-8"))
        meta["path"] = str(dst / meta["name"])
        metas.append(meta)
    return metas


def attachment_block(metas, lang, budget=TOTAL_CHARS, per_file=FILE_CHARS, with_paths=True):
    """Build one block of text from the attachments: the extracted content plus the original file paths (tools may open them read-only)."""
    if not metas:
        return ""
    lines = [MT(lang, "attach_intro_paths" if with_paths else "attach_intro_text", n=len(metas)), ""]
    left = budget
    for i, m in enumerate(metas, 1):
        lines.append(MT(lang, "attach_header", i=i, name=m["name"], kind=m["kind"], chars=f"{m['chars']:,}"))
        if with_paths and m.get("path"):
            lines.append(MT(lang, "attach_path") + m["path"])
        text = ""
        try:
            text = (Path(m["path"]).parent / "_text.txt").read_text(encoding="utf-8")
        except Exception:
            pass
        if not text:
            lines.append(MT(lang, "attach_no_text_paths" if with_paths else "attach_no_text"))
        else:
            cap = max(0, min(per_file, left))
            if len(text) > cap:
                text = text[:cap] + MT(lang, "attach_trunc_paths" if with_paths else "attach_trunc", cap=f"{cap:,}")
            left -= min(len(text), cap)
            lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n\n"


def copy_attachments(src_rid):
    """On rerun: copy a round's attachments back to staging and return the new attachment IDs."""
    r = q("SELECT attachments FROM runs WHERE id=?", (src_rid,), one=True)
    ids = []
    for m in json.loads((r or {}).get("attachments") or "[]"):
        src = ATTACH_DIR / safe_name(src_rid) / safe_name(m.get("id", ""))
        if not (src / "_meta.json").exists():
            continue
        uid = uuid.uuid4().hex[:12]
        dst = ATTACH_DIR / "_staging" / uid
        shutil.copytree(src, dst)
        meta = json.loads((dst / "_meta.json").read_text(encoding="utf-8"))
        meta["id"] = uid
        (dst / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        ids.append(uid)
    return ids


def cleanup_staging(max_age=86400):
    st = ATTACH_DIR / "_staging"
    if st.exists():
        for d in st.iterdir():
            try:
                if time.time() - d.stat().st_mtime > max_age:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


PEER_CHARS, PEERS_TOTAL = 12000, 36000


def peer_block(peers, lang="en"):
    """For follow-ups: give the current tool the other models' answers from the previous round (labeled by model) for cross-model comparison."""
    if not peers:
        return ""
    parts = [MT(lang, "peers_intro")]
    left = PEERS_TOTAL
    for name, text in peers:
        cap = max(2000, min(PEER_CHARS, left))
        text = text.strip()
        if len(text) > cap:
            text = text[:cap] + MT(lang, "truncated")
        left -= len(text)
        parts.append(MT(lang, "peer_header", name=name) + f"\n{text}\n")
    return "\n".join(parts) + "\n"


def build_transcript(history, prompt, lang="en"):
    parts = [MT(lang, "transcript_intro")]
    for i, (q_, a_) in enumerate(history, 1):
        a_ = (a_ or MT(lang, "no_answer")).strip()
        if len(a_) > 6000:
            a_ = a_[:6000] + MT(lang, "truncated")
        parts.append(MT(lang, "transcript_round", i=i, q=q_, a=a_))
    parts.append(MT(lang, "new_question", prompt=prompt))
    return "\n".join(parts)


def kill(p):
    try:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGTERM)
        else:
            p.terminate()
    except Exception:
        pass


MODEL_RE = re.compile(r"(?m)^\s*model:\s*([\w.\-:/@]+)")  # e.g. "model: gpt-…" printed by Codex on stderr


def exec_tool(run, name, spec, prompt, cwd, timeout, model="", effort="", session="", history=None, chat=False,
              peers=None,
              attach="", attach_dir="", attach_n=0):
    base, extra, note = list(spec["cmd"]), [], ""
    if attach_dir and cwd and spec.get("add_dir_args"):  # the tool runs in the workspace, so also allow reading the attachments folder
        extra += [a.replace("{dir}", attach_dir) for a in spec["add_dir_args"]]
    if model and spec.get("model_flag"):
        extra += [spec["model_flag"], model]
    if effort and spec.get("effort_args"):
        extra += [a.replace("{effort}", effort) for a in spec["effort_args"]]
    if (chat and name == "codex" and base[1:2] == ["exec"]
            and not any(arg.startswith("model_verbosity=") for arg in (*base, *extra))):
        extra += ["-c", 'model_verbosity="high"']
    rargs = [a.replace("{session}", session) for a in spec.get("resume_args") or []] if session else []
    if rargs and spec.get("resume_pos") != "prompt":
        extra += rargs
    if extra:
        i = base.index("{prompt}") if "{prompt}" in base else len(base)
        if i > 0 and base[i - 1].startswith("-"):  # e.g. agy -p {prompt}: insert before -p so "-p question" stays together
            i -= 1
        base[i:i] = extra
    if rargs and spec.get("resume_pos") == "prompt":  # subcommand form: placed right before the question
        i = base.index("{prompt}") if "{prompt}" in base else len(base)
        base[i:i] = rargs
    lang = getattr(run, "lang", "en")
    if rargs:
        note = M(lang, "resume_native", name=name, sid=session[:12])
    elif history:
        prompt = build_transcript(history, prompt, lang)
        note = M(lang, "resume_transcript", n=len(history))
    pblock = peer_block(peers, lang)
    if attach or pblock:  # attachment content and the other models' answers go before the question
        prompt = attach + pblock + MT(lang, "question_label") + prompt
    if pblock:
        note += M(lang, "peers_note", names=", ".join(n for n, _ in peers))
    uses_openai = spec.get("transport") == "openai_responses"
    if chat and not uses_openai:  # chat mode: instructions are added only when sending to the tool; the stored question stays unchanged
        prompt = MT(lang, "chat_prefix") + (prompt if attach or pblock else MT(lang, "question_label") + prompt)
        note += M(lang, "chat_note")
    cmd = [prompt if a == "{prompt}" else a.replace("{prompt}", prompt) for a in base]
    if uses_openai:
        options = ["--lang", lang]
        if chat:
            options += ["--instructions", MT(lang, "chat_prefix").strip()]
            note += M(lang, "chat_note")
        cmd = [sys.executable, str(HERE / "arena_openai.py"), *options, *cmd[1:-1]]
    shown = " ".join(shlex.quote(a) for a in base)
    out, log, t0, status = [], [], time.time(), "ok"
    actual, errhead, sess = [""], [""], [""]
    run.emit({"tool": name, "type": "start", "cmd": shown, "model": model, "effort": effort})
    if attach:
        note += M(lang, "attach_note", n=attach_n or 1, chars=f"{len(attach):,}")
    env = child_env()
    stripped = [k for k in spec.get("strip_env") or [] if env.pop(k, None) is not None]
    if stripped:  # e.g. ANTHROPIC_API_KEY would override the claude.ai subscription sign-in
        note += M(lang, "env_stripped", names=", ".join(stripped))
    if note:
        log.append(note)
        run.emit({"tool": name, "type": "log", "data": note})
    if uses_openai:
        env["OPENAI_API_KEY"] = os.environ.get("ARENA_OPENAI_API_KEY", "")
    exe = sys.executable if uses_openai else which(cmd[0])
    use_pty = bool(spec.get("pty")) and os.name == "posix"
    out_fd = None
    api_input = None
    try:
        if not exe:
            raise FileNotFoundError(cmd[0])
        if uses_openai:
            api_input = tempfile.TemporaryFile()
            api_input.write(prompt.encode("utf-8"))
            api_input.seek(0)
        if use_pty:
            import pty, termios, fcntl, struct
            out_fd, slave = pty.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[1] &= ~termios.OPOST  # do not turn \n into \r\n
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 200, 400, 0, 0))  # wider, to avoid line wrapping
            env["TERM"] = "dumb"
            try:
                p = subprocess.Popen([exe] + cmd[1:], stdout=slave, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                     cwd=cwd, env=env, start_new_session=True)
            finally:
                os.close(slave)
        else:
            p = subprocess.Popen([exe] + cmd[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=api_input if uses_openai else subprocess.DEVNULL,
                                 cwd=cwd, env=env, start_new_session=(os.name == "posix"))
    except FileNotFoundError:
        p, status = None, "not_installed"
        log.append(M(lang, "cmd_missing", exe=cmd[0], cfg=CFG_PATH.name))
    except Exception as e:
        p, status = None, "start_failed"
        log.append(f"{e}\n")
    finally:
        if api_input is not None:
            api_input.close()
    if p:
        run.procs[name] = p
        parser = PARSERS[spec["parser"]](lang) if spec.get("parser") in PARSERS else None
        ptx = PtyText() if use_pty else None

        def push(kind, text):
            if kind == "session":
                if text:
                    sess[0] = text
                return
            if kind == "meta":
                if text and not actual[0]:
                    actual[0] = text
                    run.emit({"tool": name, "type": "meta", "model": text})
                return
            if text:
                (out if kind == "out" else log).append(text)
                run.emit({"tool": name, "type": kind, "data": text})

        def pump(fd, is_err):
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                try:
                    b = os.read(fd, 4096)
                except OSError:  # the pseudo-terminal returns EIO after the child process exits
                    b = b""
                txt = dec.decode(b, final=not b)
                if ptx and not is_err:
                    txt = ptx.feed(txt) + ("" if b else ptx.flush())
                if is_err:
                    push("log", txt)
                    if not actual[0] and len(errhead[0]) < 8000:
                        errhead[0] += txt
                        m = MODEL_RE.search(errhead[0])
                        if m:
                            push("meta", m.group(1))
                else:
                    for k, v in (parser.feed(txt) if parser else [("out", txt)]):
                        push(k, v)
                if not b:
                    break
            if parser and not is_err:
                for k, v in parser.flush():
                    push(k, v)

        ths = [threading.Thread(target=pump, args=(out_fd if use_pty else p.stdout.fileno(), False), daemon=True),
               threading.Thread(target=pump, args=(p.stderr.fileno(), True), daemon=True)]
        for th in ths:
            th.start()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            status = "timeout"
            kill(p)
            p.wait()
        for th in ths:
            th.join(5)
        if out_fd is not None:
            try:
                os.close(out_fd)
            except OSError:
                pass
        if run.cancelled:
            status = "canceled"
        elif status == "ok" and p.returncode != 0:
            status = f"exit {p.returncode}"
    secs = round(time.time() - t0, 1)
    if not p:
        run.emit({"tool": name, "type": "log", "data": "".join(log)})
    q("INSERT OR REPLACE INTO outputs(run_id, tool, status, seconds, output, log, cmd, model, actual_model, effort, session) "
      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (run.id, name, status, secs, "".join(out), "".join(log), shown, model, actual[0], effort, sess[0]))
    run.emit({"tool": name, "type": "done", "status": status, "seconds": secs})
    with run.cond:
        run.pending.discard(name)
        if not run.pending:
            run.finished_at = time.time()
            run.events.append({"type": "end"})
            if run.judge and not run.cancelled:  # automatic scoring
                threading.Thread(target=start_judge, args=(run.id, run.judge_model, run.lang), daemon=True).start()
        run.cond.notify_all()


# ---------------- Scoring (anonymous review by Grok or another third-party model) ----------------
JUDGING = set()
JUDGE_KEYS = ("accuracy", "completeness", "clarity", "usefulness")


def build_judge_prompt(question, earlier, answers, lang="en"):
    J = lambda key: loc(lang, "judge", key)
    lines = list(J("intro")) + [""]
    if earlier:
        lines.append(J("earlier_intro"))
        lines += [f"{i}. {x}" for i, x in enumerate(earlier, 1)]
        lines += [J("earlier_note"), ""]
    lines += [J("question_head"), question, ""]
    for lab, text in answers:
        t = text.strip()
        if len(t) > 12000:
            t = t[:12000] + MT(lang, "truncated")
        lines += [fmt(J("answer_head"), label=lab), t, ""]
    lines += list(J("criteria")) + ["", J("format_intro"), J("format_example"), J("format_note")]
    return "\n".join(lines)


def parse_judge(text):
    t = ANSI_RE.sub("", text or "")
    cands = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    depth, start = 0, None
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                cands.append(t[start:i + 1])
    for c in sorted(cands, key=len, reverse=True):
        try:
            o = json.loads(c)
        except ValueError:
            continue
        if isinstance(o, dict) and isinstance(o.get("scores"), dict):
            return o
    return None


def strip_answer_word(label):
    """Strip the localized word for "Answer" from a label: 'Answer A' -> 'A'."""
    for w in parse_words("answer_word"):
        label = label.replace(w.upper(), "")
    return label.strip()


def norm_conflicts(o, mapping):
    """Clean up the judge's consensus and conflicts, and map the anonymous labels back to tool names."""
    def lab(k):
        k = strip_answer_word(str(k).strip().upper())
        return mapping.get(k)
    cons = [str(x).strip() for x in (o.get("consensus") or []) if str(x).strip()][:8] if isinstance(o.get("consensus"), list) else []
    out = []
    for c in (o.get("conflicts") or [])[:10] if isinstance(o.get("conflicts"), list) else []:
        if not isinstance(c, dict) or not str(c.get("topic") or "").strip():
            continue
        pos = {}
        for k, v in (c.get("positions") or {}).items() if isinstance(c.get("positions"), dict) else []:
            t = lab(k)
            if t:
                pos[t] = str(v or "").strip()
        kind = str(c.get("kind") or "").strip().lower()
        out.append({"topic": str(c["topic"]).strip(), "kind": kind if kind in ("fact", "advice", "emphasis") else "advice",
                    "positions": pos, "verdict": str(c.get("verdict") or "").strip()})
    return cons, out


def judgment(rid, lang="en"):
    j = q("SELECT * FROM judgments WHERE run_id=?", (rid,), one=True)
    if j:
        j["mapping"] = json.loads(j.get("mapping") or "{}")
        j["result"] = json.loads(j.get("result") or "{}")
        j["scores"] = q("SELECT tool, overall, detail, comment FROM judge_scores WHERE run_id=?", (rid,))
        for x in j["scores"]:
            x["detail"] = json.loads(x["detail"] or "{}")
        j["running"] = rid in JUDGING
        if j["status"] == "running" and not j["running"]:
            j["status"], j["reason"] = "error", M(lang, "judge_interrupted")
        j["elapsed"] = round(time.time() - (j.get("created_at") or time.time()), 1)
    return j


def start_judge(rid, model="", lang="en"):
    if rid in JUDGING:
        return M(lang, "judge_busy")
    cfg = load_cfg()
    jn = judge_tool(cfg)
    if not jn:
        return M(lang, "judge_none")
    spec = cfg["tools"][jn]
    label = spec.get("label") or jn
    exe = which(spec["cmd"][0])
    if not exe:
        return M(lang, "judge_missing", exe=spec['cmd'][0], label=label)
    run = q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    if not run:
        return M(lang, "run_missing")
    outs = [o for o in q("SELECT tool, output, status FROM outputs WHERE run_id=?", (rid,))
            if o["status"] == "ok" and (o["output"] or "").strip()]
    if len(outs) < 2:
        return M(lang, "judge_two")
    thread = run.get("thread") or rid
    earlier = [x["prompt"] for x in q("SELECT prompt FROM runs WHERE (id=? OR thread=?) AND created_at<? "
                                       "ORDER BY created_at", (thread, thread, run["created_at"]))]
    random.shuffle(outs)  # shuffle and anonymize to avoid position and brand bias
    labels = "ABCDEFGH"
    mapping = {labels[i]: o["tool"] for i, o in enumerate(outs)}
    prompt = build_judge_prompt(run["prompt"], earlier, [(labels[i], o["output"]) for i, o in enumerate(outs)], lang)
    metas = json.loads(run.get("attachments") or "[]")
    if metas:  # give the judge the attachment content too, to check the answers' understanding of it
        prompt = (MT(lang, "judge_attach_intro")
                  + attachment_block(metas, lang, budget=40000, per_file=20000, with_paths=False) + prompt)
    q("DELETE FROM judge_scores WHERE run_id=?", (rid,))
    q("INSERT OR REPLACE INTO judgments(run_id, judge, model, status, best, reason, mapping, result, raw, seconds, created_at) "
      "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rid, jn, model, "running", "", "", json.dumps(mapping), "{}", "", 0, time.time()))
    JUDGING.add(rid)

    def work():
        t0 = time.time()
        base = list(spec["cmd"])
        if model and spec.get("model_flag"):
            i = base.index("{prompt}") if "{prompt}" in base else len(base)
            if i > 0 and base[i - 1].startswith("-"):
                i -= 1
            base[i:i] = [spec["model_flag"], model]
        cmd = [prompt if a == "{prompt}" else a for a in base]
        status, best, reason, result, raw = "error", "", "", {}, ""
        last = [0.0]

        def progress(text):  # save the output so far every 2 seconds so the page can show progress
            if time.time() - last[0] > 2:
                last[0] = time.time()
                q("UPDATE judgments SET raw=?, seconds=? WHERE run_id=?", (text[-20000:], round(time.time() - t0, 1), rid))

        try:
            sec = cfg.get("judge_timeout", 300)
            rc, out, err = run_streaming(exe, cmd[1:], spec, tempfile.mkdtemp(prefix="arena-judge-"), sec, progress)
            raw = out + ("\n[stderr]\n" + err if err.strip() else "")
            o = parse_judge(out) if rc == 0 else None
            if rc is None:
                tail = ANSI_RE.sub("", raw).strip()[-300:] or M(lang, "judge_no_output")
                reason = M(lang, "judge_timeout_tail", label=label, sec=sec, tail=tail)
            elif not o:
                reason = M(lang, "judge_exit", label=label, code=rc) if rc else M(lang, "judge_parse", label=label)
            else:
                rows = []
                for lab, tool in mapping.items():
                    sc = (o["scores"].get(lab) or next((o["scores"][f"{w} {lab}"] for w in parse_words("answer_word")
                                                         if f"{w} {lab}" in o["scores"]), None) or {})
                    try:
                        overall = float(sc.get("overall"))
                    except (TypeError, ValueError):
                        overall = None
                    detail = {k: sc.get(k) for k in JUDGE_KEYS if sc.get(k) is not None}
                    rows.append((rid, tool, overall, json.dumps(detail), str(sc.get("comment") or "")))
                b = str(o.get("best") or "").strip().upper()
                best = "tie" if b in {w.upper() for w in parse_words("tie")} | {"TIE"} else mapping.get(strip_answer_word(b), "")
                o["consensus_n"], o["conflicts_n"] = norm_conflicts(o, mapping)
                reason, result, status = str(o.get("reason") or ""), o, "ok"
                for row in rows:
                    q("INSERT OR REPLACE INTO judge_scores(run_id, tool, overall, detail, comment) VALUES(?,?,?,?,?)", row)
        except Exception as e:
            reason = str(e)
        q("UPDATE judgments SET status=?, best=?, reason=?, result=?, raw=?, seconds=? WHERE run_id=?",
          (status, best, reason, json.dumps(result, ensure_ascii=False), raw[-20000:], round(time.time() - t0, 1), rid))
        JUDGING.discard(rid)

    threading.Thread(target=work, daemon=True).start()
    return ""


def run_streaming(exe, args, spec, cwd, timeout, on_text):
    """Run a CLI and call on_text(accumulated output) as it reads. Return (exit code or None on timeout, output, stderr)."""
    env = child_env()
    use_pty = bool(spec.get("pty")) and os.name == "posix"
    buf, err, out_fd = [], [], None
    if use_pty:
        import pty, termios, fcntl, struct
        out_fd, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[1] &= ~termios.OPOST
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 200, 400, 0, 0))
        env["TERM"] = "dumb"
        try:
            p = subprocess.Popen([exe] + args, stdout=slave, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 cwd=cwd, env=env, start_new_session=True)
        finally:
            os.close(slave)
    else:
        p = subprocess.Popen([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             cwd=cwd, env=env, start_new_session=(os.name == "posix"))
        out_fd = p.stdout.fileno()
    ptx = PtyText() if use_pty else None

    def pump_out():
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            try:
                b = os.read(out_fd, 4096)
            except OSError:
                b = b""
            t = dec.decode(b, final=not b)
            if ptx:
                t = ptx.feed(t) + ("" if b else ptx.flush())
            if t:
                buf.append(t)
                on_text("".join(buf))
            if not b:
                break

    def pump_err():
        for chunk in iter(lambda: p.stderr.read(4096), b""):
            err.append(chunk.decode("utf-8", "replace"))

    ths = [threading.Thread(target=pump_out, daemon=True), threading.Thread(target=pump_err, daemon=True)]
    for th in ths:
        th.start()
    rc = None
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill(p)
        p.wait()
    for th in ths:
        th.join(5)
    if use_pty:
        try:
            os.close(out_fd)
        except OSError:
            pass
    return rc, "".join(buf), "".join(err)


def start_run(prompt, tools, cwd, tag, models=None, efforts=None, parent=None, judge=False, judge_model="", lang="en",
              chat=False, attachments=None, share_peers=True, include_openai=True):
    cfg = load_cfg()
    on = enabled_tools(cfg)
    thread, parent_id, sessions, histories, prev = "", "", {}, {}, []
    if parent:  # follow-up: reuse the previous round's tools, models, effort and folder, and find each tool's session ID
        p = q("SELECT * FROM runs WHERE id=?", (parent,), one=True)
        if not p:
            raise ValueError(M(lang, "parent_missing"))
        thread, parent_id = p.get("thread") or p["id"], p["id"]
        tools = json.loads(p["tools"] or "[]")
        if not include_openai and "openai" in tools:
            tools = [t for t in tools if t != "openai" and t in on]
            if not tools:
                raise ValueError(M(lang, "need_openai"))
        q("UPDATE runs SET thread=? WHERE id=? AND (thread IS NULL OR thread='')", (thread, thread))
        models, efforts = json.loads(p.get("models") or "{}"), json.loads(p.get("efforts") or "{}")
        cwd, tag, chat = p.get("cwd") or None, p.get("tag") or "", bool(p.get("chat"))
        root = q("SELECT * FROM runs WHERE id=?", (thread,), one=True) or p
        workdir = p.get("workdir") or legacy_workdir(root)  # sessions live where the thread started
        for t in tools:
            rows = q("""SELECT r.prompt, o.output, o.session, o.status FROM runs r
                        LEFT JOIN outputs o ON o.run_id=r.id AND o.tool=?
                        WHERE r.id=? OR r.thread=? ORDER BY r.created_at""", (t, thread, thread))
            histories[t] = [(x["prompt"], x["output"] or "") for x in rows]
            ok = [x["session"] for x in rows if x["session"] and x["status"] == "ok"]
            sessions[t] = ok[-1] if ok else ""
        if share_peers:  # all successful answers from the round being followed up, with model names
            outs = q("SELECT tool, output, status, model, actual_model FROM outputs WHERE run_id=?", (parent_id,))
            for o in sorted(outs, key=lambda o: tools.index(o["tool"]) if o["tool"] in tools else 99):
                if o["status"] == "ok" and (o["output"] or "").strip() and o["tool"] in cfg["tools"]:
                    label = cfg["tools"][o["tool"]].get("label") or o["tool"].capitalize()
                    m = o.get("actual_model") or o.get("model") or ""
                    prev.append((o["tool"], MT(lang, "peer_label", label=label, model=m) if m else label,
                                 o["output"]))
    tools = [t for t in tools if t in on] or [t for t in on if cfg["tools"][t].get("default_on", True)]
    if not tools:
        raise ValueError(M(lang, "no_tools"))
    for rid0 in [k for k, r in RUNS.items() if r.finished_at and time.time() - r.finished_at > 1800]:
        RUNS.pop(rid0, None)  # remove from memory 30 minutes after finishing; replay from the database after that
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    models = {t: str((models or {}).get(t) or "").strip() for t in tools}
    efforts = {t: e if re.fullmatch(r"[a-z]{1,12}", e := str((efforts or {}).get(t) or "").strip()) else ""
               for t in tools}
    metas = claim_attachments(attachments, rid, lang) if attachments else []
    attach = attachment_block(metas, lang)
    api_attach = attachment_block(metas, lang, with_paths=False) if any(
        cfg["tools"][t].get("transport") == "openai_responses" for t in tools
    ) else ""
    attach_dir = str(ATTACH_DIR / rid) if metas else ""
    run_cwd = workdir if parent else (cwd or workspace_dir(cfg))  # one folder per thread, so every tool can resume its session there
    peers = {t: [(lab, out) for tt, lab, out in prev if tt != t] for t in tools}
    shared = any(peers.values())
    q("INSERT INTO runs(id, created_at, prompt, cwd, tag, tools, winner, models, efforts, thread, parent, chat, attachments, peers, "
      "workdir) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (rid, time.time(), prompt, cwd or "", tag, json.dumps(tools), "", json.dumps(models), json.dumps(efforts), thread or rid,
       parent_id, int(bool(chat)), json.dumps(metas, ensure_ascii=False), int(shared), run_cwd))
    run = Run(rid, tools)
    run.judge, run.judge_model, run.lang = bool(judge), str(judge_model or ""), lang
    RUNS[rid] = run
    for t in tools:
        spec = cfg["tools"][t]
        uses_openai = spec.get("transport") == "openai_responses"
        threading.Thread(target=exec_tool, args=(run, t, spec, prompt, run_cwd, cfg.get("timeout", 600),
                                                 models[t], efforts[t], sessions.get(t, ""),
                                                 None if sessions.get(t) else histories.get(t), bool(chat),
                                                 peers.get(t), api_attach if uses_openai else attach,
                                                 attach_dir if not uses_openai else "", len(metas)),
                         daemon=True).start()
    return rid, tools, models, efforts, shared


def run_detail(rid, with_turns=True, lang="en"):
    r = q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    if not r:
        return None
    r["tools"] = json.loads(r["tools"] or "[]")
    r["models"] = json.loads(r.get("models") or "{}")
    r["efforts"] = json.loads(r.get("efforts") or "{}")
    r["attachments"] = [{k: v for k, v in m.items() if k != "path"} for m in json.loads(r.get("attachments") or "[]")]
    r["outputs"] = q("SELECT * FROM outputs WHERE run_id=?", (rid,))
    r["ratings"] = q("SELECT tool, score, note FROM ratings WHERE run_id=?", (rid,))
    live = RUNS.get(rid)
    r["running"] = bool(live and not live.finished_at)
    r["judgment"] = judgment(rid, lang)
    r["auto_judge"] = bool(live and live.judge)
    if with_turns:
        root = r.get("thread") or rid
        ids = [x["id"] for x in q("SELECT id FROM runs WHERE id=? OR thread=? ORDER BY created_at", (root, root))]
        r["turns"] = [run_detail(i, False, lang) if i != rid else {k: v for k, v in r.items() if k != "turns"} for i in ids]
    return r


STATS_SQL = """
SELECT o.tool, {model_col} model, {effort_col} effort, COUNT(*) n, SUM(o.status='ok') ok,
  AVG(CASE WHEN o.status='ok' THEN o.seconds END) secs,
  AVG(CASE WHEN o.status='ok' THEN LENGTH(o.output) END) chars,
  AVG(r.score) score, COUNT(r.score) rated,
  SUM(runs.winner=o.tool) wins, SUM(runs.winner<>'' AND runs.winner IS NOT NULL) judged
FROM outputs o JOIN runs ON runs.id=o.run_id
LEFT JOIN ratings r ON r.run_id=o.run_id AND r.tool=o.tool AND r.score>0
WHERE (?='' OR runs.tag=?) GROUP BY o.tool{group_extra} ORDER BY o.tool, score DESC
"""


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _guard(self, u):
        self.lang = norm_lang(self.headers.get("X-Arena-Lang"))
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self._json({"error": "bad host"}, 403)  # guard against DNS rebinding
            return False
        if u.path == "/":
            return True
        tok = self.headers.get("X-Arena-Token") or parse_qs(u.query).get("token", [""])[0]
        if not secrets.compare_digest(tok, TOKEN):
            self._json({"error": M(self.lang, "bad_token")}, 403)
            return False
        global LAST_SEEN
        LAST_SEEN = time.time()
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if not self._guard(u):
            return
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/":
            page_file = HERE / "arena.html"  # use arena.html from the same folder if present, so the interface can be edited separately
            src = page_file.read_text(encoding="utf-8") if page_file.exists() else PAGE
            ui = {c: LOCALES[c].get("ui", {}) for c in LANGS}
            langs = [{"code": c, **LOCALES[c].get("_meta", {})} for c in LANGS]
            js = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
            body = (src.replace("__TOKEN__", TOKEN).replace("__I18N__", js(ui)).replace("__LANGS__", js(langs))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.startswith("/api/judge/"):
            j = judgment(p.rsplit("/", 1)[1], self.lang)
            self._json(j) if j else self._json({"error": M(self.lang, "no_judgment")}, 404)
        elif p == "/api/doctor":
            self._json(doctor(self.lang))
        elif p == "/api/prompts":
            self._json(list_prompts(self.lang))
        elif p == "/api/jobs":
            self._json(JOBS)
        elif p == "/api/ping":
            self._json({"ok": True})
        elif p == "/api/config":
            cfg = load_cfg()
            tags = [r["tag"] for r in q("SELECT DISTINCT tag FROM runs WHERE tag<>'' ORDER BY tag")]
            self._json({"tools": {k: {"color": v.get("color", "#888"), "label": v.get("label") or k.capitalize(),
                                      "models": MODEL_CACHE.get(k) or v.get("models", []),
                                      "dynamic": bool(v.get("models_cmd")),
                                      "continue_app": (v.get("continue_app") or "") if (v.get("continue_url") or v.get("continue_desktop")) else "",
                                      "continue_terminal": bool(v.get("continue_terminal")),
                                      "continue_via_url": bool(v.get("continue_url")),
                                      "flag": bool(v.get("model_flag")), "default_label": tool_text(self.lang, k, v, "default_label") or loc(self.lang, "ui", "default"),
                                      "efforts": v.get("efforts", []) if v.get("effort_args") else [],
                                      "default_on": v.get("default_on", True),
                                      "ready": v.get("transport") != "openai_responses" or openai_ready()}
                                  for k, v in cfg["tools"].items()},
                        "order": enabled_tools(cfg), "judge": judge_tool(cfg), "tags": tags})
        elif p.startswith("/api/file/"):  # open an attachment's original file for a round
            parts = p.split("/")
            if len(parts) != 6:
                return self._json({"error": "not found"}, 404)
            rid, uid, name = parts[3], parts[4], urllib.parse.unquote(parts[5])
            meta_p = ATTACH_DIR / safe_name(rid) / safe_name(uid) / "_meta.json"
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except Exception:
                return self._json({"error": "not found"}, 404)
            fp = meta_p.parent / meta["name"]
            if not fp.is_file() or meta["name"] != name:
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(meta["name"])[0] or "application/octet-stream"
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + urllib.parse.quote(meta["name"]))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox")
            self.end_headers()
            self.wfile.write(data)
        elif p.startswith("/api/stream/"):
            self._sse(p.rsplit("/", 1)[1])
        elif p.startswith("/api/run/"):
            d = run_detail(p.rsplit("/", 1)[1], True, self.lang)
            self._json(d) if d else self._json({"error": "not found"}, 404)
        elif p == "/api/history":
            kw, tag = qs.get("q", ""), qs.get("tag", "")
            limit = max(1, min(int(qs.get("limit") or 200), 500))
            rows = q("""SELECT runs.id, created_at, prompt, tag, winner, tools, models, efforts, cwd, chat, attachments,
                 (SELECT group_concat(tool||' '||score, '，') FROM ratings r WHERE r.run_id=runs.id AND score>0) scores,
                 (SELECT COUNT(*) FROM runs c WHERE c.thread=runs.id) nturns,
                 (SELECT c.winner FROM runs c WHERE c.id=runs.id OR c.thread=runs.id ORDER BY c.created_at DESC LIMIT 1) last_winner,
                 (SELECT group_concat(tool||':'||status, '|') FROM outputs o WHERE o.run_id=
                    (SELECT c.id FROM runs c WHERE c.id=runs.id OR c.thread=runs.id ORDER BY c.created_at DESC LIMIT 1)) st
                 FROM runs WHERE (thread IS NULL OR thread='' OR thread=runs.id)
                   AND (prompt LIKE ? OR EXISTS(SELECT 1 FROM runs c WHERE c.thread=runs.id AND c.prompt LIKE ?))
                   AND (?='' OR tag=?) ORDER BY created_at DESC LIMIT ?""",
                     (f"%{kw}%", f"%{kw}%", tag, tag, limit))
            for r in rows:
                r["attachments"] = [{k: v for k, v in m.items() if k != "path"} for m in json.loads(r.get("attachments") or "[]")]
            self._json({"runs": rows})
        elif p == "/api/stats":
            tag = qs.get("tag", "")
            total = q("SELECT COUNT(*) n, SUM(winner<>'') judged FROM runs WHERE (?='' OR tag=?)", (tag, tag), one=True)
            by_model = qs.get("by") == "model"
            sql = STATS_SQL.format(model_col="COALESCE(o.model,'')" if by_model else "''",
                                   effort_col="COALESCE(o.effort,'')" if by_model else "''",
                                   group_extra=", model, effort" if by_model else "")
            jrows = q("""SELECT s.tool, AVG(s.overall) avg, COUNT(s.overall) n, SUM(j.best=s.tool) best
                         FROM judge_scores s JOIN judgments j ON j.run_id=s.run_id JOIN runs ON runs.id=s.run_id
                         WHERE j.status='ok' AND (?='' OR runs.tag=?) GROUP BY s.tool ORDER BY avg DESC""", (tag, tag))
            agree = q("""SELECT COUNT(*) total, SUM(runs.winner=j.best) agree FROM runs JOIN judgments j ON j.run_id=runs.id
                         WHERE j.status='ok' AND j.best<>'' AND runs.winner<>'' AND (?='' OR runs.tag=?)""", (tag, tag), one=True)
            self._json({"tools": q(sql, (tag, tag)), "total": total, "judge": jrows, "agree": agree})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._guard(u):
            return
        p = u.path
        if p == "/api/upload":  # raw byte upload; the file name is in X-File-Name (URL-encoded)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD_MB * 1024 * 1024:
                return self._json({"error": M(self.lang, "too_big", mb=MAX_UPLOAD_MB)}, 413)
            name = urllib.parse.unquote(self.headers.get("X-File-Name") or "file")
            return self._json(save_upload(name, self.rfile, n))
        try:
            b = self._body()
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if p == "/api/continue":
            ok, message, command = continue_session(str(b.get("run") or ""), str(b.get("tool") or ""),
                                                    str(b.get("action") or "app"), self.lang)
            return self._json({"ok": ok, "message": message, "command": command, "error": "" if ok else message},
                              200 if ok else 400)
        if p == "/api/prompts":
            try:
                return self._json(save_prompt(b))
            except ValueError:
                return self._json({"error": M(self.lang, "empty_prompt")}, 400)
        if p.startswith("/api/prompts/"):
            act, pid = (p.split("/")[3:5] + ["", ""])[:2]
            if act == "delete":
                q("DELETE FROM prompts WHERE id=?", (pid,))
            elif act == "use":
                q("UPDATE prompts SET uses=uses+1, last_used=? WHERE id=?", (time.time(), pid))
            elif act == "restore":
                return self._json({"restored": ensure_seeds(restore=True)})
            elif act == "pin":
                q("UPDATE prompts SET pinned=? WHERE id=?", (int(bool(b.get("pinned"))), pid))
            else:
                return self._json({"error": "not found"}, 404)
            return self._json({"ok": True})
        if p == "/api/run":
            prompt = (b.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": M(self.lang, "empty_prompt")}, 400)
            jflag, jmodel, chat = bool(b.get("judge")), str(b.get("judge_model") or ""), bool(b.get("chat"))
            att = b.get("attachments") if isinstance(b.get("attachments"), list) else []
            if b.get("copy_attachments_from"):
                att = att + copy_attachments(str(b["copy_attachments_from"]))
            if b.get("parent"):
                try:
                    rid, tools, models, efforts, shared = start_run(
                        prompt, [], None, "", parent=str(b["parent"]), judge=jflag, judge_model=jmodel, lang=self.lang,
                        attachments=att, share_peers=b.get("share_peers", True) is not False,
                        include_openai=b.get("include_openai", True) is not False)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"id": rid, "tools": tools, "models": models, "efforts": efforts, "parent": b["parent"],
                                   "peers": int(shared)})
            cwd = os.path.expanduser(b.get("cwd") or "").strip() or None
            if cwd and not os.path.isdir(cwd):
                return self._json({"error": M(self.lang, "no_dir", d=cwd)}, 400)
            models = b.get("models") if isinstance(b.get("models"), dict) else {}
            efforts = b.get("efforts") if isinstance(b.get("efforts"), dict) else {}
            try:
                rid, tools, models, efforts, _ = start_run(prompt, b.get("tools") or [], cwd, (b.get("tag") or "").strip(),
                                                        models, efforts, judge=jflag, judge_model=jmodel, lang=self.lang,
                                                        chat=chat, attachments=att)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            row = q("SELECT attachments FROM runs WHERE id=?", (rid,), one=True) or {}
            self._json({"id": rid, "tools": tools, "models": models, "efforts": efforts, "tag": b.get("tag") or "",
                        "chat": chat, "attachments": [{k: v for k, v in m.items() if k != "path"}
                                                      for m in json.loads(row.get("attachments") or "[]")]})
        elif p.startswith("/api/cancel/"):
            run = RUNS.get(p.rsplit("/", 1)[1])
            if run:
                run.cancelled = True
                for pr in run.procs.values():
                    if pr.poll() is None:
                        kill(pr)
            self._json({"ok": True})
        elif p.startswith("/api/judge/"):
            err = start_judge(p.rsplit("/", 1)[1], str(b.get("model") or ""), self.lang)
            self._json({"error": err}, 400) if err else self._json({"ok": True})
        elif p.startswith("/api/install/"):
            err = start_install(p.rsplit("/", 1)[1], self.lang)
            self._json({"error": err}, 400) if err else self._json({"ok": True})
        elif p.startswith("/api/login/"):
            err = open_login(p.rsplit("/", 1)[1], self.lang)
            self._json({"error": err}, 400) if err else self._json({"ok": True})
        elif p == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=shutdown, daemon=True).start()
        elif p == "/api/rate":
            q("INSERT OR REPLACE INTO ratings VALUES(?,?,?,?)",
              (b["run_id"], b["tool"], int(b.get("score") or 0), b.get("note") or ""))
            self._json({"ok": True})
        elif p == "/api/winner":
            q("UPDATE runs SET winner=? WHERE id=?", (b.get("winner") or "", b["run_id"]))
            self._json({"ok": True})
        elif p.startswith("/api/delete/"):  # deleting the first question deletes the whole conversation
            rid = p.rsplit("/", 1)[1]
            for x in q("SELECT id FROM runs WHERE id=? OR thread=?", (rid, rid)):
                shutil.rmtree(ATTACH_DIR / safe_name(x["id"]), ignore_errors=True)
                for t in ("outputs", "ratings", "judgments", "judge_scores"):
                    q(f"DELETE FROM {t} WHERE run_id=?", (x["id"],))
                q("DELETE FROM runs WHERE id=?", (x["id"],))
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def _sse(self, rid):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        send = lambda ev: self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
        try:
            run = RUNS.get(rid)
            if not run:  # no longer in memory: replay from the database
                for o in q("SELECT * FROM outputs WHERE run_id=?", (rid,)):
                    send({"tool": o["tool"], "type": "start", "cmd": o["cmd"], "model": o.get("model") or "",
                          "effort": o.get("effort") or ""})
                    if o.get("actual_model"):  # noqa
                        send({"tool": o["tool"], "type": "meta", "model": o["actual_model"]})
                    send({"tool": o["tool"], "type": "out", "data": o["output"]})
                    send({"tool": o["tool"], "type": "log", "data": o["log"]})
                    send({"tool": o["tool"], "type": "done", "status": o["status"], "seconds": o["seconds"]})
                send({"type": "end"})
                self.wfile.flush()
                return
            i = 0
            while True:
                with run.cond:
                    if i >= len(run.events):
                        run.cond.wait(15)
                    batch = run.events[i:]
                    i += len(batch)
                if not batch:
                    self.wfile.write(b": ping\n\n")
                for ev in batch:
                    send(ev)
                self.wfile.flush()
                if any(ev.get("type") == "end" for ev in batch):
                    return
        except (BrokenPipeError, ConnectionResetError):
            return


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Arena</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22%3E%3Cdefs%3E%3ClinearGradient id=%22ag%22 x1=%220%22 y1=%220%22 x2=%221%22 y2=%221%22%3E%3Cstop offset=%220%22 stop-color=%22%234C8DFF%22/%3E%3Cstop offset=%22.55%22 stop-color=%22%236D6AF7%22/%3E%3Cstop offset=%221%22 stop-color=%22%23A47CF2%22/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width=%2232%22 height=%2232%22 rx=%229%22 fill=%22url%28%23ag%29%22/%3E%3Crect x=%228%22 y=%2210%22 width=%224%22 height=%2214%22 rx=%222%22 fill=%22%23fff%22/%3E%3Crect x=%2214%22 y=%2215%22 width=%224%22 height=%229%22 rx=%222%22 fill=%22%23fff%22 fill-opacity=%22.88%22/%3E%3Crect x=%2220%22 y=%2212.5%22 width=%224%22 height=%2211.5%22 rx=%222%22 fill=%22%23fff%22 fill-opacity=%22.76%22/%3E%3Ccircle cx=%2224.6%22 cy=%227.4%22 r=%221.7%22 fill=%22%23fff%22/%3E%3C/svg%3E">
<!-- marked 12.0.2 (MIT) and DOMPurify 3.1.6 (Apache-2.0/MPL-2.0) are inlined so formatting works offline -->
<script>/**
 * marked v12.0.2 - a markdown parser
 * Copyright (c) 2011-2024, Christopher Jeffrey. (MIT Licensed)
 * https://github.com/markedjs/marked
 */
!function(e,t){"object"==typeof exports&&"undefined"!=typeof module?t(exports):"function"==typeof define&&define.amd?define(["exports"],t):t((e="undefined"!=typeof globalThis?globalThis:e||self).marked={})}(this,(function(e){"use strict";function t(){return{async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null}}function n(t){e.defaults=t}e.defaults={async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null};const s=/[&<>"']/,r=new RegExp(s.source,"g"),i=/[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)/,l=new RegExp(i.source,"g"),o={"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"},a=e=>o[e];function c(e,t){if(t){if(s.test(e))return e.replace(r,a)}else if(i.test(e))return e.replace(l,a);return e}const h=/&(#(?:\d+)|(?:#x[0-9A-Fa-f]+)|(?:\w+));?/gi;function p(e){return e.replace(h,((e,t)=>"colon"===(t=t.toLowerCase())?":":"#"===t.charAt(0)?"x"===t.charAt(1)?String.fromCharCode(parseInt(t.substring(2),16)):String.fromCharCode(+t.substring(1)):""))}const u=/(^|[^\[])\^/g;function k(e,t){let n="string"==typeof e?e:e.source;t=t||"";const s={replace:(e,t)=>{let r="string"==typeof t?t:t.source;return r=r.replace(u,"$1"),n=n.replace(e,r),s},getRegex:()=>new RegExp(n,t)};return s}function g(e){try{e=encodeURI(e).replace(/%25/g,"%")}catch(e){return null}return e}const f={exec:()=>null};function d(e,t){const n=e.replace(/\|/g,((e,t,n)=>{let s=!1,r=t;for(;--r>=0&&"\\"===n[r];)s=!s;return s?"|":" |"})).split(/ \|/);let s=0;if(n[0].trim()||n.shift(),n.length>0&&!n[n.length-1].trim()&&n.pop(),t)if(n.length>t)n.splice(t);else for(;n.length<t;)n.push("");for(;s<n.length;s++)n[s]=n[s].trim().replace(/\\\|/g,"|");return n}function x(e,t,n){const s=e.length;if(0===s)return"";let r=0;for(;r<s;){const i=e.charAt(s-r-1);if(i!==t||n){if(i===t||!n)break;r++}else r++}return e.slice(0,s-r)}function b(e,t,n,s){const r=t.href,i=t.title?c(t.title):null,l=e[1].replace(/\\([\[\]])/g,"$1");if("!"!==e[0].charAt(0)){s.state.inLink=!0;const e={type:"link",raw:n,href:r,title:i,text:l,tokens:s.inlineTokens(l)};return s.state.inLink=!1,e}return{type:"image",raw:n,href:r,title:i,text:c(l)}}class w{options;rules;lexer;constructor(t){this.options=t||e.defaults}space(e){const t=this.rules.block.newline.exec(e);if(t&&t[0].length>0)return{type:"space",raw:t[0]}}code(e){const t=this.rules.block.code.exec(e);if(t){const e=t[0].replace(/^ {1,4}/gm,"");return{type:"code",raw:t[0],codeBlockStyle:"indented",text:this.options.pedantic?e:x(e,"\n")}}}fences(e){const t=this.rules.block.fences.exec(e);if(t){const e=t[0],n=function(e,t){const n=e.match(/^(\s+)(?:```)/);if(null===n)return t;const s=n[1];return t.split("\n").map((e=>{const t=e.match(/^\s+/);if(null===t)return e;const[n]=t;return n.length>=s.length?e.slice(s.length):e})).join("\n")}(e,t[3]||"");return{type:"code",raw:e,lang:t[2]?t[2].trim().replace(this.rules.inline.anyPunctuation,"$1"):t[2],text:n}}}heading(e){const t=this.rules.block.heading.exec(e);if(t){let e=t[2].trim();if(/#$/.test(e)){const t=x(e,"#");this.options.pedantic?e=t.trim():t&&!/ $/.test(t)||(e=t.trim())}return{type:"heading",raw:t[0],depth:t[1].length,text:e,tokens:this.lexer.inline(e)}}}hr(e){const t=this.rules.block.hr.exec(e);if(t)return{type:"hr",raw:t[0]}}blockquote(e){const t=this.rules.block.blockquote.exec(e);if(t){let e=t[0].replace(/\n {0,3}((?:=+|-+) *)(?=\n|$)/g,"\n    $1");e=x(e.replace(/^ *>[ \t]?/gm,""),"\n");const n=this.lexer.state.top;this.lexer.state.top=!0;const s=this.lexer.blockTokens(e);return this.lexer.state.top=n,{type:"blockquote",raw:t[0],tokens:s,text:e}}}list(e){let t=this.rules.block.list.exec(e);if(t){let n=t[1].trim();const s=n.length>1,r={type:"list",raw:"",ordered:s,start:s?+n.slice(0,-1):"",loose:!1,items:[]};n=s?`\\d{1,9}\\${n.slice(-1)}`:`\\${n}`,this.options.pedantic&&(n=s?n:"[*+-]");const i=new RegExp(`^( {0,3}${n})((?:[\t ][^\\n]*)?(?:\\n|$))`);let l="",o="",a=!1;for(;e;){let n=!1;if(!(t=i.exec(e)))break;if(this.rules.block.hr.test(e))break;l=t[0],e=e.substring(l.length);let s=t[2].split("\n",1)[0].replace(/^\t+/,(e=>" ".repeat(3*e.length))),c=e.split("\n",1)[0],h=0;this.options.pedantic?(h=2,o=s.trimStart()):(h=t[2].search(/[^ ]/),h=h>4?1:h,o=s.slice(h),h+=t[1].length);let p=!1;if(!s&&/^ *$/.test(c)&&(l+=c+"\n",e=e.substring(c.length+1),n=!0),!n){const t=new RegExp(`^ {0,${Math.min(3,h-1)}}(?:[*+-]|\\d{1,9}[.)])((?:[ \t][^\\n]*)?(?:\\n|$))`),n=new RegExp(`^ {0,${Math.min(3,h-1)}}((?:- *){3,}|(?:_ *){3,}|(?:\\* *){3,})(?:\\n+|$)`),r=new RegExp(`^ {0,${Math.min(3,h-1)}}(?:\`\`\`|~~~)`),i=new RegExp(`^ {0,${Math.min(3,h-1)}}#`);for(;e;){const a=e.split("\n",1)[0];if(c=a,this.options.pedantic&&(c=c.replace(/^ {1,4}(?=( {4})*[^ ])/g,"  ")),r.test(c))break;if(i.test(c))break;if(t.test(c))break;if(n.test(e))break;if(c.search(/[^ ]/)>=h||!c.trim())o+="\n"+c.slice(h);else{if(p)break;if(s.search(/[^ ]/)>=4)break;if(r.test(s))break;if(i.test(s))break;if(n.test(s))break;o+="\n"+c}p||c.trim()||(p=!0),l+=a+"\n",e=e.substring(a.length+1),s=c.slice(h)}}r.loose||(a?r.loose=!0:/\n *\n *$/.test(l)&&(a=!0));let u,k=null;this.options.gfm&&(k=/^\[[ xX]\] /.exec(o),k&&(u="[ ] "!==k[0],o=o.replace(/^\[[ xX]\] +/,""))),r.items.push({type:"list_item",raw:l,task:!!k,checked:u,loose:!1,text:o,tokens:[]}),r.raw+=l}r.items[r.items.length-1].raw=l.trimEnd(),r.items[r.items.length-1].text=o.trimEnd(),r.raw=r.raw.trimEnd();for(let e=0;e<r.items.length;e++)if(this.lexer.state.top=!1,r.items[e].tokens=this.lexer.blockTokens(r.items[e].text,[]),!r.loose){const t=r.items[e].tokens.filter((e=>"space"===e.type)),n=t.length>0&&t.some((e=>/\n.*\n/.test(e.raw)));r.loose=n}if(r.loose)for(let e=0;e<r.items.length;e++)r.items[e].loose=!0;return r}}html(e){const t=this.rules.block.html.exec(e);if(t){return{type:"html",block:!0,raw:t[0],pre:"pre"===t[1]||"script"===t[1]||"style"===t[1],text:t[0]}}}def(e){const t=this.rules.block.def.exec(e);if(t){const e=t[1].toLowerCase().replace(/\s+/g," "),n=t[2]?t[2].replace(/^<(.*)>$/,"$1").replace(this.rules.inline.anyPunctuation,"$1"):"",s=t[3]?t[3].substring(1,t[3].length-1).replace(this.rules.inline.anyPunctuation,"$1"):t[3];return{type:"def",tag:e,raw:t[0],href:n,title:s}}}table(e){const t=this.rules.block.table.exec(e);if(!t)return;if(!/[:|]/.test(t[2]))return;const n=d(t[1]),s=t[2].replace(/^\||\| *$/g,"").split("|"),r=t[3]&&t[3].trim()?t[3].replace(/\n[ \t]*$/,"").split("\n"):[],i={type:"table",raw:t[0],header:[],align:[],rows:[]};if(n.length===s.length){for(const e of s)/^ *-+: *$/.test(e)?i.align.push("right"):/^ *:-+: *$/.test(e)?i.align.push("center"):/^ *:-+ *$/.test(e)?i.align.push("left"):i.align.push(null);for(const e of n)i.header.push({text:e,tokens:this.lexer.inline(e)});for(const e of r)i.rows.push(d(e,i.header.length).map((e=>({text:e,tokens:this.lexer.inline(e)}))));return i}}lheading(e){const t=this.rules.block.lheading.exec(e);if(t)return{type:"heading",raw:t[0],depth:"="===t[2].charAt(0)?1:2,text:t[1],tokens:this.lexer.inline(t[1])}}paragraph(e){const t=this.rules.block.paragraph.exec(e);if(t){const e="\n"===t[1].charAt(t[1].length-1)?t[1].slice(0,-1):t[1];return{type:"paragraph",raw:t[0],text:e,tokens:this.lexer.inline(e)}}}text(e){const t=this.rules.block.text.exec(e);if(t)return{type:"text",raw:t[0],text:t[0],tokens:this.lexer.inline(t[0])}}escape(e){const t=this.rules.inline.escape.exec(e);if(t)return{type:"escape",raw:t[0],text:c(t[1])}}tag(e){const t=this.rules.inline.tag.exec(e);if(t)return!this.lexer.state.inLink&&/^<a /i.test(t[0])?this.lexer.state.inLink=!0:this.lexer.state.inLink&&/^<\/a>/i.test(t[0])&&(this.lexer.state.inLink=!1),!this.lexer.state.inRawBlock&&/^<(pre|code|kbd|script)(\s|>)/i.test(t[0])?this.lexer.state.inRawBlock=!0:this.lexer.state.inRawBlock&&/^<\/(pre|code|kbd|script)(\s|>)/i.test(t[0])&&(this.lexer.state.inRawBlock=!1),{type:"html",raw:t[0],inLink:this.lexer.state.inLink,inRawBlock:this.lexer.state.inRawBlock,block:!1,text:t[0]}}link(e){const t=this.rules.inline.link.exec(e);if(t){const e=t[2].trim();if(!this.options.pedantic&&/^</.test(e)){if(!/>$/.test(e))return;const t=x(e.slice(0,-1),"\\");if((e.length-t.length)%2==0)return}else{const e=function(e,t){if(-1===e.indexOf(t[1]))return-1;let n=0;for(let s=0;s<e.length;s++)if("\\"===e[s])s++;else if(e[s]===t[0])n++;else if(e[s]===t[1]&&(n--,n<0))return s;return-1}(t[2],"()");if(e>-1){const n=(0===t[0].indexOf("!")?5:4)+t[1].length+e;t[2]=t[2].substring(0,e),t[0]=t[0].substring(0,n).trim(),t[3]=""}}let n=t[2],s="";if(this.options.pedantic){const e=/^([^'"]*[^\s])\s+(['"])(.*)\2/.exec(n);e&&(n=e[1],s=e[3])}else s=t[3]?t[3].slice(1,-1):"";return n=n.trim(),/^</.test(n)&&(n=this.options.pedantic&&!/>$/.test(e)?n.slice(1):n.slice(1,-1)),b(t,{href:n?n.replace(this.rules.inline.anyPunctuation,"$1"):n,title:s?s.replace(this.rules.inline.anyPunctuation,"$1"):s},t[0],this.lexer)}}reflink(e,t){let n;if((n=this.rules.inline.reflink.exec(e))||(n=this.rules.inline.nolink.exec(e))){const e=t[(n[2]||n[1]).replace(/\s+/g," ").toLowerCase()];if(!e){const e=n[0].charAt(0);return{type:"text",raw:e,text:e}}return b(n,e,n[0],this.lexer)}}emStrong(e,t,n=""){let s=this.rules.inline.emStrongLDelim.exec(e);if(!s)return;if(s[3]&&n.match(/[\p{L}\p{N}]/u))return;if(!(s[1]||s[2]||"")||!n||this.rules.inline.punctuation.exec(n)){const n=[...s[0]].length-1;let r,i,l=n,o=0;const a="*"===s[0][0]?this.rules.inline.emStrongRDelimAst:this.rules.inline.emStrongRDelimUnd;for(a.lastIndex=0,t=t.slice(-1*e.length+n);null!=(s=a.exec(t));){if(r=s[1]||s[2]||s[3]||s[4]||s[5]||s[6],!r)continue;if(i=[...r].length,s[3]||s[4]){l+=i;continue}if((s[5]||s[6])&&n%3&&!((n+i)%3)){o+=i;continue}if(l-=i,l>0)continue;i=Math.min(i,i+l+o);const t=[...s[0]][0].length,a=e.slice(0,n+s.index+t+i);if(Math.min(n,i)%2){const e=a.slice(1,-1);return{type:"em",raw:a,text:e,tokens:this.lexer.inlineTokens(e)}}const c=a.slice(2,-2);return{type:"strong",raw:a,text:c,tokens:this.lexer.inlineTokens(c)}}}}codespan(e){const t=this.rules.inline.code.exec(e);if(t){let e=t[2].replace(/\n/g," ");const n=/[^ ]/.test(e),s=/^ /.test(e)&&/ $/.test(e);return n&&s&&(e=e.substring(1,e.length-1)),e=c(e,!0),{type:"codespan",raw:t[0],text:e}}}br(e){const t=this.rules.inline.br.exec(e);if(t)return{type:"br",raw:t[0]}}del(e){const t=this.rules.inline.del.exec(e);if(t)return{type:"del",raw:t[0],text:t[2],tokens:this.lexer.inlineTokens(t[2])}}autolink(e){const t=this.rules.inline.autolink.exec(e);if(t){let e,n;return"@"===t[2]?(e=c(t[1]),n="mailto:"+e):(e=c(t[1]),n=e),{type:"link",raw:t[0],text:e,href:n,tokens:[{type:"text",raw:e,text:e}]}}}url(e){let t;if(t=this.rules.inline.url.exec(e)){let e,n;if("@"===t[2])e=c(t[0]),n="mailto:"+e;else{let s;do{s=t[0],t[0]=this.rules.inline._backpedal.exec(t[0])?.[0]??""}while(s!==t[0]);e=c(t[0]),n="www."===t[1]?"http://"+t[0]:t[0]}return{type:"link",raw:t[0],text:e,href:n,tokens:[{type:"text",raw:e,text:e}]}}}inlineText(e){const t=this.rules.inline.text.exec(e);if(t){let e;return e=this.lexer.state.inRawBlock?t[0]:c(t[0]),{type:"text",raw:t[0],text:e}}}}const m=/^ {0,3}((?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)/,y=/(?:[*+-]|\d{1,9}[.)])/,$=k(/^(?!bull |blockCode|fences|blockquote|heading|html)((?:.|\n(?!\s*?\n|bull |blockCode|fences|blockquote|heading|html))+?)\n {0,3}(=+|-+) *(?:\n+|$)/).replace(/bull/g,y).replace(/blockCode/g,/ {4}/).replace(/fences/g,/ {0,3}(?:`{3,}|~{3,})/).replace(/blockquote/g,/ {0,3}>/).replace(/heading/g,/ {0,3}#{1,6}/).replace(/html/g,/ {0,3}<[^\n>]+>\n/).getRegex(),z=/^([^\n]+(?:\n(?!hr|heading|lheading|blockquote|fences|list|html|table| +\n)[^\n]+)*)/,T=/(?!\s*\])(?:\\.|[^\[\]\\])+/,R=k(/^ {0,3}\[(label)\]: *(?:\n *)?([^<\s][^\s]*|<.*?>)(?:(?: +(?:\n *)?| *\n *)(title))? *(?:\n+|$)/).replace("label",T).replace("title",/(?:"(?:\\"?|[^"\\])*"|'[^'\n]*(?:\n[^'\n]+)*\n?'|\([^()]*\))/).getRegex(),_=k(/^( {0,3}bull)([ \t][^\n]+?)?(?:\n|$)/).replace(/bull/g,y).getRegex(),A="address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul",S=/<!--(?:-?>|[\s\S]*?(?:-->|$))/,I=k("^ {0,3}(?:<(script|pre|style|textarea)[\\s>][\\s\\S]*?(?:</\\1>[^\\n]*\\n+|$)|comment[^\\n]*(\\n+|$)|<\\?[\\s\\S]*?(?:\\?>\\n*|$)|<![A-Z][\\s\\S]*?(?:>\\n*|$)|<!\\[CDATA\\[[\\s\\S]*?(?:\\]\\]>\\n*|$)|</?(tag)(?: +|\\n|/?>)[\\s\\S]*?(?:(?:\\n *)+\\n|$)|<(?!script|pre|style|textarea)([a-z][\\w-]*)(?:attribute)*? */?>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n *)+\\n|$)|</(?!script|pre|style|textarea)[a-z][\\w-]*\\s*>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n *)+\\n|$))","i").replace("comment",S).replace("tag",A).replace("attribute",/ +[a-zA-Z:_][\w.:-]*(?: *= *"[^"\n]*"| *= *'[^'\n]*'| *= *[^\s"'=<>`]+)?/).getRegex(),E=k(z).replace("hr",m).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("|lheading","").replace("|table","").replace("blockquote"," {0,3}>").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",A).getRegex(),q={blockquote:k(/^( {0,3}> ?(paragraph|[^\n]*)(?:\n|$))+/).replace("paragraph",E).getRegex(),code:/^( {4}[^\n]+(?:\n(?: *(?:\n|$))*)?)+/,def:R,fences:/^ {0,3}(`{3,}(?=[^`\n]*(?:\n|$))|~{3,})([^\n]*)(?:\n|$)(?:|([\s\S]*?)(?:\n|$))(?: {0,3}\1[~`]* *(?=\n|$)|$)/,heading:/^ {0,3}(#{1,6})(?=\s|$)(.*)(?:\n+|$)/,hr:m,html:I,lheading:$,list:_,newline:/^(?: *(?:\n|$))+/,paragraph:E,table:f,text:/^[^\n]+/},Z=k("^ *([^\\n ].*)\\n {0,3}((?:\\| *)?:?-+:? *(?:\\| *:?-+:? *)*(?:\\| *)?)(?:\\n((?:(?! *\\n|hr|heading|blockquote|code|fences|list|html).*(?:\\n|$))*)\\n*|$)").replace("hr",m).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("blockquote"," {0,3}>").replace("code"," {4}[^\\n]").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",A).getRegex(),L={...q,table:Z,paragraph:k(z).replace("hr",m).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("|lheading","").replace("table",Z).replace("blockquote"," {0,3}>").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",A).getRegex()},P={...q,html:k("^ *(?:comment *(?:\\n|\\s*$)|<(tag)[\\s\\S]+?</\\1> *(?:\\n{2,}|\\s*$)|<tag(?:\"[^\"]*\"|'[^']*'|\\s[^'\"/>\\s]*)*?/?> *(?:\\n{2,}|\\s*$))").replace("comment",S).replace(/tag/g,"(?!(?:a|em|strong|small|s|cite|q|dfn|abbr|data|time|code|var|samp|kbd|sub|sup|i|b|u|mark|ruby|rt|rp|bdi|bdo|span|br|wbr|ins|del|img)\\b)\\w+(?!:|[^\\w\\s@]*@)\\b").getRegex(),def:/^ *\[([^\]]+)\]: *<?([^\s>]+)>?(?: +(["(][^\n]+[")]))? *(?:\n+|$)/,heading:/^(#{1,6})(.*)(?:\n+|$)/,fences:f,lheading:/^(.+?)\n {0,3}(=+|-+) *(?:\n+|$)/,paragraph:k(z).replace("hr",m).replace("heading"," *#{1,6} *[^\n]").replace("lheading",$).replace("|table","").replace("blockquote"," {0,3}>").replace("|fences","").replace("|list","").replace("|html","").replace("|tag","").getRegex()},Q=/^\\([!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])/,v=/^( {2,}|\\)\n(?!\s*$)/,B="\\p{P}\\p{S}",C=k(/^((?![*_])[\spunctuation])/,"u").replace(/punctuation/g,B).getRegex(),M=k(/^(?:\*+(?:((?!\*)[punct])|[^\s*]))|^_+(?:((?!_)[punct])|([^\s_]))/,"u").replace(/punct/g,B).getRegex(),O=k("^[^_*]*?__[^_*]*?\\*[^_*]*?(?=__)|[^*]+(?=[^*])|(?!\\*)[punct](\\*+)(?=[\\s]|$)|[^punct\\s](\\*+)(?!\\*)(?=[punct\\s]|$)|(?!\\*)[punct\\s](\\*+)(?=[^punct\\s])|[\\s](\\*+)(?!\\*)(?=[punct])|(?!\\*)[punct](\\*+)(?!\\*)(?=[punct])|[^punct\\s](\\*+)(?=[^punct\\s])","gu").replace(/punct/g,B).getRegex(),D=k("^[^_*]*?\\*\\*[^_*]*?_[^_*]*?(?=\\*\\*)|[^_]+(?=[^_])|(?!_)[punct](_+)(?=[\\s]|$)|[^punct\\s](_+)(?!_)(?=[punct\\s]|$)|(?!_)[punct\\s](_+)(?=[^punct\\s])|[\\s](_+)(?!_)(?=[punct])|(?!_)[punct](_+)(?!_)(?=[punct])","gu").replace(/punct/g,B).getRegex(),j=k(/\\([punct])/,"gu").replace(/punct/g,B).getRegex(),H=k(/^<(scheme:[^\s\x00-\x1f<>]*|email)>/).replace("scheme",/[a-zA-Z][a-zA-Z0-9+.-]{1,31}/).replace("email",/[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+(@)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+(?![-_])/).getRegex(),U=k(S).replace("(?:--\x3e|$)","--\x3e").getRegex(),X=k("^comment|^</[a-zA-Z][\\w:-]*\\s*>|^<[a-zA-Z][\\w-]*(?:attribute)*?\\s*/?>|^<\\?[\\s\\S]*?\\?>|^<![a-zA-Z]+\\s[\\s\\S]*?>|^<!\\[CDATA\\[[\\s\\S]*?\\]\\]>").replace("comment",U).replace("attribute",/\s+[a-zA-Z:_][\w.:-]*(?:\s*=\s*"[^"]*"|\s*=\s*'[^']*'|\s*=\s*[^\s"'=<>`]+)?/).getRegex(),F=/(?:\[(?:\\.|[^\[\]\\])*\]|\\.|`[^`]*`|[^\[\]\\`])*?/,N=k(/^!?\[(label)\]\(\s*(href)(?:\s+(title))?\s*\)/).replace("label",F).replace("href",/<(?:\\.|[^\n<>\\])+>|[^\s\x00-\x1f]*/).replace("title",/"(?:\\"?|[^"\\])*"|'(?:\\'?|[^'\\])*'|\((?:\\\)?|[^)\\])*\)/).getRegex(),G=k(/^!?\[(label)\]\[(ref)\]/).replace("label",F).replace("ref",T).getRegex(),J=k(/^!?\[(ref)\](?:\[\])?/).replace("ref",T).getRegex(),K={_backpedal:f,anyPunctuation:j,autolink:H,blockSkip:/\[[^[\]]*?\]\([^\(\)]*?\)|`[^`]*?`|<[^<>]*?>/g,br:v,code:/^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/,del:f,emStrongLDelim:M,emStrongRDelimAst:O,emStrongRDelimUnd:D,escape:Q,link:N,nolink:J,punctuation:C,reflink:G,reflinkSearch:k("reflink|nolink(?!\\()","g").replace("reflink",G).replace("nolink",J).getRegex(),tag:X,text:/^(`+|[^`])(?:(?= {2,}\n)|[\s\S]*?(?:(?=[\\<!\[`*_]|\b_|$)|[^ ](?= {2,}\n)))/,url:f},V={...K,link:k(/^!?\[(label)\]\((.*?)\)/).replace("label",F).getRegex(),reflink:k(/^!?\[(label)\]\s*\[([^\]]*)\]/).replace("label",F).getRegex()},W={...K,escape:k(Q).replace("])","~|])").getRegex(),url:k(/^((?:ftp|https?):\/\/|www\.)(?:[a-zA-Z0-9\-]+\.?)+[^\s<]*|^email/,"i").replace("email",/[A-Za-z0-9._+-]+(@)[a-zA-Z0-9-_]+(?:\.[a-zA-Z0-9-_]*[a-zA-Z0-9])+(?![-_])/).getRegex(),_backpedal:/(?:[^?!.,:;*_'"~()&]+|\([^)]*\)|&(?![a-zA-Z0-9]+;$)|[?!.,:;*_'"~)]+(?!$))+/,del:/^(~~?)(?=[^\s~])([\s\S]*?[^\s~])\1(?=[^~]|$)/,text:/^([`~]+|[^`~])(?:(?= {2,}\n)|(?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)|[\s\S]*?(?:(?=[\\<!\[`*~_]|\b_|https?:\/\/|ftp:\/\/|www\.|$)|[^ ](?= {2,}\n)|[^a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-](?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)))/},Y={...W,br:k(v).replace("{2,}","*").getRegex(),text:k(W.text).replace("\\b_","\\b_| {2,}\\n").replace(/\{2,\}/g,"*").getRegex()},ee={normal:q,gfm:L,pedantic:P},te={normal:K,gfm:W,breaks:Y,pedantic:V};class ne{tokens;options;state;tokenizer;inlineQueue;constructor(t){this.tokens=[],this.tokens.links=Object.create(null),this.options=t||e.defaults,this.options.tokenizer=this.options.tokenizer||new w,this.tokenizer=this.options.tokenizer,this.tokenizer.options=this.options,this.tokenizer.lexer=this,this.inlineQueue=[],this.state={inLink:!1,inRawBlock:!1,top:!0};const n={block:ee.normal,inline:te.normal};this.options.pedantic?(n.block=ee.pedantic,n.inline=te.pedantic):this.options.gfm&&(n.block=ee.gfm,this.options.breaks?n.inline=te.breaks:n.inline=te.gfm),this.tokenizer.rules=n}static get rules(){return{block:ee,inline:te}}static lex(e,t){return new ne(t).lex(e)}static lexInline(e,t){return new ne(t).inlineTokens(e)}lex(e){e=e.replace(/\r\n|\r/g,"\n"),this.blockTokens(e,this.tokens);for(let e=0;e<this.inlineQueue.length;e++){const t=this.inlineQueue[e];this.inlineTokens(t.src,t.tokens)}return this.inlineQueue=[],this.tokens}blockTokens(e,t=[]){let n,s,r,i;for(e=this.options.pedantic?e.replace(/\t/g,"    ").replace(/^ +$/gm,""):e.replace(/^( *)(\t+)/gm,((e,t,n)=>t+"    ".repeat(n.length)));e;)if(!(this.options.extensions&&this.options.extensions.block&&this.options.extensions.block.some((s=>!!(n=s.call({lexer:this},e,t))&&(e=e.substring(n.raw.length),t.push(n),!0)))))if(n=this.tokenizer.space(e))e=e.substring(n.raw.length),1===n.raw.length&&t.length>0?t[t.length-1].raw+="\n":t.push(n);else if(n=this.tokenizer.code(e))e=e.substring(n.raw.length),s=t[t.length-1],!s||"paragraph"!==s.type&&"text"!==s.type?t.push(n):(s.raw+="\n"+n.raw,s.text+="\n"+n.text,this.inlineQueue[this.inlineQueue.length-1].src=s.text);else if(n=this.tokenizer.fences(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.heading(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.hr(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.blockquote(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.list(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.html(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.def(e))e=e.substring(n.raw.length),s=t[t.length-1],!s||"paragraph"!==s.type&&"text"!==s.type?this.tokens.links[n.tag]||(this.tokens.links[n.tag]={href:n.href,title:n.title}):(s.raw+="\n"+n.raw,s.text+="\n"+n.raw,this.inlineQueue[this.inlineQueue.length-1].src=s.text);else if(n=this.tokenizer.table(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.lheading(e))e=e.substring(n.raw.length),t.push(n);else{if(r=e,this.options.extensions&&this.options.extensions.startBlock){let t=1/0;const n=e.slice(1);let s;this.options.extensions.startBlock.forEach((e=>{s=e.call({lexer:this},n),"number"==typeof s&&s>=0&&(t=Math.min(t,s))})),t<1/0&&t>=0&&(r=e.substring(0,t+1))}if(this.state.top&&(n=this.tokenizer.paragraph(r)))s=t[t.length-1],i&&"paragraph"===s.type?(s.raw+="\n"+n.raw,s.text+="\n"+n.text,this.inlineQueue.pop(),this.inlineQueue[this.inlineQueue.length-1].src=s.text):t.push(n),i=r.length!==e.length,e=e.substring(n.raw.length);else if(n=this.tokenizer.text(e))e=e.substring(n.raw.length),s=t[t.length-1],s&&"text"===s.type?(s.raw+="\n"+n.raw,s.text+="\n"+n.text,this.inlineQueue.pop(),this.inlineQueue[this.inlineQueue.length-1].src=s.text):t.push(n);else if(e){const t="Infinite loop on byte: "+e.charCodeAt(0);if(this.options.silent){console.error(t);break}throw new Error(t)}}return this.state.top=!0,t}inline(e,t=[]){return this.inlineQueue.push({src:e,tokens:t}),t}inlineTokens(e,t=[]){let n,s,r,i,l,o,a=e;if(this.tokens.links){const e=Object.keys(this.tokens.links);if(e.length>0)for(;null!=(i=this.tokenizer.rules.inline.reflinkSearch.exec(a));)e.includes(i[0].slice(i[0].lastIndexOf("[")+1,-1))&&(a=a.slice(0,i.index)+"["+"a".repeat(i[0].length-2)+"]"+a.slice(this.tokenizer.rules.inline.reflinkSearch.lastIndex))}for(;null!=(i=this.tokenizer.rules.inline.blockSkip.exec(a));)a=a.slice(0,i.index)+"["+"a".repeat(i[0].length-2)+"]"+a.slice(this.tokenizer.rules.inline.blockSkip.lastIndex);for(;null!=(i=this.tokenizer.rules.inline.anyPunctuation.exec(a));)a=a.slice(0,i.index)+"++"+a.slice(this.tokenizer.rules.inline.anyPunctuation.lastIndex);for(;e;)if(l||(o=""),l=!1,!(this.options.extensions&&this.options.extensions.inline&&this.options.extensions.inline.some((s=>!!(n=s.call({lexer:this},e,t))&&(e=e.substring(n.raw.length),t.push(n),!0)))))if(n=this.tokenizer.escape(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.tag(e))e=e.substring(n.raw.length),s=t[t.length-1],s&&"text"===n.type&&"text"===s.type?(s.raw+=n.raw,s.text+=n.text):t.push(n);else if(n=this.tokenizer.link(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.reflink(e,this.tokens.links))e=e.substring(n.raw.length),s=t[t.length-1],s&&"text"===n.type&&"text"===s.type?(s.raw+=n.raw,s.text+=n.text):t.push(n);else if(n=this.tokenizer.emStrong(e,a,o))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.codespan(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.br(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.del(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.autolink(e))e=e.substring(n.raw.length),t.push(n);else if(this.state.inLink||!(n=this.tokenizer.url(e))){if(r=e,this.options.extensions&&this.options.extensions.startInline){let t=1/0;const n=e.slice(1);let s;this.options.extensions.startInline.forEach((e=>{s=e.call({lexer:this},n),"number"==typeof s&&s>=0&&(t=Math.min(t,s))})),t<1/0&&t>=0&&(r=e.substring(0,t+1))}if(n=this.tokenizer.inlineText(r))e=e.substring(n.raw.length),"_"!==n.raw.slice(-1)&&(o=n.raw.slice(-1)),l=!0,s=t[t.length-1],s&&"text"===s.type?(s.raw+=n.raw,s.text+=n.text):t.push(n);else if(e){const t="Infinite loop on byte: "+e.charCodeAt(0);if(this.options.silent){console.error(t);break}throw new Error(t)}}else e=e.substring(n.raw.length),t.push(n);return t}}class se{options;constructor(t){this.options=t||e.defaults}code(e,t,n){const s=(t||"").match(/^\S*/)?.[0];return e=e.replace(/\n$/,"")+"\n",s?'<pre><code class="language-'+c(s)+'">'+(n?e:c(e,!0))+"</code></pre>\n":"<pre><code>"+(n?e:c(e,!0))+"</code></pre>\n"}blockquote(e){return`<blockquote>\n${e}</blockquote>\n`}html(e,t){return e}heading(e,t,n){return`<h${t}>${e}</h${t}>\n`}hr(){return"<hr>\n"}list(e,t,n){const s=t?"ol":"ul";return"<"+s+(t&&1!==n?' start="'+n+'"':"")+">\n"+e+"</"+s+">\n"}listitem(e,t,n){return`<li>${e}</li>\n`}checkbox(e){return"<input "+(e?'checked="" ':"")+'disabled="" type="checkbox">'}paragraph(e){return`<p>${e}</p>\n`}table(e,t){return t&&(t=`<tbody>${t}</tbody>`),"<table>\n<thead>\n"+e+"</thead>\n"+t+"</table>\n"}tablerow(e){return`<tr>\n${e}</tr>\n`}tablecell(e,t){const n=t.header?"th":"td";return(t.align?`<${n} align="${t.align}">`:`<${n}>`)+e+`</${n}>\n`}strong(e){return`<strong>${e}</strong>`}em(e){return`<em>${e}</em>`}codespan(e){return`<code>${e}</code>`}br(){return"<br>"}del(e){return`<del>${e}</del>`}link(e,t,n){const s=g(e);if(null===s)return n;let r='<a href="'+(e=s)+'"';return t&&(r+=' title="'+t+'"'),r+=">"+n+"</a>",r}image(e,t,n){const s=g(e);if(null===s)return n;let r=`<img src="${e=s}" alt="${n}"`;return t&&(r+=` title="${t}"`),r+=">",r}text(e){return e}}class re{strong(e){return e}em(e){return e}codespan(e){return e}del(e){return e}html(e){return e}text(e){return e}link(e,t,n){return""+n}image(e,t,n){return""+n}br(){return""}}class ie{options;renderer;textRenderer;constructor(t){this.options=t||e.defaults,this.options.renderer=this.options.renderer||new se,this.renderer=this.options.renderer,this.renderer.options=this.options,this.textRenderer=new re}static parse(e,t){return new ie(t).parse(e)}static parseInline(e,t){return new ie(t).parseInline(e)}parse(e,t=!0){let n="";for(let s=0;s<e.length;s++){const r=e[s];if(this.options.extensions&&this.options.extensions.renderers&&this.options.extensions.renderers[r.type]){const e=r,t=this.options.extensions.renderers[e.type].call({parser:this},e);if(!1!==t||!["space","hr","heading","code","table","blockquote","list","html","paragraph","text"].includes(e.type)){n+=t||"";continue}}switch(r.type){case"space":continue;case"hr":n+=this.renderer.hr();continue;case"heading":{const e=r;n+=this.renderer.heading(this.parseInline(e.tokens),e.depth,p(this.parseInline(e.tokens,this.textRenderer)));continue}case"code":{const e=r;n+=this.renderer.code(e.text,e.lang,!!e.escaped);continue}case"table":{const e=r;let t="",s="";for(let t=0;t<e.header.length;t++)s+=this.renderer.tablecell(this.parseInline(e.header[t].tokens),{header:!0,align:e.align[t]});t+=this.renderer.tablerow(s);let i="";for(let t=0;t<e.rows.length;t++){const n=e.rows[t];s="";for(let t=0;t<n.length;t++)s+=this.renderer.tablecell(this.parseInline(n[t].tokens),{header:!1,align:e.align[t]});i+=this.renderer.tablerow(s)}n+=this.renderer.table(t,i);continue}case"blockquote":{const e=r,t=this.parse(e.tokens);n+=this.renderer.blockquote(t);continue}case"list":{const e=r,t=e.ordered,s=e.start,i=e.loose;let l="";for(let t=0;t<e.items.length;t++){const n=e.items[t],s=n.checked,r=n.task;let o="";if(n.task){const e=this.renderer.checkbox(!!s);i?n.tokens.length>0&&"paragraph"===n.tokens[0].type?(n.tokens[0].text=e+" "+n.tokens[0].text,n.tokens[0].tokens&&n.tokens[0].tokens.length>0&&"text"===n.tokens[0].tokens[0].type&&(n.tokens[0].tokens[0].text=e+" "+n.tokens[0].tokens[0].text)):n.tokens.unshift({type:"text",text:e+" "}):o+=e+" "}o+=this.parse(n.tokens,i),l+=this.renderer.listitem(o,r,!!s)}n+=this.renderer.list(l,t,s);continue}case"html":{const e=r;n+=this.renderer.html(e.text,e.block);continue}case"paragraph":{const e=r;n+=this.renderer.paragraph(this.parseInline(e.tokens));continue}case"text":{let i=r,l=i.tokens?this.parseInline(i.tokens):i.text;for(;s+1<e.length&&"text"===e[s+1].type;)i=e[++s],l+="\n"+(i.tokens?this.parseInline(i.tokens):i.text);n+=t?this.renderer.paragraph(l):l;continue}default:{const e='Token with "'+r.type+'" type was not found.';if(this.options.silent)return console.error(e),"";throw new Error(e)}}}return n}parseInline(e,t){t=t||this.renderer;let n="";for(let s=0;s<e.length;s++){const r=e[s];if(this.options.extensions&&this.options.extensions.renderers&&this.options.extensions.renderers[r.type]){const e=this.options.extensions.renderers[r.type].call({parser:this},r);if(!1!==e||!["escape","html","link","image","strong","em","codespan","br","del","text"].includes(r.type)){n+=e||"";continue}}switch(r.type){case"escape":{const e=r;n+=t.text(e.text);break}case"html":{const e=r;n+=t.html(e.text);break}case"link":{const e=r;n+=t.link(e.href,e.title,this.parseInline(e.tokens,t));break}case"image":{const e=r;n+=t.image(e.href,e.title,e.text);break}case"strong":{const e=r;n+=t.strong(this.parseInline(e.tokens,t));break}case"em":{const e=r;n+=t.em(this.parseInline(e.tokens,t));break}case"codespan":{const e=r;n+=t.codespan(e.text);break}case"br":n+=t.br();break;case"del":{const e=r;n+=t.del(this.parseInline(e.tokens,t));break}case"text":{const e=r;n+=t.text(e.text);break}default:{const e='Token with "'+r.type+'" type was not found.';if(this.options.silent)return console.error(e),"";throw new Error(e)}}}return n}}class le{options;constructor(t){this.options=t||e.defaults}static passThroughHooks=new Set(["preprocess","postprocess","processAllTokens"]);preprocess(e){return e}postprocess(e){return e}processAllTokens(e){return e}}class oe{defaults={async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null};options=this.setOptions;parse=this.#e(ne.lex,ie.parse);parseInline=this.#e(ne.lexInline,ie.parseInline);Parser=ie;Renderer=se;TextRenderer=re;Lexer=ne;Tokenizer=w;Hooks=le;constructor(...e){this.use(...e)}walkTokens(e,t){let n=[];for(const s of e)switch(n=n.concat(t.call(this,s)),s.type){case"table":{const e=s;for(const s of e.header)n=n.concat(this.walkTokens(s.tokens,t));for(const s of e.rows)for(const e of s)n=n.concat(this.walkTokens(e.tokens,t));break}case"list":{const e=s;n=n.concat(this.walkTokens(e.items,t));break}default:{const e=s;this.defaults.extensions?.childTokens?.[e.type]?this.defaults.extensions.childTokens[e.type].forEach((s=>{const r=e[s].flat(1/0);n=n.concat(this.walkTokens(r,t))})):e.tokens&&(n=n.concat(this.walkTokens(e.tokens,t)))}}return n}use(...e){const t=this.defaults.extensions||{renderers:{},childTokens:{}};return e.forEach((e=>{const n={...e};if(n.async=this.defaults.async||n.async||!1,e.extensions&&(e.extensions.forEach((e=>{if(!e.name)throw new Error("extension name required");if("renderer"in e){const n=t.renderers[e.name];t.renderers[e.name]=n?function(...t){let s=e.renderer.apply(this,t);return!1===s&&(s=n.apply(this,t)),s}:e.renderer}if("tokenizer"in e){if(!e.level||"block"!==e.level&&"inline"!==e.level)throw new Error("extension level must be 'block' or 'inline'");const n=t[e.level];n?n.unshift(e.tokenizer):t[e.level]=[e.tokenizer],e.start&&("block"===e.level?t.startBlock?t.startBlock.push(e.start):t.startBlock=[e.start]:"inline"===e.level&&(t.startInline?t.startInline.push(e.start):t.startInline=[e.start]))}"childTokens"in e&&e.childTokens&&(t.childTokens[e.name]=e.childTokens)})),n.extensions=t),e.renderer){const t=this.defaults.renderer||new se(this.defaults);for(const n in e.renderer){if(!(n in t))throw new Error(`renderer '${n}' does not exist`);if("options"===n)continue;const s=n,r=e.renderer[s],i=t[s];t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n||""}}n.renderer=t}if(e.tokenizer){const t=this.defaults.tokenizer||new w(this.defaults);for(const n in e.tokenizer){if(!(n in t))throw new Error(`tokenizer '${n}' does not exist`);if(["options","rules","lexer"].includes(n))continue;const s=n,r=e.tokenizer[s],i=t[s];t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n}}n.tokenizer=t}if(e.hooks){const t=this.defaults.hooks||new le;for(const n in e.hooks){if(!(n in t))throw new Error(`hook '${n}' does not exist`);if("options"===n)continue;const s=n,r=e.hooks[s],i=t[s];le.passThroughHooks.has(n)?t[s]=e=>{if(this.defaults.async)return Promise.resolve(r.call(t,e)).then((e=>i.call(t,e)));const n=r.call(t,e);return i.call(t,n)}:t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n}}n.hooks=t}if(e.walkTokens){const t=this.defaults.walkTokens,s=e.walkTokens;n.walkTokens=function(e){let n=[];return n.push(s.call(this,e)),t&&(n=n.concat(t.call(this,e))),n}}this.defaults={...this.defaults,...n}})),this}setOptions(e){return this.defaults={...this.defaults,...e},this}lexer(e,t){return ne.lex(e,t??this.defaults)}parser(e,t){return ie.parse(e,t??this.defaults)}#e(e,t){return(n,s)=>{const r={...s},i={...this.defaults,...r};!0===this.defaults.async&&!1===r.async&&(i.silent||console.warn("marked(): The async option was set to true by an extension. The async: false option sent to parse will be ignored."),i.async=!0);const l=this.#t(!!i.silent,!!i.async);if(null==n)return l(new Error("marked(): input parameter is undefined or null"));if("string"!=typeof n)return l(new Error("marked(): input parameter is of type "+Object.prototype.toString.call(n)+", string expected"));if(i.hooks&&(i.hooks.options=i),i.async)return Promise.resolve(i.hooks?i.hooks.preprocess(n):n).then((t=>e(t,i))).then((e=>i.hooks?i.hooks.processAllTokens(e):e)).then((e=>i.walkTokens?Promise.all(this.walkTokens(e,i.walkTokens)).then((()=>e)):e)).then((e=>t(e,i))).then((e=>i.hooks?i.hooks.postprocess(e):e)).catch(l);try{i.hooks&&(n=i.hooks.preprocess(n));let s=e(n,i);i.hooks&&(s=i.hooks.processAllTokens(s)),i.walkTokens&&this.walkTokens(s,i.walkTokens);let r=t(s,i);return i.hooks&&(r=i.hooks.postprocess(r)),r}catch(e){return l(e)}}}#t(e,t){return n=>{if(n.message+="\nPlease report this to https://github.com/markedjs/marked.",e){const e="<p>An error occurred:</p><pre>"+c(n.message+"",!0)+"</pre>";return t?Promise.resolve(e):e}if(t)return Promise.reject(n);throw n}}}const ae=new oe;function ce(e,t){return ae.parse(e,t)}ce.options=ce.setOptions=function(e){return ae.setOptions(e),ce.defaults=ae.defaults,n(ce.defaults),ce},ce.getDefaults=t,ce.defaults=e.defaults,ce.use=function(...e){return ae.use(...e),ce.defaults=ae.defaults,n(ce.defaults),ce},ce.walkTokens=function(e,t){return ae.walkTokens(e,t)},ce.parseInline=ae.parseInline,ce.Parser=ie,ce.parser=ie.parse,ce.Renderer=se,ce.TextRenderer=re,ce.Lexer=ne,ce.lexer=ne.lex,ce.Tokenizer=w,ce.Hooks=le,ce.parse=ce;const he=ce.options,pe=ce.setOptions,ue=ce.use,ke=ce.walkTokens,ge=ce.parseInline,fe=ce,de=ie.parse,xe=ne.lex;e.Hooks=le,e.Lexer=ne,e.Marked=oe,e.Parser=ie,e.Renderer=se,e.TextRenderer=re,e.Tokenizer=w,e.getDefaults=t,e.lexer=xe,e.marked=ce,e.options=he,e.parse=fe,e.parseInline=ge,e.parser=de,e.setOptions=pe,e.use=ue,e.walkTokens=ke}));</script>
<script>/*! @license DOMPurify 3.1.6 | (c) Cure53 and other contributors | Released under the Apache license 2.0 and Mozilla Public License 2.0 | github.com/cure53/DOMPurify/blob/3.1.6/LICENSE */
!function(e,t){"object"==typeof exports&&"undefined"!=typeof module?module.exports=t():"function"==typeof define&&define.amd?define(t):(e="undefined"!=typeof globalThis?globalThis:e||self).DOMPurify=t()}(this,(function(){"use strict";const{entries:e,setPrototypeOf:t,isFrozen:n,getPrototypeOf:o,getOwnPropertyDescriptor:r}=Object;let{freeze:i,seal:a,create:l}=Object,{apply:c,construct:s}="undefined"!=typeof Reflect&&Reflect;i||(i=function(e){return e}),a||(a=function(e){return e}),c||(c=function(e,t,n){return e.apply(t,n)}),s||(s=function(e,t){return new e(...t)});const u=b(Array.prototype.forEach),m=b(Array.prototype.pop),p=b(Array.prototype.push),f=b(String.prototype.toLowerCase),d=b(String.prototype.toString),h=b(String.prototype.match),g=b(String.prototype.replace),T=b(String.prototype.indexOf),y=b(String.prototype.trim),E=b(Object.prototype.hasOwnProperty),_=b(RegExp.prototype.test),A=(N=TypeError,function(){for(var e=arguments.length,t=new Array(e),n=0;n<e;n++)t[n]=arguments[n];return s(N,t)});var N;function b(e){return function(t){for(var n=arguments.length,o=new Array(n>1?n-1:0),r=1;r<n;r++)o[r-1]=arguments[r];return c(e,t,o)}}function S(e,o){let r=arguments.length>2&&void 0!==arguments[2]?arguments[2]:f;t&&t(e,null);let i=o.length;for(;i--;){let t=o[i];if("string"==typeof t){const e=r(t);e!==t&&(n(o)||(o[i]=e),t=e)}e[t]=!0}return e}function R(e){for(let t=0;t<e.length;t++){E(e,t)||(e[t]=null)}return e}function w(t){const n=l(null);for(const[o,r]of e(t)){E(t,o)&&(Array.isArray(r)?n[o]=R(r):r&&"object"==typeof r&&r.constructor===Object?n[o]=w(r):n[o]=r)}return n}function C(e,t){for(;null!==e;){const n=r(e,t);if(n){if(n.get)return b(n.get);if("function"==typeof n.value)return b(n.value)}e=o(e)}return function(){return null}}const L=i(["a","abbr","acronym","address","area","article","aside","audio","b","bdi","bdo","big","blink","blockquote","body","br","button","canvas","caption","center","cite","code","col","colgroup","content","data","datalist","dd","decorator","del","details","dfn","dialog","dir","div","dl","dt","element","em","fieldset","figcaption","figure","font","footer","form","h1","h2","h3","h4","h5","h6","head","header","hgroup","hr","html","i","img","input","ins","kbd","label","legend","li","main","map","mark","marquee","menu","menuitem","meter","nav","nobr","ol","optgroup","option","output","p","picture","pre","progress","q","rp","rt","ruby","s","samp","section","select","shadow","small","source","spacer","span","strike","strong","style","sub","summary","sup","table","tbody","td","template","textarea","tfoot","th","thead","time","tr","track","tt","u","ul","var","video","wbr"]),D=i(["svg","a","altglyph","altglyphdef","altglyphitem","animatecolor","animatemotion","animatetransform","circle","clippath","defs","desc","ellipse","filter","font","g","glyph","glyphref","hkern","image","line","lineargradient","marker","mask","metadata","mpath","path","pattern","polygon","polyline","radialgradient","rect","stop","style","switch","symbol","text","textpath","title","tref","tspan","view","vkern"]),v=i(["feBlend","feColorMatrix","feComponentTransfer","feComposite","feConvolveMatrix","feDiffuseLighting","feDisplacementMap","feDistantLight","feDropShadow","feFlood","feFuncA","feFuncB","feFuncG","feFuncR","feGaussianBlur","feImage","feMerge","feMergeNode","feMorphology","feOffset","fePointLight","feSpecularLighting","feSpotLight","feTile","feTurbulence"]),O=i(["animate","color-profile","cursor","discard","font-face","font-face-format","font-face-name","font-face-src","font-face-uri","foreignobject","hatch","hatchpath","mesh","meshgradient","meshpatch","meshrow","missing-glyph","script","set","solidcolor","unknown","use"]),x=i(["math","menclose","merror","mfenced","mfrac","mglyph","mi","mlabeledtr","mmultiscripts","mn","mo","mover","mpadded","mphantom","mroot","mrow","ms","mspace","msqrt","mstyle","msub","msup","msubsup","mtable","mtd","mtext","mtr","munder","munderover","mprescripts"]),k=i(["maction","maligngroup","malignmark","mlongdiv","mscarries","mscarry","msgroup","mstack","msline","msrow","semantics","annotation","annotation-xml","mprescripts","none"]),M=i(["#text"]),I=i(["accept","action","align","alt","autocapitalize","autocomplete","autopictureinpicture","autoplay","background","bgcolor","border","capture","cellpadding","cellspacing","checked","cite","class","clear","color","cols","colspan","controls","controlslist","coords","crossorigin","datetime","decoding","default","dir","disabled","disablepictureinpicture","disableremoteplayback","download","draggable","enctype","enterkeyhint","face","for","headers","height","hidden","high","href","hreflang","id","inputmode","integrity","ismap","kind","label","lang","list","loading","loop","low","max","maxlength","media","method","min","minlength","multiple","muted","name","nonce","noshade","novalidate","nowrap","open","optimum","pattern","placeholder","playsinline","popover","popovertarget","popovertargetaction","poster","preload","pubdate","radiogroup","readonly","rel","required","rev","reversed","role","rows","rowspan","spellcheck","scope","selected","shape","size","sizes","span","srclang","start","src","srcset","step","style","summary","tabindex","title","translate","type","usemap","valign","value","width","wrap","xmlns","slot"]),U=i(["accent-height","accumulate","additive","alignment-baseline","ascent","attributename","attributetype","azimuth","basefrequency","baseline-shift","begin","bias","by","class","clip","clippathunits","clip-path","clip-rule","color","color-interpolation","color-interpolation-filters","color-profile","color-rendering","cx","cy","d","dx","dy","diffuseconstant","direction","display","divisor","dur","edgemode","elevation","end","fill","fill-opacity","fill-rule","filter","filterunits","flood-color","flood-opacity","font-family","font-size","font-size-adjust","font-stretch","font-style","font-variant","font-weight","fx","fy","g1","g2","glyph-name","glyphref","gradientunits","gradienttransform","height","href","id","image-rendering","in","in2","k","k1","k2","k3","k4","kerning","keypoints","keysplines","keytimes","lang","lengthadjust","letter-spacing","kernelmatrix","kernelunitlength","lighting-color","local","marker-end","marker-mid","marker-start","markerheight","markerunits","markerwidth","maskcontentunits","maskunits","max","mask","media","method","mode","min","name","numoctaves","offset","operator","opacity","order","orient","orientation","origin","overflow","paint-order","path","pathlength","patterncontentunits","patterntransform","patternunits","points","preservealpha","preserveaspectratio","primitiveunits","r","rx","ry","radius","refx","refy","repeatcount","repeatdur","restart","result","rotate","scale","seed","shape-rendering","specularconstant","specularexponent","spreadmethod","startoffset","stddeviation","stitchtiles","stop-color","stop-opacity","stroke-dasharray","stroke-dashoffset","stroke-linecap","stroke-linejoin","stroke-miterlimit","stroke-opacity","stroke","stroke-width","style","surfacescale","systemlanguage","tabindex","targetx","targety","transform","transform-origin","text-anchor","text-decoration","text-rendering","textlength","type","u1","u2","unicode","values","viewbox","visibility","version","vert-adv-y","vert-origin-x","vert-origin-y","width","word-spacing","wrap","writing-mode","xchannelselector","ychannelselector","x","x1","x2","xmlns","y","y1","y2","z","zoomandpan"]),P=i(["accent","accentunder","align","bevelled","close","columnsalign","columnlines","columnspan","denomalign","depth","dir","display","displaystyle","encoding","fence","frame","height","href","id","largeop","length","linethickness","lspace","lquote","mathbackground","mathcolor","mathsize","mathvariant","maxsize","minsize","movablelimits","notation","numalign","open","rowalign","rowlines","rowspacing","rowspan","rspace","rquote","scriptlevel","scriptminsize","scriptsizemultiplier","selection","separator","separators","stretchy","subscriptshift","supscriptshift","symmetric","voffset","width","xmlns"]),F=i(["xlink:href","xml:id","xlink:title","xml:space","xmlns:xlink"]),H=a(/\{\{[\w\W]*|[\w\W]*\}\}/gm),z=a(/<%[\w\W]*|[\w\W]*%>/gm),B=a(/\${[\w\W]*}/gm),W=a(/^data-[\-\w.\u00B7-\uFFFF]/),G=a(/^aria-[\-\w]+$/),Y=a(/^(?:(?:(?:f|ht)tps?|mailto|tel|callto|sms|cid|xmpp):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i),j=a(/^(?:\w+script|data):/i),X=a(/[\u0000-\u0020\u00A0\u1680\u180E\u2000-\u2029\u205F\u3000]/g),q=a(/^html$/i),$=a(/^[a-z][.\w]*(-[.\w]+)+$/i);var K=Object.freeze({__proto__:null,MUSTACHE_EXPR:H,ERB_EXPR:z,TMPLIT_EXPR:B,DATA_ATTR:W,ARIA_ATTR:G,IS_ALLOWED_URI:Y,IS_SCRIPT_OR_DATA:j,ATTR_WHITESPACE:X,DOCTYPE_NAME:q,CUSTOM_ELEMENT:$});const V=1,Z=3,J=7,Q=8,ee=9,te=function(){return"undefined"==typeof window?null:window};var ne=function t(){let n=arguments.length>0&&void 0!==arguments[0]?arguments[0]:te();const o=e=>t(e);if(o.version="3.1.6",o.removed=[],!n||!n.document||n.document.nodeType!==ee)return o.isSupported=!1,o;let{document:r}=n;const a=r,c=a.currentScript,{DocumentFragment:s,HTMLTemplateElement:N,Node:b,Element:R,NodeFilter:H,NamedNodeMap:z=n.NamedNodeMap||n.MozNamedAttrMap,HTMLFormElement:B,DOMParser:W,trustedTypes:G}=n,j=R.prototype,X=C(j,"cloneNode"),$=C(j,"remove"),ne=C(j,"nextSibling"),oe=C(j,"childNodes"),re=C(j,"parentNode");if("function"==typeof N){const e=r.createElement("template");e.content&&e.content.ownerDocument&&(r=e.content.ownerDocument)}let ie,ae="";const{implementation:le,createNodeIterator:ce,createDocumentFragment:se,getElementsByTagName:ue}=r,{importNode:me}=a;let pe={};o.isSupported="function"==typeof e&&"function"==typeof re&&le&&void 0!==le.createHTMLDocument;const{MUSTACHE_EXPR:fe,ERB_EXPR:de,TMPLIT_EXPR:he,DATA_ATTR:ge,ARIA_ATTR:Te,IS_SCRIPT_OR_DATA:ye,ATTR_WHITESPACE:Ee,CUSTOM_ELEMENT:_e}=K;let{IS_ALLOWED_URI:Ae}=K,Ne=null;const be=S({},[...L,...D,...v,...x,...M]);let Se=null;const Re=S({},[...I,...U,...P,...F]);let we=Object.seal(l(null,{tagNameCheck:{writable:!0,configurable:!1,enumerable:!0,value:null},attributeNameCheck:{writable:!0,configurable:!1,enumerable:!0,value:null},allowCustomizedBuiltInElements:{writable:!0,configurable:!1,enumerable:!0,value:!1}})),Ce=null,Le=null,De=!0,ve=!0,Oe=!1,xe=!0,ke=!1,Me=!0,Ie=!1,Ue=!1,Pe=!1,Fe=!1,He=!1,ze=!1,Be=!0,We=!1,Ge=!0,Ye=!1,je={},Xe=null;const qe=S({},["annotation-xml","audio","colgroup","desc","foreignobject","head","iframe","math","mi","mn","mo","ms","mtext","noembed","noframes","noscript","plaintext","script","style","svg","template","thead","title","video","xmp"]);let $e=null;const Ke=S({},["audio","video","img","source","image","track"]);let Ve=null;const Ze=S({},["alt","class","for","id","label","name","pattern","placeholder","role","summary","title","value","style","xmlns"]),Je="http://www.w3.org/1998/Math/MathML",Qe="http://www.w3.org/2000/svg",et="http://www.w3.org/1999/xhtml";let tt=et,nt=!1,ot=null;const rt=S({},[Je,Qe,et],d);let it=null;const at=["application/xhtml+xml","text/html"];let lt=null,ct=null;const st=r.createElement("form"),ut=function(e){return e instanceof RegExp||e instanceof Function},mt=function(){let e=arguments.length>0&&void 0!==arguments[0]?arguments[0]:{};if(!ct||ct!==e){if(e&&"object"==typeof e||(e={}),e=w(e),it=-1===at.indexOf(e.PARSER_MEDIA_TYPE)?"text/html":e.PARSER_MEDIA_TYPE,lt="application/xhtml+xml"===it?d:f,Ne=E(e,"ALLOWED_TAGS")?S({},e.ALLOWED_TAGS,lt):be,Se=E(e,"ALLOWED_ATTR")?S({},e.ALLOWED_ATTR,lt):Re,ot=E(e,"ALLOWED_NAMESPACES")?S({},e.ALLOWED_NAMESPACES,d):rt,Ve=E(e,"ADD_URI_SAFE_ATTR")?S(w(Ze),e.ADD_URI_SAFE_ATTR,lt):Ze,$e=E(e,"ADD_DATA_URI_TAGS")?S(w(Ke),e.ADD_DATA_URI_TAGS,lt):Ke,Xe=E(e,"FORBID_CONTENTS")?S({},e.FORBID_CONTENTS,lt):qe,Ce=E(e,"FORBID_TAGS")?S({},e.FORBID_TAGS,lt):{},Le=E(e,"FORBID_ATTR")?S({},e.FORBID_ATTR,lt):{},je=!!E(e,"USE_PROFILES")&&e.USE_PROFILES,De=!1!==e.ALLOW_ARIA_ATTR,ve=!1!==e.ALLOW_DATA_ATTR,Oe=e.ALLOW_UNKNOWN_PROTOCOLS||!1,xe=!1!==e.ALLOW_SELF_CLOSE_IN_ATTR,ke=e.SAFE_FOR_TEMPLATES||!1,Me=!1!==e.SAFE_FOR_XML,Ie=e.WHOLE_DOCUMENT||!1,Fe=e.RETURN_DOM||!1,He=e.RETURN_DOM_FRAGMENT||!1,ze=e.RETURN_TRUSTED_TYPE||!1,Pe=e.FORCE_BODY||!1,Be=!1!==e.SANITIZE_DOM,We=e.SANITIZE_NAMED_PROPS||!1,Ge=!1!==e.KEEP_CONTENT,Ye=e.IN_PLACE||!1,Ae=e.ALLOWED_URI_REGEXP||Y,tt=e.NAMESPACE||et,we=e.CUSTOM_ELEMENT_HANDLING||{},e.CUSTOM_ELEMENT_HANDLING&&ut(e.CUSTOM_ELEMENT_HANDLING.tagNameCheck)&&(we.tagNameCheck=e.CUSTOM_ELEMENT_HANDLING.tagNameCheck),e.CUSTOM_ELEMENT_HANDLING&&ut(e.CUSTOM_ELEMENT_HANDLING.attributeNameCheck)&&(we.attributeNameCheck=e.CUSTOM_ELEMENT_HANDLING.attributeNameCheck),e.CUSTOM_ELEMENT_HANDLING&&"boolean"==typeof e.CUSTOM_ELEMENT_HANDLING.allowCustomizedBuiltInElements&&(we.allowCustomizedBuiltInElements=e.CUSTOM_ELEMENT_HANDLING.allowCustomizedBuiltInElements),ke&&(ve=!1),He&&(Fe=!0),je&&(Ne=S({},M),Se=[],!0===je.html&&(S(Ne,L),S(Se,I)),!0===je.svg&&(S(Ne,D),S(Se,U),S(Se,F)),!0===je.svgFilters&&(S(Ne,v),S(Se,U),S(Se,F)),!0===je.mathMl&&(S(Ne,x),S(Se,P),S(Se,F))),e.ADD_TAGS&&(Ne===be&&(Ne=w(Ne)),S(Ne,e.ADD_TAGS,lt)),e.ADD_ATTR&&(Se===Re&&(Se=w(Se)),S(Se,e.ADD_ATTR,lt)),e.ADD_URI_SAFE_ATTR&&S(Ve,e.ADD_URI_SAFE_ATTR,lt),e.FORBID_CONTENTS&&(Xe===qe&&(Xe=w(Xe)),S(Xe,e.FORBID_CONTENTS,lt)),Ge&&(Ne["#text"]=!0),Ie&&S(Ne,["html","head","body"]),Ne.table&&(S(Ne,["tbody"]),delete Ce.tbody),e.TRUSTED_TYPES_POLICY){if("function"!=typeof e.TRUSTED_TYPES_POLICY.createHTML)throw A('TRUSTED_TYPES_POLICY configuration option must provide a "createHTML" hook.');if("function"!=typeof e.TRUSTED_TYPES_POLICY.createScriptURL)throw A('TRUSTED_TYPES_POLICY configuration option must provide a "createScriptURL" hook.');ie=e.TRUSTED_TYPES_POLICY,ae=ie.createHTML("")}else void 0===ie&&(ie=function(e,t){if("object"!=typeof e||"function"!=typeof e.createPolicy)return null;let n=null;const o="data-tt-policy-suffix";t&&t.hasAttribute(o)&&(n=t.getAttribute(o));const r="dompurify"+(n?"#"+n:"");try{return e.createPolicy(r,{createHTML:e=>e,createScriptURL:e=>e})}catch(e){return console.warn("TrustedTypes policy "+r+" could not be created."),null}}(G,c)),null!==ie&&"string"==typeof ae&&(ae=ie.createHTML(""));i&&i(e),ct=e}},pt=S({},["mi","mo","mn","ms","mtext"]),ft=S({},["foreignobject","annotation-xml"]),dt=S({},["title","style","font","a","script"]),ht=S({},[...D,...v,...O]),gt=S({},[...x,...k]),Tt=function(e){p(o.removed,{element:e});try{re(e).removeChild(e)}catch(t){$(e)}},yt=function(e,t){try{p(o.removed,{attribute:t.getAttributeNode(e),from:t})}catch(e){p(o.removed,{attribute:null,from:t})}if(t.removeAttribute(e),"is"===e&&!Se[e])if(Fe||He)try{Tt(t)}catch(e){}else try{t.setAttribute(e,"")}catch(e){}},Et=function(e){let t=null,n=null;if(Pe)e="<remove></remove>"+e;else{const t=h(e,/^[\r\n\t ]+/);n=t&&t[0]}"application/xhtml+xml"===it&&tt===et&&(e='<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body>'+e+"</body></html>");const o=ie?ie.createHTML(e):e;if(tt===et)try{t=(new W).parseFromString(o,it)}catch(e){}if(!t||!t.documentElement){t=le.createDocument(tt,"template",null);try{t.documentElement.innerHTML=nt?ae:o}catch(e){}}const i=t.body||t.documentElement;return e&&n&&i.insertBefore(r.createTextNode(n),i.childNodes[0]||null),tt===et?ue.call(t,Ie?"html":"body")[0]:Ie?t.documentElement:i},_t=function(e){return ce.call(e.ownerDocument||e,e,H.SHOW_ELEMENT|H.SHOW_COMMENT|H.SHOW_TEXT|H.SHOW_PROCESSING_INSTRUCTION|H.SHOW_CDATA_SECTION,null)},At=function(e){return e instanceof B&&("string"!=typeof e.nodeName||"string"!=typeof e.textContent||"function"!=typeof e.removeChild||!(e.attributes instanceof z)||"function"!=typeof e.removeAttribute||"function"!=typeof e.setAttribute||"string"!=typeof e.namespaceURI||"function"!=typeof e.insertBefore||"function"!=typeof e.hasChildNodes)},Nt=function(e){return"function"==typeof b&&e instanceof b},bt=function(e,t,n){pe[e]&&u(pe[e],(e=>{e.call(o,t,n,ct)}))},St=function(e){let t=null;if(bt("beforeSanitizeElements",e,null),At(e))return Tt(e),!0;const n=lt(e.nodeName);if(bt("uponSanitizeElement",e,{tagName:n,allowedTags:Ne}),e.hasChildNodes()&&!Nt(e.firstElementChild)&&_(/<[/\w]/g,e.innerHTML)&&_(/<[/\w]/g,e.textContent))return Tt(e),!0;if(e.nodeType===J)return Tt(e),!0;if(Me&&e.nodeType===Q&&_(/<[/\w]/g,e.data))return Tt(e),!0;if(!Ne[n]||Ce[n]){if(!Ce[n]&&wt(n)){if(we.tagNameCheck instanceof RegExp&&_(we.tagNameCheck,n))return!1;if(we.tagNameCheck instanceof Function&&we.tagNameCheck(n))return!1}if(Ge&&!Xe[n]){const t=re(e)||e.parentNode,n=oe(e)||e.childNodes;if(n&&t){for(let o=n.length-1;o>=0;--o){const r=X(n[o],!0);r.__removalCount=(e.__removalCount||0)+1,t.insertBefore(r,ne(e))}}}return Tt(e),!0}return e instanceof R&&!function(e){let t=re(e);t&&t.tagName||(t={namespaceURI:tt,tagName:"template"});const n=f(e.tagName),o=f(t.tagName);return!!ot[e.namespaceURI]&&(e.namespaceURI===Qe?t.namespaceURI===et?"svg"===n:t.namespaceURI===Je?"svg"===n&&("annotation-xml"===o||pt[o]):Boolean(ht[n]):e.namespaceURI===Je?t.namespaceURI===et?"math"===n:t.namespaceURI===Qe?"math"===n&&ft[o]:Boolean(gt[n]):e.namespaceURI===et?!(t.namespaceURI===Qe&&!ft[o])&&!(t.namespaceURI===Je&&!pt[o])&&!gt[n]&&(dt[n]||!ht[n]):!("application/xhtml+xml"!==it||!ot[e.namespaceURI]))}(e)?(Tt(e),!0):"noscript"!==n&&"noembed"!==n&&"noframes"!==n||!_(/<\/no(script|embed|frames)/i,e.innerHTML)?(ke&&e.nodeType===Z&&(t=e.textContent,u([fe,de,he],(e=>{t=g(t,e," ")})),e.textContent!==t&&(p(o.removed,{element:e.cloneNode()}),e.textContent=t)),bt("afterSanitizeElements",e,null),!1):(Tt(e),!0)},Rt=function(e,t,n){if(Be&&("id"===t||"name"===t)&&(n in r||n in st))return!1;if(ve&&!Le[t]&&_(ge,t));else if(De&&_(Te,t));else if(!Se[t]||Le[t]){if(!(wt(e)&&(we.tagNameCheck instanceof RegExp&&_(we.tagNameCheck,e)||we.tagNameCheck instanceof Function&&we.tagNameCheck(e))&&(we.attributeNameCheck instanceof RegExp&&_(we.attributeNameCheck,t)||we.attributeNameCheck instanceof Function&&we.attributeNameCheck(t))||"is"===t&&we.allowCustomizedBuiltInElements&&(we.tagNameCheck instanceof RegExp&&_(we.tagNameCheck,n)||we.tagNameCheck instanceof Function&&we.tagNameCheck(n))))return!1}else if(Ve[t]);else if(_(Ae,g(n,Ee,"")));else if("src"!==t&&"xlink:href"!==t&&"href"!==t||"script"===e||0!==T(n,"data:")||!$e[e]){if(Oe&&!_(ye,g(n,Ee,"")));else if(n)return!1}else;return!0},wt=function(e){return"annotation-xml"!==e&&h(e,_e)},Ct=function(e){bt("beforeSanitizeAttributes",e,null);const{attributes:t}=e;if(!t)return;const n={attrName:"",attrValue:"",keepAttr:!0,allowedAttributes:Se};let r=t.length;for(;r--;){const i=t[r],{name:a,namespaceURI:l,value:c}=i,s=lt(a);let p="value"===a?c:y(c);if(n.attrName=s,n.attrValue=p,n.keepAttr=!0,n.forceKeepAttr=void 0,bt("uponSanitizeAttribute",e,n),p=n.attrValue,Me&&_(/((--!?|])>)|<\/(style|title)/i,p)){yt(a,e);continue}if(n.forceKeepAttr)continue;if(yt(a,e),!n.keepAttr)continue;if(!xe&&_(/\/>/i,p)){yt(a,e);continue}ke&&u([fe,de,he],(e=>{p=g(p,e," ")}));const f=lt(e.nodeName);if(Rt(f,s,p)){if(!We||"id"!==s&&"name"!==s||(yt(a,e),p="user-content-"+p),ie&&"object"==typeof G&&"function"==typeof G.getAttributeType)if(l);else switch(G.getAttributeType(f,s)){case"TrustedHTML":p=ie.createHTML(p);break;case"TrustedScriptURL":p=ie.createScriptURL(p)}try{l?e.setAttributeNS(l,a,p):e.setAttribute(a,p),At(e)?Tt(e):m(o.removed)}catch(e){}}}bt("afterSanitizeAttributes",e,null)},Lt=function e(t){let n=null;const o=_t(t);for(bt("beforeSanitizeShadowDOM",t,null);n=o.nextNode();)bt("uponSanitizeShadowNode",n,null),St(n)||(n.content instanceof s&&e(n.content),Ct(n));bt("afterSanitizeShadowDOM",t,null)};return o.sanitize=function(e){let t=arguments.length>1&&void 0!==arguments[1]?arguments[1]:{},n=null,r=null,i=null,l=null;if(nt=!e,nt&&(e="\x3c!--\x3e"),"string"!=typeof e&&!Nt(e)){if("function"!=typeof e.toString)throw A("toString is not a function");if("string"!=typeof(e=e.toString()))throw A("dirty is not a string, aborting")}if(!o.isSupported)return e;if(Ue||mt(t),o.removed=[],"string"==typeof e&&(Ye=!1),Ye){if(e.nodeName){const t=lt(e.nodeName);if(!Ne[t]||Ce[t])throw A("root node is forbidden and cannot be sanitized in-place")}}else if(e instanceof b)n=Et("\x3c!----\x3e"),r=n.ownerDocument.importNode(e,!0),r.nodeType===V&&"BODY"===r.nodeName||"HTML"===r.nodeName?n=r:n.appendChild(r);else{if(!Fe&&!ke&&!Ie&&-1===e.indexOf("<"))return ie&&ze?ie.createHTML(e):e;if(n=Et(e),!n)return Fe?null:ze?ae:""}n&&Pe&&Tt(n.firstChild);const c=_t(Ye?e:n);for(;i=c.nextNode();)St(i)||(i.content instanceof s&&Lt(i.content),Ct(i));if(Ye)return e;if(Fe){if(He)for(l=se.call(n.ownerDocument);n.firstChild;)l.appendChild(n.firstChild);else l=n;return(Se.shadowroot||Se.shadowrootmode)&&(l=me.call(a,l,!0)),l}let m=Ie?n.outerHTML:n.innerHTML;return Ie&&Ne["!doctype"]&&n.ownerDocument&&n.ownerDocument.doctype&&n.ownerDocument.doctype.name&&_(q,n.ownerDocument.doctype.name)&&(m="<!DOCTYPE "+n.ownerDocument.doctype.name+">\n"+m),ke&&u([fe,de,he],(e=>{m=g(m,e," ")})),ie&&ze?ie.createHTML(m):m},o.setConfig=function(){mt(arguments.length>0&&void 0!==arguments[0]?arguments[0]:{}),Ue=!0},o.clearConfig=function(){ct=null,Ue=!1},o.isValidAttribute=function(e,t,n){ct||mt({});const o=lt(e),r=lt(t);return Rt(o,r,n)},o.addHook=function(e,t){"function"==typeof t&&(pe[e]=pe[e]||[],p(pe[e],t))},o.removeHook=function(e){if(pe[e])return m(pe[e])},o.removeHooks=function(e){pe[e]&&(pe[e]=[])},o.removeAllHooks=function(){pe={}},o}();return ne}));
//# sourceMappingURL=purify.min.js.map</script>
<style>
:root{
  --paper:#F3F7FF; --surface:rgba(255,255,255,.80); --solid:#FFFFFF; --ink:#1A2342; --muted:#66748F;
  --line:#DCE5F5; --soft:#EEF3FC; --focus:#3D7BF7; --bad:#C2413A; --ok:#25835A; --gold:#B7791F; --gold-bg:#FBF3E2;
  --grad:linear-gradient(115deg,#3D7BF7 0%,#6B6CF6 52%,#9F7CF0 100%);
  --bg:radial-gradient(1100px 620px at 8% -12%,rgba(79,141,255,.20),transparent 62%),
       radial-gradient(900px 560px at 102% -6%,rgba(164,124,242,.17),transparent 58%),
       radial-gradient(900px 700px at 50% 120%,rgba(110,170,255,.12),transparent 60%),
       linear-gradient(180deg,#F7FAFF 0%,#EDF3FE 100%);
  --shadow:0 1px 2px rgba(40,70,140,.05),0 14px 36px -20px rgba(50,90,190,.32);
  --ui:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --read:"Iowan Old Style","Palatino Linotype",Palatino,"Songti SC","Source Han Serif SC","Noto Serif CJK SC",serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#0D1322; --surface:rgba(22,30,52,.74); --solid:#161E33; --ink:#E4EAF8; --muted:#8D9AB7;
  --line:#26324E; --soft:#1C2642; --focus:#86A9FF; --bad:#EA6E64; --ok:#5DC490; --gold:#E3B55B; --gold-bg:#3A3020;
  --grad:linear-gradient(115deg,#5B92FF 0%,#7E7CFA 52%,#B08CF6 100%);
  --bg:radial-gradient(1100px 620px at 8% -12%,rgba(70,120,255,.26),transparent 62%),
       radial-gradient(900px 560px at 102% -6%,rgba(150,110,240,.22),transparent 58%),
       linear-gradient(180deg,#0E1527 0%,#0B1120 100%);
  --shadow:0 1px 2px rgba(0,0,0,.35),0 16px 40px -20px rgba(0,0,0,.7);
}}
*{box-sizing:border-box}
html.lock{overflow:hidden}
body{margin:0;min-height:100vh;background:var(--bg);background-attachment:fixed;color:var(--ink);font:14px/1.55 var(--ui);-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:6px}
[hidden]{display:none!important}
.wrap{max-width:1480px;margin:0 auto;padding:0 28px}
.sp{flex:1}

/* Top bar */
.top{position:sticky;top:0;z-index:30;background:color-mix(in srgb,var(--paper) 62%,transparent);backdrop-filter:saturate(1.4) blur(10px);-webkit-backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
.top .wrap{display:flex;align-items:center;gap:26px;height:56px}
.brand{display:flex;align-items:center;gap:10px;font-weight:650;font-size:16px;letter-spacing:-.01em}
.logo{display:inline-flex;width:26px;height:26px;filter:drop-shadow(0 3px 8px rgba(80,110,240,.35))}
.logo svg{width:100%;height:100%}
.word{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
nav{display:flex;gap:2px}
nav button{border:0;background:none;padding:6px 12px;border-radius:8px;color:var(--muted);cursor:pointer}
nav button:hover{color:var(--ink)}
nav button.on{background:var(--solid);color:var(--ink);font-weight:600;box-shadow:0 0 0 1px var(--line),0 4px 12px -8px rgba(60,100,220,.45)}
#conn{color:var(--bad);font-size:12.5px}
.langsw{display:inline-flex;padding:2px;border-radius:9px;background:color-mix(in srgb,var(--soft) 80%,transparent);border:1px solid var(--line)}
.langsw button{border:0;background:none;padding:3px 10px;border-radius:7px;font-size:12.5px;color:var(--muted);cursor:pointer}
.langsw button.on{background:var(--solid);color:var(--ink);font-weight:600;box-shadow:0 1px 3px rgba(40,70,140,.15)}

/* Buttons */
.btn{border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:6px 14px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:color-mix(in srgb,var(--ink) 30%,var(--line))}
.btn:disabled{opacity:.45;cursor:default}
.btn.sm{padding:3px 10px;font-size:12.5px;border-radius:7px}
.btn.ghost{background:transparent}
.btn.primary{background:var(--grad);color:#fff;border-color:transparent;font-weight:600;box-shadow:0 8px 20px -10px rgba(77,105,240,.75)}
.btn.primary:hover{border-color:transparent;filter:brightness(1.06)}
.btn.on{background:var(--gold-bg);border-color:var(--gold);color:var(--gold)}
.ib{display:inline-grid;place-items:center;width:28px;height:28px;border:0;background:none;border-radius:7px;color:var(--muted);cursor:pointer}
.ib:hover{background:var(--soft);color:var(--ink)}
.ib[aria-pressed="true"]{background:var(--soft);color:var(--ink)}
.chip{display:inline-block;padding:1px 9px;border-radius:999px;background:var(--soft);font-size:12px;color:var(--muted)}
.chip.win{background:var(--gold-bg);color:var(--gold)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--c);flex:none}
input.field,select.field{border:1px solid var(--line);background:var(--solid);border-radius:8px;padding:6px 10px;min-width:0}

/* Composer */
.composer{margin:26px 0 18px;background:var(--surface);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:16px 16px 14px;transition:box-shadow .2s,border-color .2s}
.composer:focus-within{border-color:color-mix(in srgb,var(--focus) 45%,var(--line));box-shadow:0 0 0 4px color-mix(in srgb,var(--focus) 12%,transparent),0 18px 44px -22px rgba(70,100,230,.55)}
.cin{display:flex;gap:12px;align-items:flex-end}
#prompt{flex:1;border:0;outline:0;resize:none;background:transparent;font-size:16px;line-height:1.65;min-height:54px;max-height:40vh;padding:2px 4px}
#prompt::placeholder{color:var(--muted)}
.cin .btn.primary{padding:8px 22px;border-radius:10px}
.lanes{display:grid;gap:14px;margin-top:12px}
.lane{display:flex;align-items:center;gap:6px;padding:5px 6px 5px 4px;border-radius:11px;background:color-mix(in srgb,var(--soft) 85%,transparent);border:1px solid transparent;min-width:0}
.lane.off{background:transparent;border-color:var(--line);border-style:dashed}
.lane.off .lsw,.lane.off select{opacity:.5}
.lsw{display:flex;align-items:center;gap:8px;border:0;background:none;padding:4px 8px;border-radius:7px;cursor:pointer;font-weight:600}
.lsw .box{width:15px;height:15px;border-radius:4px;border:1.5px solid var(--c);display:grid;place-items:center}
.lane:not(.off) .lsw .box{background:var(--c)}
.lane:not(.off) .lsw .box::after{content:"";width:7px;height:4px;border:2px solid #fff;border-top:0;border-right:0;transform:rotate(-45deg) translate(1px,-1px)}
.lane select{flex:1;min-width:0;border:0;background:transparent;color:var(--muted);padding:4px 6px;border-radius:7px;cursor:pointer;text-overflow:ellipsis}
.lane select.eff{flex:none;border-left:1px solid var(--line);border-radius:0 7px 7px 0;padding-left:10px}
.lane select.eff.set,.lane select.mdl.set{color:var(--ink)}
.lane select:hover{background:var(--solid);color:var(--ink)}
.copts{display:flex;gap:10px;align-items:center;margin-top:10px;color:var(--muted);font-size:12.5px}
.copts .field{font-size:12.5px;padding:4px 9px}
#tag{width:170px}#cwd{flex:1;max-width:420px}
kbd{font:11.5px var(--ui);border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 5px;background:var(--solid)}

/* Feed */
.feedhead{display:flex;align-items:center;gap:10px;margin:6px 2px 10px;color:var(--muted);font-size:13px}
.feedhead h2{font-size:13px;font-weight:500;margin:0}
#empty{padding:56px 20px;text-align:center;color:var(--muted)}
.round{background:var(--surface);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid var(--line);border-radius:16px;margin-bottom:10px;transition:box-shadow .2s}
.round.open{box-shadow:var(--shadow)}
.round.running{position:relative}
.round.running::after{content:"";position:absolute;left:16px;right:16px;top:0;height:2px;border-radius:2px;background:var(--grad);background-size:200% 100%;animation:flow 1.6s linear infinite}
@keyframes flow{to{background-position:-200% 0}}
.rh{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto auto;align-items:center;gap:14px;padding:12px 16px 12px 12px;cursor:pointer;border-radius:14px}
.rh:hover .rq{color:var(--ink)}
.chev{display:grid;place-items:center;width:24px;height:24px;color:var(--muted);transition:transform .18s}
.round.open>.rh .chev{transform:rotate(90deg)}
.rq{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14.5px;color:color-mix(in srgb,var(--ink) 85%,var(--muted))}
.round.open .rq{white-space:pre-wrap;overflow:visible;font-size:15.5px;font-weight:600;color:var(--ink);line-height:1.6}
.lanebar{display:flex;gap:3px}
.lanebar i{width:16px;height:6px;border-radius:3px;background:var(--line)}
.lanebar i.run{background:color-mix(in srgb,var(--c) 50%,var(--line));animation:pulse 1.2s ease-in-out infinite}
.lanebar i.ok{background:var(--c)}
.lanebar i.bad{background:repeating-linear-gradient(135deg,var(--bad) 0 2px,transparent 2px 4px);box-shadow:inset 0 0 0 1px var(--bad)}
@keyframes pulse{50%{opacity:.35}}
.rmeta{display:flex;gap:8px;align-items:center;justify-content:flex-end;min-width:250px;color:var(--muted);font-size:12.5px;white-space:nowrap;font-variant-numeric:tabular-nums}
.ract{display:flex;gap:6px;justify-content:flex-end;min-width:100px}
.round:not(.open) .ract .rerun{visibility:hidden}
.round:not(.open) .rh:hover .ract .rerun,.round:not(.open) .rh:focus-within .ract .rerun{visibility:visible}
.rbody{padding:2px 14px 14px}
.cols{display:grid;gap:14px}
.rfoot{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px;color:var(--muted);font-size:12.5px}
.turn+.turn{border-top:1px dashed var(--line);margin-top:14px;padding-top:12px}
.th2{display:flex;gap:8px;align-items:center;padding:2px 2px 10px;cursor:pointer;color:var(--muted);font-size:13.5px;border-radius:8px}
.round:not(.multi) .th2{display:none}
.th2 .tn{flex:none;font-size:12px;padding:1px 8px;border-radius:999px;background:var(--soft)}
.th2 .tq{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.turn:not(.folded) .th2 .tq{white-space:pre-wrap;color:var(--ink);font-weight:600}
.turn:not(.folded) .th2 .chev{transform:rotate(90deg)}
.turn.folded .tbody{display:none}
.turn.folded .th2{padding-bottom:2px}
.chev.sm{width:18px;height:18px}
.follow{display:flex;gap:10px;align-items:flex-end;margin-top:16px;padding:8px 8px 8px 14px;border:1px solid var(--line);border-radius:14px;background:var(--solid);transition:box-shadow .2s,border-color .2s}
.follow:focus-within{border-color:color-mix(in srgb,var(--focus) 45%,var(--line));box-shadow:0 0 0 4px color-mix(in srgb,var(--focus) 10%,transparent)}
.fq{flex:1;border:0;outline:0;resize:none;background:transparent;font-size:14.5px;line-height:1.6;padding:4px 0;max-height:30vh}
.fhint{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;margin:6px 4px 0;color:var(--muted);font-size:12px}
.fhint .sp{flex:1}
.fshare{display:inline-flex;align-items:center;gap:5px;color:var(--ink);cursor:pointer;font-size:12.5px}
.fshare input{margin:0}
.chip.peer{color:var(--muted)}
.attbtn{width:36px;height:36px;border-radius:10px}
.attbtn svg{width:18px;height:18px}
.atts{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.atts:empty{display:none}
.att{display:inline-flex;align-items:center;gap:7px;max-width:340px;padding:4px 6px 4px 8px;border-radius:9px;border:1px solid var(--line);background:var(--solid);font-size:12.5px}
.att .ak{flex:none;font-size:10.5px;font-weight:700;letter-spacing:.02em;padding:1px 5px;border-radius:5px;background:var(--soft);color:var(--muted)}
.att .an{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.att .as{flex:none;color:var(--muted);font-size:11.5px}
.att.err{border-color:var(--bad)}.att.err .as{color:var(--bad)}
.att.up .as::after{content:"…";animation:pulse 1.2s infinite}
.att .ax{flex:none;border:0;background:none;color:var(--muted);cursor:pointer;font-size:15px;line-height:1;padding:0 3px;border-radius:5px}
.att .ax:hover{background:var(--soft);color:var(--ink)}
a.att{text-decoration:none}a.att:hover{border-color:var(--focus)}
.tatts{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.composer.drop,.follow.drop{outline:2px dashed var(--focus);outline-offset:3px}
.chatopt{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:9px;background:color-mix(in srgb,var(--soft) 85%,transparent);color:var(--ink);cursor:pointer;white-space:nowrap}
.chip.chat{background:color-mix(in srgb,var(--focus) 12%,transparent);color:var(--focus)}
.judgeopt{display:flex;align-items:center;gap:6px;padding:2px 4px 2px 10px;border-radius:9px;background:color-mix(in srgb,var(--soft) 85%,transparent)}
.judgeopt select{border:0;background:transparent;color:var(--muted);padding:3px 4px;border-radius:6px;font-size:12.5px;cursor:pointer}
.judgeopt label{display:flex;align-items:center;gap:4px;cursor:pointer;color:var(--ink)}
#gomenu{position:fixed;z-index:60;min-width:230px;max-width:320px;padding:5px;border-radius:12px;background:var(--paper);
  border:1px solid var(--line);box-shadow:0 18px 40px -18px rgba(30,50,110,.45)}
#gomenu button{display:block;width:100%;text-align:left;border:0;background:none;padding:8px 10px;border-radius:8px;font:inherit;font-size:13px;color:var(--ink);cursor:pointer}
#gomenu button:hover,#gomenu button:focus-visible{background:var(--soft);outline:none}
#gomenu p{margin:4px 10px 6px;color:var(--muted);font-size:11.5px;line-height:1.5}
#toast{white-space:pre-line}
#ppBtn{display:inline-flex;align-items:center;gap:6px;background:#1E3A8A;border-color:#1E3A8A;color:#fff;font-weight:600;
  box-shadow:0 6px 16px -10px rgba(30,58,138,.8)}
#ppBtn:hover{background:#1E40AF;border-color:#1E40AF}
#ppBtn[aria-expanded="true"]{background:#172554;border-color:#172554}
#ppBtn:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
@media (prefers-color-scheme:dark){#ppBtn{background:#2848B0;border-color:#2848B0}#ppBtn:hover{background:#3257C8;border-color:#3257C8}
  #ppBtn[aria-expanded="true"]{background:#1E3A8A;border-color:#1E3A8A}}
#pp{position:fixed;top:0;right:0;bottom:0;width:360px;max-width:92vw;z-index:40;display:flex;flex-direction:column;
  background:var(--paper);border-left:1px solid var(--line);box-shadow:-18px 0 40px -28px rgba(40,70,140,.45);
  transform:translateX(100%);visibility:hidden;transition:transform .22s ease,visibility 0s linear .22s}
body.ppo #pp{transform:none;visibility:visible;transition:transform .22s ease}
@media (min-width:1200px){body.ppo{padding-right:360px}body.ppo #pp{box-shadow:none}}
@media (prefers-reduced-motion:reduce){#pp,body.ppo #pp{transition:none}}
.pph{display:flex;align-items:center;gap:8px;padding:0 12px 0 16px;height:56px;border-bottom:1px solid var(--line);flex:none}
.pph h2{margin:0;font-size:15px;font-weight:650}
.ppn{color:var(--muted);font-size:12.5px}
.ppbar{display:flex;gap:8px;padding:12px 16px 0;flex:none}
.ppbar input{flex:1;font-size:13px}
.ppcats{display:flex;flex-wrap:wrap;gap:6px;padding:10px 16px 0;flex:none}
.ppcats:empty{display:none}
.ppcats button{border:1px solid var(--line);background:none;border-radius:999px;padding:2px 10px;font-size:12px;color:var(--muted);cursor:pointer}
.ppcats button[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.pplist{flex:1;overflow:auto;padding:10px 10px 4px;display:flex;flex-direction:column;gap:2px}
.pi{position:relative;border-radius:10px}
.pi:hover,.pi:focus-within{background:var(--soft)}
.pib{display:block;width:100%;text-align:left;border:0;background:none;padding:9px 10px 10px;border-radius:10px;cursor:pointer;color:var(--ink);font:inherit}
.pib:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.pit{display:flex;align-items:center;gap:6px;font-weight:600;font-size:13.5px;padding-right:84px}
.pit .pin{color:var(--gold);flex:none}
.pit .pin svg{fill:currentColor}
.pic{font-weight:400;font-size:11.5px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:0 7px;flex:none}
.pip{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-top:3px;color:var(--muted);font-size:12.5px;line-height:1.5;white-space:pre-line}
.pip mark{background:color-mix(in srgb,var(--focus) 22%,transparent);color:inherit;border-radius:3px;padding:0 2px}
.pia{position:absolute;top:5px;right:6px;display:flex;gap:0;opacity:0;transition:opacity .12s}
.pi:hover .pia,.pi:focus-within .pia{opacity:1}
.pia .ib{width:26px;height:26px}
.pia .ib.on{color:var(--gold)}.pia .ib.on svg{fill:currentColor}
.pia .ib.danger{color:var(--bad);width:auto;padding:0 7px;font-size:12px}
.ppempty{padding:28px 14px;text-align:center;color:var(--muted);font-size:13px}
.ppform{flex:1;display:flex;flex-direction:column;gap:10px;padding:14px 16px;overflow:auto}
.ppform label{display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:var(--muted)}
.ppform label.grow{flex:1}
.ppform .field{font:13.5px/1.55 var(--ui);color:var(--ink)}
.ppform textarea{flex:1;min-height:180px;resize:vertical}
.ppfa{display:flex;justify-content:flex-end;gap:8px}
.pphint{margin:0;color:var(--muted);font-size:12px;line-height:1.55}
.pphelp{padding:10px 16px 4px;border-top:1px solid var(--line);flex:none}
.ppfoot{padding:0 10px 10px;flex:none}
.ppfoot button{border:0;background:none;color:var(--muted);font-size:12px;padding:4px 6px;border-radius:6px;cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.ppfoot button:hover{color:var(--ink)}
.dis{margin-top:14px;border:1px solid var(--line);border-radius:12px;background:var(--solid)}
.dis>summary{display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;list-style:none;font-size:13.5px;font-weight:600;color:var(--ink)}
.dis>summary::-webkit-details-marker{display:none}
.dis>summary .chev{transition:transform .2s}.dis[open]>summary .chev{transform:rotate(90deg)}
.dis>summary .dsub{font-weight:400;color:var(--muted);font-size:12.5px}
.dbody{padding:2px 14px 14px;display:grid;gap:12px}
.dcons{margin:0;padding:10px 12px 10px 30px;border-radius:9px;background:color-mix(in srgb,var(--ok) 8%,transparent);font-size:13px;line-height:1.6}
.dcons b{display:block;margin-left:-18px;margin-bottom:2px;color:var(--ok);font-size:12.5px}
.dc{border-top:1px solid var(--line);padding-top:12px}
.dc:first-child{border-top:0;padding-top:0}
.dt{display:flex;gap:8px;align-items:baseline;font-size:13.5px;font-weight:600;line-height:1.5;margin-bottom:8px}
.dk{flex:none;font-size:11px;font-weight:700;padding:1px 7px;border-radius:6px}
.dk.fact{background:color-mix(in srgb,var(--bad) 13%,transparent);color:var(--bad)}
.dk.advice{background:var(--gold-bg);color:var(--gold)}
.dk.emphasis{background:var(--soft);color:var(--muted)}
.dp{display:grid;gap:10px}
.dp>div{padding:8px 10px;border-radius:9px;background:color-mix(in srgb,var(--soft) 60%,transparent);border-left:3px solid var(--c);font-size:13px;line-height:1.55}
.dp>div.nm{color:var(--muted)}
.dp b{display:block;font-size:12px;margin-bottom:2px;color:var(--ink)}
.dv{margin-top:8px;font-size:12.5px;line-height:1.6;color:var(--muted)}
.dv b{color:var(--ink);font-weight:600}
@media (max-width:760px){.dp{grid-template-columns:1fr!important}}
.jd{padding:7px 12px 8px;font-size:12.5px;line-height:1.6;color:var(--muted);border-top:1px dashed var(--line);background:color-mix(in srgb,var(--soft) 55%,transparent)}
.jd b{color:var(--ink);margin-right:6px}
.jd .jx{font-size:11.5px;margin-left:6px;opacity:.85}
.jres{order:10;flex:1 1 100%;font-size:13px;line-height:1.7;color:var(--muted);white-space:normal;word-break:break-word}
.jres:empty{display:none}
.jres b{color:var(--ink)}
.jres.err{color:var(--bad)}
.jres .agree{color:var(--ok);margin-left:6px}
#sjudge{margin-top:22px}
#sjudge h3{font-size:14px;margin:0 0 10px}

/* Answer column */
.col{position:relative;display:flex;flex-direction:column;min-width:0;border:1px solid var(--line);border-radius:12px;background:var(--solid);overflow:hidden}
.col::before{content:"";position:absolute;inset:0 0 auto;height:2px;background:var(--c)}
.col.won{border-color:var(--gold);box-shadow:0 0 0 1px var(--gold)}
.ch{display:flex;align-items:center;gap:8px;padding:10px 8px 10px 14px;border-bottom:1px solid var(--line)}
.ch b{font-size:14.5px}
.mchip{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1 auto}
.state{font-size:12px;padding:1px 9px;border-radius:999px;white-space:nowrap;background:var(--soft);color:var(--muted)}
.state.run{color:var(--focus);background:color-mix(in srgb,var(--focus) 12%,transparent)}
.state.ok{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}
.state.bad{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent)}
.tm{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;min-width:3.2em;text-align:right}
.body{flex:1;padding:8px 20px 14px;max-height:min(60vh,620px);overflow:auto;overscroll-behavior:contain;min-height:150px}
.col.full .body{max-height:none}
.more{display:none;border:0;border-top:1px solid var(--line);background:var(--soft);color:var(--muted);padding:7px;font-size:12.5px;cursor:pointer}
.more:hover{color:var(--ink)}
.col.overflow .more{display:block}
.cf{display:flex;align-items:center;gap:8px;padding:8px 10px 8px 12px;border-top:1px solid var(--line)}
.stars{display:flex}
.star{border:0;background:none;padding:0 1px;font-size:17px;line-height:1;color:var(--line);cursor:pointer}
.star.on{color:var(--gold)}
.note{flex:1;min-width:0;border:0;background:transparent;font-size:12.5px;padding:4px 6px;border-radius:6px}
.note:hover,.note:focus{background:var(--soft);outline:0}
.log{padding:0 14px 8px;font-size:12px;color:var(--muted)}
.log summary{cursor:pointer;padding:4px 0}
.log pre{white-space:pre-wrap;max-height:220px;overflow:auto;margin:4px 0;font:11.5px/1.5 var(--mono);background:var(--soft);padding:8px 10px;border-radius:7px}
.ph{color:var(--muted);font-size:13.5px;margin:12px 0}
.ph a{color:var(--focus)}
.thinking::after{content:"…";animation:pulse 1.2s infinite}

/* Answer formatting */
.md{font:15.5px/1.85 var(--read);word-break:break-word}
.md>:first-child{margin-top:8px}
.md h1,.md h2,.md h3,.md h4{line-height:1.4;margin:1.2em 0 .5em}
.md h1{font-size:1.3em}.md h2{font-size:1.18em}.md h3{font-size:1.06em}
.md p,.md ul,.md ol{margin:.6em 0}
.md li{margin:.2em 0}
.md blockquote{margin:.8em 0;padding:.1em 1em;border-left:3px solid var(--line);color:var(--muted)}
.md code{font:.86em var(--mono);background:var(--soft);padding:.1em .35em;border-radius:4px}
.md pre{position:relative;background:var(--soft);padding:12px 14px;border-radius:8px;overflow:auto;font:12.5px/1.6 var(--mono)}
.md pre code{background:none;padding:0;font-size:inherit}
.md table{border-collapse:collapse;font:13.5px/1.6 var(--ui);margin:.8em 0;display:block;overflow:auto}
.md th,.md td{border:1px solid var(--line);padding:5px 10px;text-align:left}
.md th{background:var(--soft)}
.md hr{border:0;border-top:1px solid var(--line);margin:1.4em 0}
.md a{color:var(--focus)}
.pcopy{position:absolute;top:6px;right:6px;border:1px solid var(--line);background:var(--solid);border-radius:6px;font:11.5px var(--ui);padding:1px 8px;cursor:pointer;opacity:0;transition:opacity .15s}
.md pre:hover .pcopy,.pcopy:focus{opacity:1}
.rawtxt{white-space:pre-wrap;font:12.5px/1.65 var(--mono);background:var(--soft);padding:12px;border-radius:8px}

/* Reader mode */
#reader{position:fixed;inset:0;z-index:50;background:color-mix(in srgb,#14204a 38%,transparent);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);display:flex;justify-content:center;padding:24px;animation:fade .15s}
@keyframes fade{from{opacity:0}}
.rp{width:min(980px,100%);max-height:100%;display:flex;flex-direction:column;background:var(--solid);border-radius:16px;box-shadow:0 30px 80px -20px rgba(0,0,0,.45);overflow:hidden}
.rph{display:flex;align-items:center;gap:6px;padding:10px 10px 10px 14px;border-bottom:1px solid var(--line)}
.rtab{display:flex;align-items:center;gap:8px;border:0;background:none;padding:6px 12px;border-radius:8px;cursor:pointer;color:var(--muted)}
.rtab small{font-size:11.5px;opacity:.8}
.rtab.on{background:var(--soft);color:var(--ink);font-weight:600}
#rq{padding:14px 56px 0;color:var(--muted);font-size:13.5px;white-space:pre-wrap;max-height:5.5em;overflow:auto}
#rdoc{overflow:auto;padding:12px 56px 64px;outline:0}
#rdoc .md{font-size:17.5px;line-height:1.95;max-width:40em;margin:0 auto}
#rdoc .ph,#rdoc .rawtxt{max-width:46em;margin-left:auto;margin-right:auto}
.rhint{color:var(--muted);font-size:12px;padding:0 8px}

/* Other pages */
.page{padding:26px 0 60px}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:14px}
.list{list-style:none;padding:0;margin:0;background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.hitem{display:flex;gap:16px;align-items:center;padding:12px 16px;border-top:1px solid var(--line)}
.hitem:first-child{border-top:0}
.hq{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hmain{flex:1;min-width:0}
.hmeta{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12.5px;margin-top:3px}
.hact{display:flex;gap:6px}
table.grid{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.grid th,.grid td{padding:11px 14px;border-top:1px solid var(--line);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.grid th{border-top:0;color:var(--muted);font-weight:500;font-size:12.5px;background:var(--soft)}
.grid th:first-child,.grid td:first-child{text-align:left}
.grid td.tool{font-weight:600;white-space:nowrap}
.grid td.tool .dot{display:inline-block;margin-right:8px;vertical-align:1px}
.grid td.tool small{margin-left:6px}
.grid td.tool small{font-weight:400;color:var(--muted)}
.meter{display:inline-block;width:64px;height:6px;border-radius:3px;background:var(--soft);margin-left:8px;vertical-align:middle;overflow:hidden}
.meter i{display:block;height:100%;background:var(--gold)}
.grid.env td{text-align:left;white-space:normal}
.grid.env td.path{font:12px var(--mono);color:var(--muted);word-break:break-all}
.hint{font-size:12px;color:var(--muted);margin-top:4px}
.joblog{white-space:pre-wrap;background:var(--solid);border:1px solid var(--line);padding:12px;border-radius:10px;max-height:280px;overflow:auto;font:12px/1.55 var(--mono)}
.note-s{color:var(--muted);font-size:12.5px;margin-top:12px}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--ink);color:var(--solid);padding:9px 16px;border-radius:10px;font-size:13px;z-index:60;box-shadow:var(--shadow);max-width:80vw}

@media (max-width:900px){
  .wrap{padding:0 14px}.top .wrap{gap:12px}
  .lanes,.cols{grid-template-columns:1fr!important}
  .rh{grid-template-columns:auto minmax(0,1fr) auto;row-gap:6px}
  .rmeta,.ract{grid-column:2/-1;min-width:0;justify-content:flex-start}
  #rq,#rdoc{padding-left:20px;padding-right:20px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top"><div class="wrap">
  <div class="brand"><span class="logo" aria-hidden="true"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="ag2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4C8DFF"/><stop offset=".55" stop-color="#6D6AF7"/><stop offset="1" stop-color="#A47CF2"/></linearGradient></defs><rect width="32" height="32" rx="9" fill="url(#ag2)"/><rect x="8" y="10" width="4" height="14" rx="2" fill="#fff"/><rect x="14" y="15" width="4" height="9" rx="2" fill="#fff" fill-opacity=".88"/><rect x="20" y="12.5" width="4" height="11.5" rx="2" fill="#fff" fill-opacity=".76"/><circle cx="24.6" cy="7.4" r="1.7" fill="#fff"/></svg></span><span class="word">AI Arena</span></div>
  <nav>
    <button data-v="compare" class="on" data-i18n="nav.compare">Compare</button><button data-v="history" data-i18n="nav.history">History</button>
    <button data-v="stats" data-i18n="nav.stats">Stats</button><button data-v="env" data-i18n="nav.env">Setup</button>
  </nav>
  <span class="sp"></span><span id="conn" hidden data-i18n="conn">The background service is not running. Reopen AI Arena.</span>
  <button class="btn sm ghost" id="ppBtn" aria-controls="pp" aria-expanded="false"><span class="ppico"></span><span data-i18n="pp.btn">Prompts</span></button>
  <div class="langsw" role="group" aria-label="Language"></div>
  <button class="btn sm ghost" id="quit" title="Stop the background service" data-i18n="quit" data-i18n-title="quit.title">Quit</button>
</div></header>

<main class="wrap">
<section id="v-compare">
  <div class="composer">
    <div class="cin">
      <textarea id="prompt" rows="1" placeholder="Ask a question and the selected models answer side by side" data-i18n-ph="prompt.ph"></textarea>
      <button class="ib attbtn" id="attBtn" type="button" data-i18n-title="Attach files: Word, Excel, PowerPoint, PDF, text, images and more. You can also drag files here or paste them." data-i18n-aria="att.btn"></button>
      <input type="file" id="attInput" multiple hidden>
      <button class="btn primary" id="go" data-i18n="send">Send</button>
    </div>
    <div class="atts" id="atts"></div>
    <div class="lanes" id="lanes"></div>
    <div class="copts">
      <input class="field" id="tag" placeholder="Tag, e.g. writing" list="tagList" aria-label="Tag" data-i18n-ph="tag.ph" data-i18n-aria="tag"><datalist id="tagList"></datalist>
      <input class="field" id="cwd" placeholder="Working folder (optional)" aria-label="Working folder" data-i18n-ph="cwd.ph" data-i18n-aria="cwd">
      <label class="chatopt" id="chatOpt"><input type="checkbox" id="chatMode"><span data-i18n="chat.label">Chat mode</span></label>
      <span class="judgeopt" id="judgeOpt" hidden></span>
      <span class="sp"></span><span><kbd>⌘</kbd> <kbd>Enter</kbd> <span data-i18n="toSend">to send</span></span>
    </div>
  </div>
  <div class="feedhead"><h2 data-i18n="recent">Recent comparisons</h2><span class="sp"></span><button class="btn sm ghost" id="collapseAll" data-i18n="collapseAll">Collapse all</button></div>
  <div id="feed"></div>
  <p id="empty" hidden data-i18n="empty">No comparisons yet. Ask a question above and the selected models will answer side by side.</p>
</section>

<section id="v-history" class="page" hidden>
  <div class="bar"><input class="field" id="hq" placeholder="Search questions" style="flex:1" data-i18n-ph="hq.ph" data-i18n-aria="hq.ph"><select class="field" id="htag"></select></div>
  <ul class="list" id="hlist"></ul>
</section>

<section id="v-stats" class="page" hidden>
  <div class="bar"><select class="field" id="stag"></select>
    <select class="field" id="sby"><option value="" data-i18n="sby.tool">By tool</option><option value="model" data-i18n="sby.model">By tool, model and effort</option></select>
    <span id="stotal" class="note-s" style="margin:0"></span></div>
  <table class="grid" id="stable"></table>
  <div id="sjudge"></div>
  <p class="note-s" data-i18n="stats.note">Win rate is the number of times a tool was picked as best, divided by the judged rounds it took part in. Average score counts rated answers only; average time and length count successful answers only.</p>
</section>

<section id="v-env" class="page" hidden>
  <div class="bar"><button class="btn" id="recheck" data-i18n="recheck">Check again</button><span id="envNode" class="note-s" style="margin:0"></span></div>
  <div class="bar" id="openaiPreference" hidden>
    <label class="chatopt"><input type="checkbox" role="switch" id="showOpenai"><span data-i18n="e.showOpenai">Show OpenAI GPT</span></label><span class="note-s" id="openaiKeyNote" style="margin:0" hidden data-i18n="e.openaiNoKey">Set ARENA_OPENAI_API_KEY first; until then this column stays hidden in Compare</span>
  </div>
  <table class="grid env" id="etable"></table>
  <div id="ejobs"></div>
  <details class="note-s"><summary data-i18n="env.path">Searched PATH</summary><pre id="epath" class="joblog"></pre></details>
  <p class="note-s" data-i18n="env.note">For a missing tool, click Install, then Sign in and finish the one-time sign-in in the Terminal window, then click Check again.</p>
</section>
</main>

<aside id="pp" aria-labelledby="ppTitle" inert>
  <div class="pph"><h2 id="ppTitle" data-i18n="pp.title">Prompts</h2><span class="ppn" id="ppN"></span><span class="sp"></span>
    <button class="btn sm" id="ppNew" data-i18n="pp.new">New</button><button class="ib" id="ppClose" data-i18n-title="Close" data-i18n-aria="pp.close"></button></div>
  <div class="ppbar"><input class="field" type="search" id="ppQ" data-i18n-ph="pp.search" data-i18n-aria="pp.search">
    <button class="btn sm ghost" id="ppSaveCur" data-i18n="pp.saveCur" data-i18n-title="Save what is in the question box as a prompt"></button></div>
  <div class="ppcats" id="ppCats" role="group" data-i18n-aria="pp.cats"></div>
  <div class="pplist" id="ppList"></div>
  <form class="ppform" id="ppForm" hidden>
    <label><span data-i18n="pp.f.title">Title</span><input class="field" id="ppfTitle" maxlength="200" data-i18n-ph="pp.f.titlePh"></label>
    <label><span data-i18n="pp.f.cat">Category</span><input class="field" id="ppfCat" maxlength="40" list="ppCatList" data-i18n-ph="pp.f.catPh"><datalist id="ppCatList"></datalist></label>
    <label class="grow"><span data-i18n="pp.f.body">Text</span><textarea class="field" id="ppfBody" rows="10" required></textarea></label>
    <p class="pphint" data-i18n="pp.f.hint">Write the parts you change each time as {{placeholder}}. After inserting, the first one is selected; press Tab to jump to the next.</p>
    <div class="ppfa"><button type="button" class="btn sm ghost" id="ppfCancel" data-i18n="cancel">Cancel</button><button type="submit" class="btn sm primary" data-i18n="pp.f.save">Save</button></div>
  </form>
  <p class="pphint pphelp" data-i18n="pp.help">Click a prompt to insert it into the box you used last. {{ }} marks a placeholder; press Tab to jump between them.</p>
  <div class="ppfoot"><button type="button" id="ppRestore" data-i18n="pp.restore" data-i18n-title="Add back any built-in prompts you deleted; your own prompts are not affected">Restore built-in prompts</button></div>
</aside>

<div id="reader" hidden role="dialog" aria-modal="true" aria-label="Reader" data-i18n-aria="reader">
  <div class="rp">
    <div class="rph"><div id="rtabs" style="display:flex;gap:2px;flex-wrap:wrap"></div><span class="sp"></span>
      <span class="rhint" data-i18n="reader.hint">← → switch   Esc close</span>
      <button class="ib" id="rraw" title="Raw / formatted" data-i18n-title="raw"></button><button class="ib" id="rcopy" title="Copy answer" data-i18n-title="copyAns"></button><button class="ib" id="rclose" title="Close" data-i18n-title="close"></button></div>
    <div id="rq"></div>
    <div id="rdoc" tabindex="0"></div>
  </div>
</div>
<div id="toast" hidden role="status"></div>

<script type="application/json" id="i18n-data">__I18N__</script>
<script type="application/json" id="langs-data">__LANGS__</script>
<script>
const TOKEN="__TOKEN__";
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ANSI=/\x1b\[[0-9;?]*[ -\/]*[@-~]/g;
const clean=t=>String(t||'').replace(ANSI,'');
const cap=s=>s?s[0].toUpperCase()+s.slice(1):'';
const tname=t=>CFG.tools[t]?.label||cap(t);
const svg=p=>`<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
const I={chev:'<path d="M6 3.5 10.5 8 6 12.5"/>',expand:'<path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5 9 7M2.5 13.5 7 9"/>',
  copy:'<rect x="2.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M5.5 5.5v-2a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-2"/>',
  code:'<path d="M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5"/>',close:'<path d="M4 4l8 8M12 4l-8 8"/>',
  book:'<path d="M3 2.5h7.5A2.5 2.5 0 0 1 13 5v8.5H5.5A2.5 2.5 0 0 1 3 11z"/><path d="M3 11a2.5 2.5 0 0 1 2.5-2.5H13"/>',
  star:'<path d="m8 2.3 1.75 3.55 3.9.57-2.83 2.76.67 3.9L8 11.24l-3.49 1.84.67-3.9-2.83-2.76 3.9-.57z"/>',
  pen:'<path d="m10.8 2.7 2.5 2.5-7.6 7.6H3.2v-2.5z"/>',trash:'<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5"/>',
  go:'<path d="M9.5 2.5h4v4M13.5 2.5l-6 6M12 9.5v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h3"/>',
  clip:'<path d="M13.2 7.6 8.1 12.7a3.2 3.2 0 0 1-4.6-4.6l5.6-5.6a2.1 2.1 0 0 1 3 3L6.6 11a1.05 1.05 0 0 1-1.5-1.5l4.9-4.9"/>'};
/* ---------- Language ---------- */
// Interface text of every language, filled in by arena.py from locales/*.json when it serves the page.
let I18N={},LANGS=[];
try{I18N=JSON.parse($('#i18n-data').textContent);LANGS=JSON.parse($('#langs-data').textContent)}catch(e){}
if(!LANGS.length){  // an older background service is still running and did not fill in the text: offer a working Quit
  document.body.innerHTML=`<div style="max-width:620px;margin:80px auto;padding:28px 32px;border-radius:16px;background:#fff;
    box-shadow:0 18px 40px -24px rgba(30,50,110,.45);font:15px/1.7 system-ui,sans-serif;color:#1f2937">
    <h2 style="margin:0 0 8px;font-size:18px">AI Arena was updated</h2>
    <p style="margin:0 0 18px">The background service still running is an older version. Quit it, then open AI Arena again.</p>
    <button id="staleQuit" style="padding:8px 18px;border:0;border-radius:10px;background:#1E3A8A;color:#fff;font:inherit;cursor:pointer">Quit the old service</button>
    <p id="staleDone" hidden style="margin:16px 0 0;color:#047857">Stopped. Double-click AI Arena to open the new version.</p></div>`;
  document.getElementById('staleQuit').onclick=()=>fetch('/api/shutdown',{method:'POST',headers:{'Content-Type':'application/json','X-Arena-Token':TOKEN},body:'{}'})
    .catch(()=>{}).finally(()=>{document.getElementById('staleQuit').disabled=true;document.getElementById('staleDone').hidden=false});
  throw new Error('AI Arena: outdated background service');
}
const LANG_CODES=LANGS.map(l=>l.code);
let LANG=(()=>{try{const v=localStorage.getItem('arenaLang');if(LANG_CODES.includes(v))return v}catch(e){}
  for(const n of navigator.languages||[navigator.language||'']){const c=LANG_CODES.find(c=>n.toLowerCase()===c||n.toLowerCase().startsWith(c+'-'));if(c)return c}
  return LANG_CODES.includes('en')?'en':LANG_CODES[0]})();
const META=LANGS.find(l=>l.code===LANG)||{};
const TT=k=>I18N[LANG]?.[k]??I18N.en?.[k];  // raw lookup; undefined when no language has the key
const T=(k,...a)=>String(TT(k)??k).replace(/\{(\d)\}/g,(m,i)=>a[+i]??'');
const statusText=s=>TT('status.'+s)??s;
const allLangs=k=>[...new Set(LANG_CODES.map(c=>I18N[c]?.[k]).filter(Boolean))];  // a key's text in every language
const reEsc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
function applyI18n(){
  document.documentElement.lang=META.html_lang||LANG;
  $('.langsw').setAttribute('aria-label',T('lang.aria'));
  $$('[data-i18n]').forEach(e=>e.textContent=T(e.dataset.i18n));
  $$('[data-i18n-ph]').forEach(e=>e.placeholder=T(e.dataset.i18nPh));
  $$('[data-i18n-title]').forEach(e=>e.title=T(e.dataset.i18nTitle));
  $$('[data-i18n-aria]').forEach(e=>e.setAttribute('aria-label',T(e.dataset.i18nAria)));
  $$('.langsw button').forEach(b=>{b.classList.toggle('on',b.dataset.l===LANG);b.setAttribute('aria-pressed',b.dataset.l===LANG)});
}
$('.langsw').innerHTML=LANGS.map(l=>`<button data-l="${esc(l.code)}" lang="${esc(l.html_lang||l.code)}">${esc(l.name||l.code)}</button>`).join('');
$$('.langsw button').forEach(b=>b.onclick=()=>{if(b.dataset.l===LANG)return;try{localStorage.setItem('arenaLang',b.dataset.l)}catch(e){}location.reload()});
function md(t){t=clean(t);return (window.marked&&window.DOMPurify)?DOMPurify.sanitize(marked.parse(t)):'<pre class="rawtxt">'+esc(t)+'</pre>'}
const ls={get(k,d){try{return JSON.parse(localStorage.getItem(k))??d}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
function toast(m){const el=$('#toast');el.textContent=String(m);el.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>el.hidden=true,3500)}
function offline(on){$('#conn').hidden=on;$('#go').disabled=!on}
function api(path,opt={}){return fetch(path,{...opt,headers:{'Content-Type':'application/json','X-Arena-Token':TOKEN,'X-Arena-Lang':LANG}})
  .catch(()=>{offline(false);throw T('err.offline')})
  .then(async r=>{offline(true);const j=await r.json().catch(()=>({}));if(!r.ok)throw j.error||T('err.req',r.status);return j})}
const post=(p,b={})=>api(p,{method:'POST',body:JSON.stringify(b)});
function fmtTime(ts){const d=new Date(ts*1000),n=new Date(),loc=META.date_locale||LANG,hm=d.toLocaleTimeString(loc,{hour:'2-digit',minute:'2-digit',hour12:false});
  if(d.toDateString()===n.toDateString())return hm;
  return `${d.toLocaleDateString(loc,{month:'short',day:'numeric'})} ${hm}`}
function flash(b,t){if(b.dataset.f)return;b.dataset.f=1;const o=b.innerHTML;b.textContent=t;setTimeout(()=>{b.innerHTML=o;delete b.dataset.f},1100)}
function copyText(t,b){navigator.clipboard.writeText(t).then(()=>b&&flash(b,'✓')).catch(()=>toast(T('copyFail')))}

/* ---------- Config and model pickers ---------- */
let CFG={tools:{},order:[],tags:[]},SEL={},CUSTOM={},SHOW_OPENAI=ls.get('arenaShowOpenai',false)===true;
const openaiShown=()=>SHOW_OPENAI&&CFG.tools.openai?.ready!==false;  // hidden whenever no API key is set
const visibleTools=()=>CFG.order.filter(t=>t!=='openai'||openaiShown());
const openaiSelected=()=>openaiShown()&&SEL.openai?.on===true;
const color=t=>CFG.tools[t]?.color||'#8893a3';
const effName=e=>{const l=TT('eff.'+e);return l&&l!==e?l:''};  // localized effort name, e.g. for high; English shows the code itself
const effLabel=e=>e?`${e} ${effName(e)}`.trim():'';
const dlabel=t=>CFG.tools[t]?.default_label||T('default');
function modelLabel(t,id){if(!id)return dlabel(t);const m=(CFG.tools[t]?.models||[]).find(x=>x.id===id);return m?m.label:id}
function renderLanes(){
  const tools=visibleTools();
  $('#lanes').style.gridTemplateColumns=tools.length?`repeat(${tools.length},minmax(0,1fr))`:'';
  $('#lanes').innerHTML=tools.map(t=>{const v=CFG.tools[t],sel=SEL[t];
    const opts=[{id:'',label:v.ready===false?T('lane.apiKey'):dlabel(t)},...v.models,...(CUSTOM[t]||[]).map(id=>({id,label:id}))];
    return `<div class="lane${sel.on?'':' off'}" style="--c:${esc(v.color)}" data-t="${esc(t)}">
      <button class="lsw" role="switch" aria-checked="${sel.on}" aria-disabled="${v.ready===false}" title="${esc(T(v.ready===false?'lane.apiSetup':'lane.on'))}"><span class="box"></span>${esc(tname(t))}</button>
      <select class="mdl${sel.model?' set':''}" aria-label="${esc(T('lane.modelAria',tname(t)))}" title="${esc(T('lane.model'))}" ${v.flag&&v.ready!==false?'':'disabled'}>
        ${opts.map(o=>`<option value="${esc(o.id)}"${o.id===sel.model?' selected':''}>${esc(o.label)}</option>`).join('')}
        ${v.flag?`<option value="__custom">${esc(T('lane.custom'))}</option>`:''}</select>
      ${(v.efforts||[]).length?`<select class="eff${sel.effort?' set':''}" aria-label="${esc(T('lane.effAria',tname(t)))}" title="${esc(T('lane.effTitle'))}" ${v.ready===false?'disabled':''}>
        <option value="">${esc(T('lane.effDefault'))}</option>${v.efforts.map(e=>`<option value="${esc(e)}"${e===sel.effort?' selected':''} title="${esc(effName(e)||e)}">Effort ${esc(e)}</option>`).join('')}</select>`:''}</div>`}).join('');
}
$('#lanes').addEventListener('click',e=>{const b=e.target.closest('.lsw');if(!b)return;const t=b.closest('.lane').dataset.t;
  if(CFG.tools[t].ready===false){toast(T('lane.apiSetup'));return}
  SEL[t].on=!SEL[t].on;ls.set('arenaSel',SEL);renderLanes()});
$('#lanes').addEventListener('change',e=>{const s=e.target.closest('select');if(!s)return;const t=s.closest('.lane').dataset.t;
  if(s.classList.contains('eff')){SEL[t].effort=s.value;ls.set('arenaSel',SEL);renderLanes();return}
  if(s.value==='__custom'){const id=(prompt(T('lane.customPrompt',tname(t)))||'').trim();
    if(id){CUSTOM[t]=[...new Set([...(CUSTOM[t]||[]),id])];ls.set('arenaCustom',CUSTOM);SEL[t].model=id}}
  else SEL[t].model=s.value;
  ls.set('arenaSel',SEL);renderLanes()});

/* ---------- Conversation threads and rounds ---------- */
// one card = one conversation thread (th); each question in it = one round (r), scored and counted separately
const ROUNDS=new Map(),TURNS=new Map(),feed=$('#feed');
const segCls=st=>!st?'':st==='run'?'run':st==='ok'?'ok':'bad';
function stateOf(s){if(!s.status)return[T('st.queued'),''];if(s.status==='run')return[T('st.run'),'run'];if(s.status==='ok')return[T('st.ok'),'ok'];
  if(/^exit /.test(s.status))return[T('st.err',s.status.slice(5)),'bad'];return[statusText(s.status),'bad']}
const modelText=(r,t)=>{const s=r.S[t];return s&&s.actual?s.actual:modelLabel(t,(s&&s.model)||r.models[t]||'')};
const lastTurn=th=>th.turns[th.turns.length-1];
const winText=w=>w==='tie'?T('tie'):esc(T('bestOf',tname(w)));

function makeRound(meta){
  const th={id:meta.id,meta,tools:meta.tools||[],turns:[],built:false,open:false};
  ROUNDS.set(th.id,th);
  const el=th.el=document.createElement('article');el.className='round';
  el.innerHTML=`<header class="rh" tabindex="0" aria-expanded="false">
    <span class="chev">${svg(I.chev)}</span><span class="rq"></span>
    <span class="lanebar" aria-hidden="true">${th.tools.map(t=>`<i data-t="${esc(t)}" style="--c:${esc(color(t))}"></i>`).join('')}</span>
    <span class="rmeta"><span class="nturn"></span><span class="winc"></span><span class="chatc"></span><span class="tagc"></span><time></time></span>
    <span class="ract"><button class="btn sm stop" hidden>${T('stop')}</button><button class="btn sm ghost rerun" title="${esc(T('rerun.title'))}">${T('rerun')}</button></span>
  </header>
  <div class="rbody" hidden><div class="turns"></div>
    <div class="atts fatts"></div>
    <div class="follow"><textarea class="fq" rows="1" placeholder="${esc(T('fq.ph'))}" aria-label="${esc(T('fq'))}"></textarea><button class="ib attbtn fatt" type="button" title="${esc(T('att.title'))}" aria-label="${esc(T('att.btn'))}"></button><input type="file" class="fattin" multiple hidden><button class="btn primary fgo">${T('fgo')}</button></div>
    <p class="fhint"><label class="fshare" title="${esc(T('peers.title'))}"><input type="checkbox" class="fpeer"${PEERS?' checked':''}> ${T('peers.label')}</label><span class="sp"></span><span>${T('fhint')} <kbd>⌘</kbd> <kbd>Enter</kbd> ${T('toSend')}</span></p></div>`;
  $('.rq',el).textContent=meta.prompt;$('time',el).textContent=fmtTime(meta.created_at);
  if(meta.tag)$('.tagc',el).innerHTML=`<span class="chip">${esc(meta.tag)}</span>`;
  if(+meta.chat)$('.chatc',el).innerHTML=`<span class="chip chat">${T('chat.chip')}</span>`;
  if((meta.attachments||[]).length)$('.tagc',el).insertAdjacentHTML('beforebegin',`<span class="chip" title="${esc(meta.attachments.map(a=>a.name).join('\n'))}">📎 ${meta.attachments.length}</span>`);
  (meta.st||'').split('|').filter(Boolean).forEach(x=>{const i=x.indexOf(':'),seg=$(`.lanebar i[data-t="${CSS.escape(x.slice(0,i))}"]`,el);if(seg)seg.className=segCls(x.slice(i+1))});
  const w=meta.last_winner??meta.winner;if(w)$('.winc',el).innerHTML=`<span class="chip win">${winText(w)}</span>`;
  if((meta.nturns||0)>1)$('.nturn',el).innerHTML=`<span class="chip">${T('rounds',meta.nturns)}</span>`;
  const rh=$('.rh',el);
  rh.onclick=e=>{if(!e.target.closest('button'))setOpen(th,!th.open)};
  rh.onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&e.target===rh){e.preventDefault();setOpen(th,!th.open)}};
  $('.rerun',el).onclick=()=>{const keep=CHAT;CHAT=!!+meta.chat;send(meta.prompt,th.tools,meta.models||{},meta.efforts||{},meta.tag||'',meta.cwd||'',[],(meta.attachments||[]).length?th.id:'').finally(()=>CHAT=keep)};
  $('.stop',el).onclick=()=>th.turns.filter(r=>r.running).forEach(r=>post('/api/cancel/'+encodeURIComponent(r.id)).catch(toast));
  const fq=$('.fq',el);
  fq.oninput=()=>{fq.style.height='auto';fq.style.height=Math.min(fq.scrollHeight,innerHeight*.3)+'px'};
  fq.onkeydown=e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();follow(th)}};
  $('.fgo',el).onclick=()=>follow(th);
  $('.fpeer',el).onchange=e=>{PEERS=e.target.checked;ls.set('arenaPeers',PEERS);$$('.fpeer').forEach(x=>x.checked=PEERS)};
  th.att=makeAttacher($('.fatts',el),$('.fattin',el),$('.follow',el),$('.fatt',el));
  return th;
}
function setOpen(th,open){th.open=open;th.el.classList.toggle('open',open);$('.rbody',th.el).hidden=!open;$('.rh',th.el).setAttribute('aria-expanded',open);
  if(open&&!th.built)buildThread(th)}
async function buildThread(th){
  th.built=true;const box=$('.turns',th.el);box.innerHTML=`<p class="ph">${T('loading')}</p>`;
  try{const d=await api('/api/run/'+encodeURIComponent(th.id));box.innerHTML='';th.meta.cwd=d.cwd;
    (d.turns&&d.turns.length?d.turns:[d]).forEach((t,i)=>fillTurn(makeTurn(th,norm(t),i),t));
    foldOld(th);paintHead(th)}
  catch(e){box.innerHTML='';th.built=false;toast(e)}
}
function makeTurn(th,meta,idx){
  const r={id:meta.id,meta,th,idx,tools:meta.tools||[],models:meta.models||{},efforts:meta.efforts||{},S:{},winner:meta.winner||'',es:null,allFull:false,running:false,folded:false};
  TURNS.set(r.id,r);th.turns.push(r);
  const el=r.el=document.createElement('section');el.className='turn';
  el.innerHTML=`<div class="th2" tabindex="0" role="button" aria-expanded="true"><span class="chev sm">${svg(I.chev)}</span><span class="tn">${T('roundN',idx+1)}</span><span class="tq"></span>${+meta.peers?`<span class="chip peer" title="${esc(T('peers.title'))}">${T('peers.chip')}</span>`:''}<span class="tsum"></span></div>
    <div class="tbody">${(meta.attachments||[]).length?`<div class="tatts">${attLinks(r.id,meta.attachments)}</div>`:''}<div class="cols" style="grid-template-columns:repeat(${r.tools.length},minmax(0,1fr))"></div>
    <div class="rfoot"><button class="btn sm ghost allfull">${T('allFull')}</button>${CFG.judge?`<button class="btn sm ghost jbtn">${esc(T('j.ask',tname(CFG.judge)))}</button><span class="jres"></span>`:''}<span class="sp"></span><button class="btn sm ghost tie">${T('tie')}</button></div><details class="dis" hidden></details></div>`;
  $('.tq',el).textContent=meta.prompt;
  if(CFG.judge)$('.jbtn',el).onclick=()=>askJudge(r);
  const h2=$('.th2',el);h2.onclick=()=>fold(r,!r.folded);
  h2.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();fold(r,!r.folded)}};
  $('.tie',el).onclick=()=>setWinner(r,r.winner==='tie'?'':'tie');
  $('.allfull',el).onclick=e=>{r.allFull=!r.allFull;e.target.textContent=r.allFull?T('allFold'):T('allFull');r.tools.forEach(t=>{r.S[t].full=r.allFull;paint(r,t)})};
  $('.turns',th.el).appendChild(el);
  for(const t of r.tools){r.S[t]={out:'',log:'',status:'',secs:null,t0:null,raw:false,score:0,note:'',model:r.models[t]||'',effort:r.efforts[t]||'',actual:'',full:false};makeCol(r,t,$('.cols',el))}
  paintWinner(r);return r;
}
function fillTurn(r,d){   // finished rounds use the stored results; only running ones attach to the stream
  r.winner=d.winner||'';r.autoJudge=!!d.auto_judge;
  if(d.judgment){renderJudge(r,d.judgment);if(d.judgment.running)pollJudge(r)}
  for(const x of d.ratings||[])if(r.S[x.tool]){Object.assign(r.S[x.tool],{score:x.score,note:x.note||''});$('.note',r.S[x.tool].el).value=x.note||'';paintStars(r,x.tool)}
  paintWinner(r);
  if(d.running){openStream(r);return}
  const got=new Set();
  for(const o of d.outputs||[])if(r.S[o.tool]){got.add(o.tool);Object.assign(r.S[o.tool],{out:o.output||'',log:o.log||'',status:o.status,secs:o.seconds,
    actual:o.actual_model||'',model:o.model||r.S[o.tool].model,effort:o.effort||r.S[o.tool].effort});$('.ch b',r.S[o.tool].el).title=o.cmd||''}
  for(const t of r.tools){if(!got.has(t))r.S[t].status='interrupted';paint(r,t)}
}
function fold(r,f){r.folded=f;r.el.classList.toggle('folded',f);$('.th2',r.el).setAttribute('aria-expanded',!f)}
function foldOld(th){th.turns.forEach((r,i)=>fold(r,i<th.turns.length-1))}
function paintHead(th){
  const last=lastTurn(th),el=th.el;if(!last)return;
  el.classList.toggle('multi',th.turns.length>1);
  $('.nturn',el).innerHTML=th.turns.length>1?`<span class="chip">${T('rounds',th.turns.length)}</span>`:'';
  for(const t of th.tools){const seg=$(`.lanebar i[data-t="${CSS.escape(t)}"]`,el);if(seg)seg.className=segCls(last.S[t]?.status)}
  $('.winc',el).innerHTML=last.winner?`<span class="chip win">${winText(last.winner)}</span>`:'';
  const busy=th.turns.some(r=>r.running);
  $('.stop',el).hidden=!busy;el.classList.toggle('running',busy);$('.fgo',el).disabled=busy;
}
function makeCol(r,t,parent){
  const s=r.S[t],c=s.el=document.createElement('section');c.className='col';c.style.setProperty('--c',color(t));
  c.innerHTML=`<header class="ch"><span class="dot"></span><b>${esc(tname(t))}</b><span class="mchip"></span><span class="sp"></span>
    <span class="state"></span><span class="tm"></span>
    <button class="ib go" hidden title="${esc(T('go.title'))}" aria-label="${esc(T('go.title'))}" aria-haspopup="menu" aria-expanded="false">${svg(I.go)}</button><button class="ib raw" title="${esc(T('raw'))}" aria-pressed="false">${svg(I.code)}</button><button class="ib cp" title="${esc(T('copyAns'))}">${svg(I.copy)}</button><button class="ib ex" title="${esc(T('reader'))}">${svg(I.expand)}</button></header>
  <div class="body" tabindex="0"></div><button class="more">${T('more')}</button>
  <div class="cf"><span class="stars">${[1,2,3,4,5].map(i=>`<button class="star" data-v="${i}" aria-label="${esc(T('stars',i))}">★</button>`).join('')}</span>
    <input class="note" placeholder="${esc(T('note.ph'))}" aria-label="${esc(T('note.aria',tname(t)))}"><button class="btn sm ghost win">${T('best')}</button></div>
  <div class="jd" hidden></div>
  <details class="log"><summary>${T('log')}<span class="cost"></span></summary><pre></pre></details>`;
  $('.raw',c).onclick=()=>{s.raw=!s.raw;paint(r,t)};
  $('.cp',c).onclick=e=>copyText(clean(s.out),e.currentTarget);
  $('.ex',c).onclick=()=>openReader(r,t);
  $('.go',c).onclick=e=>goMenu(r,t,e.currentTarget);
  $('.more',c).onclick=()=>{s.full=!s.full;paint(r,t);if(!s.full)c.scrollIntoView({block:'nearest'})};
  $$('.star',c).forEach(b=>b.onclick=()=>rate(r,t,s.score===+b.dataset.v?0:+b.dataset.v));
  $('.note',c).onchange=e=>{s.note=e.target.value;rate(r,t,s.score)};
  $('.win',c).onclick=()=>setWinner(r,r.winner===t?'':t);
  parent.appendChild(c);paint(r,t);
}
function content(s){
  if(s.out)return s.raw?`<pre class="rawtxt">${esc(clean(s.out))}</pre>`:`<div class="md">${md(s.out)}</div>`;
  if(s.status==='not_installed')return `<p class="ph">${T('c.notFound')}<a href="#" data-go="env">${T('c.install')}</a></p>`;
  if(s.status==='run')return `<p class="ph thinking">${T('c.thinking')}</p>`;
  if(s.status)return `<p class="ph">${T('c.noOut')}</p>`;
  return `<p class="ph">${T('st.queued')}</p>`;
}
function decorate(root){$$('.md pre',root).forEach(p=>{const txt=(p.querySelector('code')||p).textContent,b=document.createElement('button');
  b.className='pcopy';b.textContent=T('copy');b.onclick=e=>{e.stopPropagation();navigator.clipboard.writeText(txt);b.textContent=T('copied');setTimeout(()=>b.textContent=T('copy'),1100)};p.appendChild(b)})}
const dirty=new Map();let raf=0;
function paint(r,t){dirty.set(r.id+'|'+t,[r,t]);if(!raf)raf=requestAnimationFrame(()=>{raf=0;for(const [a,b] of dirty.values())render(a,b);dirty.clear()})}
function render(r,t){
  const s=r.S[t],c=s&&s.el;if(!c)return;
  const body=$('.body',c),atBottom=body.scrollHeight-body.scrollTop-body.clientHeight<60;
  body.innerHTML=content(s);decorate(body);
  c.classList.toggle('full',s.full);
  if(s.status==='run'&&atBottom&&!s.full)body.scrollTop=body.scrollHeight;
  c.classList.toggle('overflow',s.full||body.scrollHeight>body.clientHeight+8);
  $('.more',c).textContent=s.full?T('less'):T('more');
  $('.log pre',c).textContent=clean(s.log)||T('logEmpty');
  $('.go',c).hidden=!(s.status==='ok'&&canContinue(t));
  const [lab,cls]=stateOf(s),st=$('.state',c);st.textContent=lab;st.className='state '+cls;
  const mc=$('.mchip',c);mc.textContent=modelText(r,t)+(s.effort?' / '+s.effort:'');
  mc.title=(s.actual?T('m.actual',s.actual)+(s.model?T('m.chosen',modelLabel(t,s.model)):''):T('m.model',modelLabel(t,s.model)))
    +(s.effort?'\n'+T('m.effort',effLabel(s.effort)):'');
  const cost=(s.log.match(/cost=\$([\d.]+)/)||[])[1];$('.cost',c).textContent=cost?T('cost',(+cost).toFixed(3)):'';
  $('.raw',c).setAttribute('aria-pressed',s.raw);
  if(r===lastTurn(r.th))paintHead(r.th);
  if(READER.r===r&&READER.t===t)renderReader(false);
}
setInterval(()=>{for(const r of TURNS.values())for(const t of r.tools){const s=r.S[t],el=s&&s.el&&$('.tm',s.el);if(!el)continue;
  el.textContent=s.secs!=null?s.secs+'s':s.t0?((Date.now()-s.t0)/1000).toFixed(1)+'s':''}},200);
function setRunning(r,on){r.running=on;paintHead(r.th)}
function openStream(r){
  if(r.es)r.es.close();
  const es=r.es=new EventSource(`/api/stream/${encodeURIComponent(r.id)}?token=${TOKEN}`);
  es.onopen=()=>{for(const t of r.tools){Object.assign(r.S[t],{out:'',log:'',status:'',secs:null,t0:null,actual:''});paint(r,t)}};
  es.onmessage=e=>{const ev=JSON.parse(e.data);
    if(ev.type==='end'){es.close();r.es=null;setRunning(r,false);refreshTags();if(r.autoJudge)pollJudge(r,true);return}
    const s=r.S[ev.tool];if(!s)return;
    if(ev.type==='start'){s.status='run';s.t0=Date.now();if(ev.model!=null)s.model=ev.model;if(ev.effort!=null)s.effort=ev.effort;setRunning(r,true);$('.ch b',s.el).title=ev.cmd||''}
    else if(ev.type==='out')s.out+=ev.data;else if(ev.type==='log')s.log+=ev.data;
    else if(ev.type==='meta')s.actual=ev.model;else if(ev.type==='done'){s.status=ev.status;s.secs=ev.seconds}
    paint(r,ev.tool)};
}
function paintStars(r,t){$$('.star',r.S[t].el).forEach(b=>b.classList.toggle('on',+b.dataset.v<=r.S[t].score))}
function rate(r,t,v){r.S[t].score=v;paintStars(r,t);post('/api/rate',{run_id:r.id,tool:t,score:v,note:r.S[t].note}).catch(toast)}
function paintWinner(r){const w=r.winner;
  for(const t in r.S){const c=r.S[t].el;if(c){c.classList.toggle('won',w===t);$('.win',c).classList.toggle('on',w===t)}}
  $('.tie',r.el).classList.toggle('on',w==='tie');
  $('.tsum',r.el).innerHTML=w?`<span class="chip win">${winText(w)}</span>`:'';
  if(r===lastTurn(r.th))paintHead(r.th)}
function setWinner(r,w){r.winner=w;r.meta.winner=w;paintWinner(r);if(r.judge)renderJudge(r,r.judge);post('/api/winner',{run_id:r.id,winner:w}).catch(toast)}

/* ---------- Continue a session in the tool's own app or in Terminal ---------- */
const canContinue=t=>!!(CFG.tools[t]?.continue_app||CFG.tools[t]?.continue_terminal);
function goClose(){const m=$('#gomenu');if(!m)return;m.remove();$$('.ib.go[aria-expanded="true"]').forEach(b=>b.setAttribute('aria-expanded','false'))}
function goMenu(r,t,btn){
  const open=btn.getAttribute('aria-expanded')==='true';goClose();if(open)return;
  const v=CFG.tools[t]||{},m=document.createElement('div');m.id='gomenu';m.setAttribute('role','menu');
  m.innerHTML=(v.continue_app?`<button role="menuitem" data-a="app">${esc(T('go.app',v.continue_app))}</button>`:'')
    +(v.continue_terminal?`<button role="menuitem" data-a="terminal">${esc(T('go.terminal'))}</button><button role="menuitem" data-a="command">${esc(T('go.copy'))}</button>`:'')
    +`<p>${esc(T('go.note'))}</p>`;
  document.body.appendChild(m);btn.setAttribute('aria-expanded','true');
  const b=btn.getBoundingClientRect(),w=m.offsetWidth;
  m.style.top=(b.bottom+6)+'px';m.style.left=Math.max(8,Math.min(innerWidth-w-8,b.right-w))+'px';
  m.querySelector('button')?.focus();
  m.onclick=async e=>{const it=e.target.closest('button');if(!it)return;goClose();
    try{const d=await post('/api/continue',{run:r.id,tool:t,action:it.dataset.a});
      if(it.dataset.a==='command'){await navigator.clipboard.writeText(d.command).catch(()=>{});return toast(T('go.copied')+'\n'+d.command)}
      toast(d.message+(it.dataset.a==='app'&&v.continue_via_url?' '+T('go.urlHint'):''))}catch(err){toast(err)}};
  m.onkeydown=e=>{if(e.key==='Escape'){goClose();btn.focus()}};
}
document.addEventListener('mousedown',e=>{if($('#gomenu')&&!e.target.closest('#gomenu,.ib.go'))goClose()});
addEventListener('scroll',goClose,true);

/* ---------- Prompt library (right-side panel) ---------- */
const PP={items:[],cat:'',q:'',edit:null,target:null,loaded:false};
const PH_RE=/\{\{[^{}\n]*\}\}/g;
const ppDocked=()=>matchMedia('(min-width:1200px)').matches;
function ppOpen(on){
  document.body.classList.toggle('ppo',on);$('#pp').inert=!on;$('#ppBtn').setAttribute('aria-expanded',on);ls.set('arenaPP',on);
  if(on){if(!PP.loaded)ppLoad();setTimeout(()=>$('#ppQ').focus(),60)}
}
async function ppLoad(){try{PP.items=await api('/api/prompts');PP.loaded=true;ppRender()}catch(e){toast(e)}}
function ppRender(){
  $('#ppN').textContent=PP.items.length?String(PP.items.length):'';
  const cats=[...new Set([...PP.items].sort((a,b)=>b.created_at-a.created_at).map(x=>x.category).filter(Boolean))];  // your categories first, then built-in categories in catalog order
  if(PP.cat&&!cats.includes(PP.cat))PP.cat='';
  $('#ppCats').innerHTML=cats.length?[['',T('pp.all')],...cats.map(c=>[c,c])].map(([v,l])=>`<button type="button" data-c="${esc(v)}" aria-pressed="${PP.cat===v}">${esc(l)}</button>`).join(''):'';
  $('#ppCatList').innerHTML=cats.map(c=>`<option value="${esc(c)}">`).join('');
  const qq=PP.q.trim().toLowerCase();
  const list=PP.items.filter(x=>(!PP.cat||x.category===PP.cat)&&(!qq||(x.title+'\n'+x.body+'\n'+x.category).toLowerCase().includes(qq)));
  const hl=s=>esc(s).replace(/\{\{[^{}\n]*\}\}/g,m=>`<mark>${m}</mark>`);
  $('#ppList').innerHTML=list.length?list.map(x=>`<div class="pi" data-id="${esc(x.id)}">
    <button type="button" class="pib" title="${esc(T('pp.insert'))}"><span class="pit">${x.pinned?`<span class="pin" aria-label="${esc(T('pp.pinned'))}">${svg(I.star)}</span>`:''}<span>${esc(x.title)}</span>${x.category?`<span class="pic">${esc(x.category)}</span>`:''}</span>
    <span class="pip">${hl(x.body)}</span></button>
    <div class="pia"><button type="button" class="ib pinb${x.pinned?' on':''}" aria-pressed="${!!x.pinned}" title="${esc(T(x.pinned?'pp.unpin':'pp.pin'))}" aria-label="${esc(T(x.pinned?'pp.unpin':'pp.pin'))}">${svg(I.star)}</button><button type="button" class="ib edb" title="${esc(T('pp.edit'))}" aria-label="${esc(T('pp.edit'))}">${svg(I.pen)}</button><button type="button" class="ib delb" title="${esc(T('pp.del'))}" aria-label="${esc(T('pp.del'))}">${svg(I.trash)}</button></div></div>`).join('')
    :`<p class="ppempty">${esc(T(PP.items.length?'pp.noMatch':'pp.empty'))}</p>`;
}
function ppForm(item,body){
  PP.edit=item||{};$('#ppForm').hidden=false;$('#ppList').hidden=$('.ppbar').hidden=$('#ppCats').hidden=$('.pphelp').hidden=$('.ppfoot').hidden=true;
  $('#ppfTitle').value=item?.title||'';$('#ppfCat').value=item?.category||PP.cat||'';$('#ppfBody').value=body??item?.body??'';
  ($('#ppfBody').value?$('#ppfTitle'):$('#ppfBody')).focus();
}
function ppFormClose(){PP.edit=null;$('#ppForm').hidden=true;$('#ppList').hidden=$('.ppbar').hidden=$('#ppCats').hidden=$('.pphelp').hidden=$('.ppfoot').hidden=false}
function ppTarget(){const t=PP.target;return t&&t.isConnected&&t.offsetParent?t:$('#prompt')}
function selectPlaceholder(ta,from){
  PH_RE.lastIndex=0;const all=[...ta.value.matchAll(PH_RE)];if(!all.length)return false;
  const m=all.find(x=>x.index>=from)||all[0];ta.focus();ta.setSelectionRange(m.index,m.index+m[0].length);return true;
}
function ppInsert(x){
  if(!$('#v-compare').offsetParent)show('compare');
  const ta=ppTarget(),s=ta.selectionStart??ta.value.length,e=ta.selectionEnd??s;
  const pre=ta.value.slice(0,s),sep=pre&&!/\n\s*$/.test(pre)?'\n\n':'';
  ta.focus();ta.setRangeText(sep+x.body,s,e,'end');ta.dispatchEvent(new Event('input',{bubbles:true}));
  if(!selectPlaceholder(ta,s))ta.setSelectionRange(ta.selectionEnd,ta.selectionEnd);
  post('/api/prompts/use/'+encodeURIComponent(x.id)).catch(()=>{});
  if(!ppDocked())ppOpen(false);
}
document.addEventListener('focusin',e=>{if(e.target.matches('#prompt,.fq'))PP.target=e.target});
document.addEventListener('keydown',e=>{  // Tab jumps to the next {{placeholder}}
  const ta=e.target;if(e.key!=='Tab'||e.shiftKey||e.altKey||e.metaKey||e.ctrlKey||!ta.matches?.('#prompt,.fq'))return;
  PH_RE.lastIndex=0;if(!PH_RE.test(ta.value))return;
  e.preventDefault();selectPlaceholder(ta,ta.selectionEnd);
});
function ppInit(){
  $('#ppBtn .ppico').innerHTML=svg(I.book);$('#ppClose').innerHTML=svg(I.close);
  $('#ppBtn').onclick=()=>ppOpen(!document.body.classList.contains('ppo'));
  $('#ppClose').onclick=()=>{ppOpen(false);$('#ppBtn').focus()};
  $('#pp').addEventListener('keydown',e=>{if(e.key==='Escape'){e.stopPropagation();if(!$('#ppForm').hidden)ppFormClose();else{ppOpen(false);$('#ppBtn').focus()}}});
  $('#ppQ').oninput=e=>{PP.q=e.target.value;ppRender()};
  $('#ppQ').onkeydown=e=>{if(e.key==='Enter'){const b=$('#ppList .pib');if(b){e.preventDefault();b.click()}}};
  $('#ppCats').onclick=e=>{const b=e.target.closest('button');if(!b)return;PP.cat=b.dataset.c;ppRender()};
  $('#ppNew').onclick=()=>ppForm(null);
  $('#ppSaveCur').onclick=()=>{const v=ppTarget().value.trim();if(!v)return toast(T('pp.nothing'));ppForm(null,v)};
  $('#ppfCancel').onclick=ppFormClose;
  $('#ppRestore').onclick=async()=>{try{const r=await post('/api/prompts/restore');await ppLoad();toast(r.restored?T('pp.restored',r.restored):T('pp.restoredNone'))}catch(e){toast(e)}};
  $('#ppForm').onsubmit=async e=>{e.preventDefault();
    const body=$('#ppfBody').value.trim();if(!body)return $('#ppfBody').focus();
    try{const it=await post('/api/prompts',{id:PP.edit?.id||'',title:$('#ppfTitle').value,category:$('#ppfCat').value,body,pinned:PP.edit?.pinned||0});
      const i=PP.items.findIndex(x=>x.id===it.id);if(i>=0)PP.items[i]=it;else PP.items.unshift(it);
      ppFormClose();ppRender();toast(T('pp.saved'))}catch(err){toast(err)}};
  $('#ppList').onclick=async e=>{
    const row=e.target.closest('.pi');if(!row)return;const x=PP.items.find(i=>i.id===row.dataset.id);if(!x)return;
    if(e.target.closest('.pinb')){x.pinned=x.pinned?0:1;await post('/api/prompts/pin/'+encodeURIComponent(x.id),{pinned:x.pinned}).catch(toast);
      PP.items.sort((a,b)=>(b.pinned-a.pinned)||(b.created_at-a.created_at));return ppRender()}
    if(e.target.closest('.edb'))return ppForm(x);
    const d=e.target.closest('.delb');
    if(d){if(d.dataset.sure){await post('/api/prompts/delete/'+encodeURIComponent(x.id)).catch(toast);PP.items=PP.items.filter(i=>i!==x);return ppRender()}
      d.dataset.sure='1';d.classList.add('danger');d.textContent=T('pp.sure');setTimeout(()=>{if(d.isConnected){delete d.dataset.sure;d.classList.remove('danger');d.innerHTML=svg(I.trash)}},3000);return}
    if(e.target.closest('.pib'))ppInsert(x);
  };
  if(ls.get('arenaPP',false))ppOpen(true);
}

/* ---------- Dynamic model lists (agy models is read in the background and added to the picker when ready) ---------- */
function modelsLater(n){
  if(n>=6||!CFG.order.some(t=>CFG.tools[t].dynamic&&!(CFG.tools[t].models||[]).length))return;
  setTimeout(async()=>{try{const c=await api('/api/config');let got=false;
    for(const t of CFG.order)if(CFG.tools[t].dynamic&&!(CFG.tools[t].models||[]).length&&(c.tools[t]?.models||[]).length){CFG.tools[t].models=c.tools[t].models;got=true}
    if(got)renderLanes()}catch(e){}modelsLater(n+1)},10000);
}

/* ---------- Attachments ---------- */
const fmtSize=n=>n<1024?n+' B':n<1048576?(n/1024).toFixed(0)+' KB':(n/1048576).toFixed(1)+' MB';
const kindTag=a=>({Word:'DOC',Excel:'XLS',PowerPoint:'PPT',PDF:'PDF',RTF:'RTF',ODT:'ODT',CSV:'CSV',image:'IMG',text:'TXT'}[a.kind]||(a.ext||'').replace('.','').slice(0,4).toUpperCase()||'FILE');
const attStatus=a=>a.chars>0?T('att.chars',a.chars.toLocaleString()):a.kind==='image'?T('att.image'):T('att.noText');
function makeAttacher(box,input,dropZone,btn){
  const A={items:[]};
  const render=()=>{box.innerHTML=A.items.map((a,i)=>`<span class="att${a.state==='up'?' up':a.state==='err'?' err':''}" title="${esc(a.name+'  '+fmtSize(a.size||0))}">
    <span class="ak">${esc(kindTag(a))}</span><span class="an">${esc(a.name)}</span>
    <span class="as">${a.state==='up'?T('att.uploading'):a.state==='err'?esc(a.err||T('att.fail')):esc(attStatus(a))}</span>
    <button class="ax" type="button" data-i="${i}" aria-label="${esc(T('att.remove'))} ${esc(a.name)}">×</button></span>`).join('')};
  box.addEventListener('click',e=>{const b=e.target.closest('.ax');if(!b)return;A.items.splice(+b.dataset.i,1);render()});
  A.add=files=>{for(const f of files){
    if(f.size>50*1048576){toast(T('att.tooBig',f.name));continue}
    const it={name:f.name,size:f.size,state:'up',ext:(f.name.match(/\.[^.]+$/)||[''])[0].toLowerCase()};A.items.push(it);render();
    fetch('/api/upload',{method:'POST',headers:{'X-Arena-Token':TOKEN,'X-Arena-Lang':LANG,'X-File-Name':encodeURIComponent(f.name),'Content-Type':'application/octet-stream'},body:f})
      .then(async r=>{const j=await r.json().catch(()=>({}));if(!r.ok)throw j.error||T('att.fail');Object.assign(it,j,{state:'ok'})})
      .catch(e=>{it.state='err';it.err=String(e)}).finally(render)}};
  A.ids=()=>A.items.filter(a=>a.state==='ok').map(a=>a.id);
  A.pending=()=>A.items.some(a=>a.state==='up');
  A.clear=()=>{A.items=[];render()};
  btn.innerHTML=svg(I.clip);btn.onclick=()=>input.click();
  input.onchange=()=>{A.add([...input.files]);input.value=''};
  dropZone.addEventListener('dragover',e=>{if([...e.dataTransfer.types].includes('Files')){e.preventDefault();dropZone.classList.add('drop')}});
  dropZone.addEventListener('dragleave',e=>{if(!dropZone.contains(e.relatedTarget))dropZone.classList.remove('drop')});
  dropZone.addEventListener('drop',e=>{if(!e.dataTransfer.files.length)return;e.preventDefault();dropZone.classList.remove('drop');A.add([...e.dataTransfer.files])});
  dropZone.addEventListener('paste',e=>{const fs=[...(e.clipboardData?.files||[])];if(fs.length){e.preventDefault();A.add(fs)}});
  return A;
}
const fileUrl=(rid,a)=>`/api/file/${encodeURIComponent(rid)}/${encodeURIComponent(a.id)}/${encodeURIComponent(a.name)}?token=${TOKEN}`;
function attLinks(rid,list){return (list||[]).map(a=>`<a class="att" href="${fileUrl(rid,a)}" target="_blank" rel="noopener" title="${esc(T('att.open'))}"><span class="ak">${esc(kindTag(a))}</span><span class="an">${esc(a.name)}</span><span class="as">${esc(attStatus(a))}</span></a>`).join('')}
let COMPOSER_ATT=null;

/* ---------- Scoring (anonymous review by a third-party model) ---------- */
let JSEL={model:'',auto:false},CHAT=true,PEERS=(()=>{try{return localStorage.getItem('arenaPeers')!=='false'}catch(e){return true}})();
function renderJudgeOpt(){
  const box=$('#judgeOpt');if(!CFG.judge){box.hidden=true;return}
  const v=CFG.tools[CFG.judge],opts=[{id:'',label:dlabel(CFG.judge)},...(v.models||[])];
  box.hidden=false;
  box.innerHTML=`<label title="${esc(T('j.autoTitle',tname(CFG.judge)))}"><input type="checkbox" id="jauto" ${JSEL.auto?'checked':''}>${esc(T('j.auto',tname(CFG.judge)))}</label>
    <select id="jmodel" aria-label="${esc(T('j.modelAria'))}">${opts.map(o=>`<option value="${esc(o.id)}"${o.id===JSEL.model?' selected':''}>${esc(o.label)}</option>`).join('')}</select>`;
  $('#jauto').onchange=e=>{JSEL.auto=e.target.checked;ls.set('arenaJudge',JSEL)};
  $('#jmodel').onchange=e=>{JSEL.model=e.target.value;ls.set('arenaJudge',JSEL)};
}
async function askJudge(r){
  const b=$('.jbtn',r.el);b.disabled=true;
  try{await post('/api/judge/'+encodeURIComponent(r.id),{model:JSEL.model});renderJudge(r,{status:'running',running:true});pollJudge(r)}
  catch(e){b.disabled=false;toast(e)}
}
function pollJudge(r,waitStart){
  clearTimeout(r.jt);let tries=0;
  const tick=async()=>{tries++;
    try{const j=await api('/api/judge/'+encodeURIComponent(r.id));renderJudge(r,j);if(j.running||j.status==='running')r.jt=setTimeout(tick,2000)}
    catch(e){if(waitStart&&tries<8)r.jt=setTimeout(tick,1500)}};
  r.jt=setTimeout(tick,waitStart?1200:2000);
}
function deanon(t,map){  // map the anonymous labels in Grok's comments (Answer A, A vs C) back to tool names
  const nm=l=>map&&map[l]?tname(map[l]):null;
  return String(t||'').replace(new RegExp(`(?:${[...allLangs('j.answerWord'),'Answer','Response'].map(reEsc).join('|')})\\s*([A-H])\\b`,'g'),(m,l)=>nm(l)||m).replace(/\b([A-H])'s\b/g,(m,l)=>nm(l)?nm(l)+"'s":m)
    .replace(/(^|[^A-Za-z0-9])([A-H])(?=\s*(?:[\u4e00-\u9fff，。、：；！？,.;:!?）)]|$))/g,(m,p,l)=>nm(l)?p+nm(l):m);
}
function renderJudge(r,j){
  r.judge=j;const name=esc(tname(CFG.judge||'grok')),b=$('.jbtn',r.el),res=$('.jres',r.el);if(!res)return;
  const busy=j.running||j.status==='running';
  b.disabled=busy;b.textContent=busy?T('j.busy',tname(CFG.judge)):j.status==='ok'?T('j.again'):j.status?T('j.retry'):T('j.ask',tname(CFG.judge));
  res.classList.toggle('err',j.status==='error');res.title='';
  for(const t of r.tools){const jd=$('.jd',r.S[t].el);jd.hidden=true;jd.innerHTML=''}
  {const d=$('.dis',r.el);if(d&&j.status!=='ok'){d.hidden=true;d.innerHTML=''}}
  if(busy){const tail=clean(j.raw||'').trim().split('\n').filter(Boolean).pop()||'';
    res.textContent=T('j.sending',tname(CFG.judge||'grok'))+(j.elapsed!=null?'  '+T('j.elapsed',Math.round(j.elapsed)):'');
    res.title=tail?T('j.last',tail.slice(-300)):'';return}
  if(j.status==='error'){res.textContent=T('j.fail',j.reason||T('j.unknown'));res.title=(j.raw||'').slice(-2000);return}
  if(j.status!=='ok'){res.textContent='';return}
  for(const x of j.scores||[]){const s=r.S[x.tool];if(!s)continue;const jd=$('.jd',s.el);jd.hidden=false;
    const det=Object.entries(x.detail||{}).map(([k,v])=>(T('k.'+k)||k)+' '+v).join('  ');
    jd.innerHTML=`<b>${name} ${x.overall!=null?x.overall:'—'}/5</b>${esc(deanon(x.comment,j.mapping))}${det?`<span class="jx">${esc(det)}</span>`:''}`}
  const best=j.best==='tie'?T('tie'):j.best?tname(j.best):T('j.none');
  const agree=r.winner&&j.best?(r.winner===j.best?`<span class="agree">${T('j.agree')}</span>`:''):'';
  renderConflicts(r,j);
  const why=deanon(j.reason,j.mapping);res.innerHTML=`${esc(T('j.best',tname(CFG.judge||'grok')))}<b>${esc(best)}</b>${agree}${why?esc(T('j.sep'))+esc(why):''}`;res.title=why;
}

function renderConflicts(r,j){  // consensus and conflicts: each tool's position sits under that tool's answer column
  const box=$('.dis',r.el);if(!box)return;
  const res=j.result||{},cons=res.consensus_n||[],conf=res.conflicts_n||[];
  if(!cons.length&&!conf.length){box.hidden=true;box.innerHTML='';return}
  const nf=conf.filter(c=>c.kind==='fact').length,wasOpen=box.dataset.done?box.open:null;
  const sub=[conf.length?T(conf.length===1?'d.nConf1':'d.nConf',conf.length)+(nf?T('d.nFact',nf):''):T('d.noConf'),cons.length?T(cons.length===1?'d.nCons1':'d.nCons',cons.length):''].filter(Boolean).join(T('d.dot'));
  const D=x=>esc(deanon(x,j.mapping));
  box.innerHTML=`<summary><span class="chev sm">${svg(I.chev)}</span>${esc(T('d.title'))}<span class="dsub">${esc(sub)}</span></summary>
  <div class="dbody">${conf.map(c=>`<div class="dc"><div class="dt"><span class="dk ${esc(c.kind)}">${esc(T('d.k.'+c.kind))}</span><span>${D(c.topic)}</span></div>
    <div class="dp" style="grid-template-columns:repeat(${r.tools.length},minmax(0,1fr))">${r.tools.map(t=>{const v=(c.positions||{})[t];
      const nm=!v||new RegExp(`^(${[...allLangs('d.na'),'not mentioned'].map(reEsc).join('|')})[.。]?$`,'i').test(v.trim());
      return `<div class="${nm?'nm':''}" style="--c:${esc(color(t))}"><b>${esc(tname(t))}</b>${v?D(v):esc(T('d.na'))}</div>`}).join('')}</div>
    ${c.verdict?`<div class="dv"><b>${esc(T('d.verdict',tname(CFG.judge||'grok')))}</b>${D(c.verdict)}</div>`:''}</div>`).join('')}
  ${cons.length?`<ul class="dcons"><b>${esc(T('d.cons'))}</b>${cons.map(x=>`<li>${D(x)}</li>`).join('')}</ul>`:''}</div>`;
  box.hidden=false;box.open=wasOpen!=null?wasOpen:conf.length>0;box.dataset.done='1';
}

async function send(prompt,tools,models,efforts,tag,cwd,atts,copyFrom){
  tools=tools.filter(t=>t!=='openai'||openaiSelected());
  if(!tools.length){toast(T('t.enableOpenai'));return false}
  try{const d=await post('/api/run',{prompt,tools,models,efforts,tag,cwd,judge:!!(CFG.judge&&JSEL.auto),judge_model:JSEL.model,chat:CHAT,attachments:(atts||[]).map(a=>a.id),copy_attachments_from:copyFrom||''});
    for(const x of ROUNDS.values())if(x.open)setOpen(x,false);
    const meta={id:d.id,prompt,tag:d.tag,created_at:Date.now()/1000,tools:d.tools,models:d.models||{},efforts:d.efforts||{},winner:'',cwd,chat:d.chat?1:0,
      attachments:d.attachments||(atts||[]).map(({id,name,size,kind,ext,chars})=>({id,name,size,kind,ext,chars}))};
    const th=makeRound(meta);th.built=true;
    $('#empty').hidden=true;feed.prepend(th.el);
    const r=makeTurn(th,meta,0);r.autoJudge=!!(CFG.judge&&JSEL.auto);setOpen(th,true);paintHead(th);openStream(r);
    window.scrollTo({top:0,behavior:'smooth'});return true}
  catch(e){toast(e);return false}
}
async function follow(th){
  const fq=$('.fq',th.el),p=fq.value.trim(),last=lastTurn(th);if(!p)return fq.focus();if(!last)return;
  if(th.turns.some(r=>r.running))return toast(T('t.busy'));
  if(th.att.pending())return toast(T('att.pending'));
  const atts=th.att.items.filter(a=>a.state==='ok');
  try{const d=await post('/api/run',{prompt:p,parent:last.id,judge:!!(CFG.judge&&JSEL.auto),judge_model:JSEL.model,attachments:atts.map(a=>a.id),share_peers:PEERS,include_openai:openaiSelected()});
    const r=makeTurn(th,{id:d.id,prompt:p,tools:d.tools,models:d.models||{},efforts:d.efforts||{},winner:'',peers:d.peers||0,
      attachments:atts.map(({id,name,size,kind,ext,chars})=>({id,name,size,kind,ext,chars}))},th.turns.length);
    th.att.clear();
    r.autoJudge=!!(CFG.judge&&JSEL.auto);
    foldOld(th);paintHead(th);openStream(r);fq.value='';fq.style.height='auto';
    r.el.scrollIntoView({behavior:'smooth',block:'start'})}
  catch(e){toast(e)}
}
function submitComposer(){
  const p=$('#prompt').value.trim();if(!p)return $('#prompt').focus();
  const tools=visibleTools().filter(t=>SEL[t].on);if(!tools.length)return toast(T('t.pickTool'));
  if(COMPOSER_ATT.pending())return toast(T('att.pending'));
  const atts=COMPOSER_ATT.items.filter(a=>a.state==='ok');
  send(p,tools,Object.fromEntries(tools.map(t=>[t,SEL[t].model||''])),Object.fromEntries(tools.map(t=>[t,SEL[t].effort||''])),
    $('#tag').value.trim(),$('#cwd').value.trim(),atts)
    .then(ok=>{if(ok){$('#prompt').value='';autosize();COMPOSER_ATT.clear()}});
}
function autosize(){const p=$('#prompt');p.style.height='auto';p.style.height=Math.min(p.scrollHeight,innerHeight*.4)+'px'}
$('#prompt').addEventListener('input',autosize);
$('#prompt').addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();submitComposer()}});
$('#go').onclick=submitComposer;
$('#collapseAll').onclick=()=>{for(const th of ROUNDS.values())if(th.open)setOpen(th,false)};
const pj=(v,d)=>typeof v==='string'?(()=>{try{return JSON.parse(v||'null')??d}catch(e){return d}})():(v||d);
const norm=m=>({...m,tools:pj(m.tools,[]),models:pj(m.models,{}),efforts:pj(m.efforts,{}),attachments:pj(m.attachments,[])});
async function loadFeed(){
  const d=await api('/api/history?limit=20');feed.innerHTML='';ROUNDS.clear();TURNS.clear();
  for(const m of d.runs)feed.appendChild(makeRound(norm(m)).el);
  $('#empty').hidden=d.runs.length>0;
  const first=ROUNDS.values().next().value;if(first)setOpen(first,true);
}
async function openRun(id){
  show('compare');let th=ROUNDS.get(id);
  if(!th){const d=await api('/api/run/'+encodeURIComponent(id)),turns=d.turns||[d],last=turns[turns.length-1];
    th=makeRound(norm({...d,nturns:turns.length,last_winner:last.winner,st:(last.outputs||[]).map(o=>o.tool+':'+o.status).join('|')}));
    feed.prepend(th.el);$('#empty').hidden=true}
  for(const x of ROUNDS.values())if(x.open&&x!==th)setOpen(x,false);
  setOpen(th,true);th.el.scrollIntoView({behavior:'smooth',block:'start'});
}

/* ---------- Reader mode ---------- */
const READER={r:null,t:null};
$('#rraw').innerHTML=svg(I.code);$('#rcopy').innerHTML=svg(I.copy);$('#rclose').innerHTML=svg(I.close);
function openReader(r,t){READER.r=r;READER.t=t;$('#reader').hidden=false;document.documentElement.classList.add('lock');renderReader(true);$('#rdoc').focus()}
function closeReader(){READER.r=null;$('#reader').hidden=true;document.documentElement.classList.remove('lock')}
function renderReader(reset){const {r,t}=READER;if(!r)return;const s=r.S[t],doc=$('#rdoc'),y=doc.scrollTop;
  $('#rtabs').innerHTML=r.tools.map(x=>`<button class="rtab${x===t?' on':''}" data-t="${esc(x)}" style="--c:${esc(color(x))}"><span class="dot"></span>${esc(tname(x))}<small>${esc(modelText(r,x))}${r.S[x]?.effort?' '+esc(r.S[x].effort):''}</small></button>`).join('');
  $('#rq').textContent=r.meta.prompt;doc.innerHTML=content(s);decorate(doc);doc.scrollTop=reset?0:y;$('#rraw').setAttribute('aria-pressed',s.raw)}
function readerStep(d){const {r,t}=READER,i=r.tools.indexOf(t);READER.t=r.tools[(i+d+r.tools.length)%r.tools.length];renderReader(true)}
$('#rtabs').onclick=e=>{const b=e.target.closest('.rtab');if(b){READER.t=b.dataset.t;renderReader(true)}};
$('#rclose').onclick=closeReader;
$('#reader').onclick=e=>{if(e.target.id==='reader')closeReader()};
$('#rraw').onclick=()=>{const s=READER.r.S[READER.t];s.raw=!s.raw;paint(READER.r,READER.t);renderReader(false)};
$('#rcopy').onclick=e=>copyText(clean(READER.r.S[READER.t].out),e.currentTarget);
document.addEventListener('keydown',e=>{if($('#reader').hidden)return;
  if(e.key==='Escape')closeReader();else if(e.key==='ArrowRight')readerStep(1);else if(e.key==='ArrowLeft')readerStep(-1)});
document.addEventListener('click',e=>{const a=e.target.closest('[data-go]');if(a){e.preventDefault();closeReader();show(a.dataset.go)}});

/* ---------- History ---------- */
async function loadHistory(){
  const d=await api(`/api/history?q=${encodeURIComponent($('#hq').value)}&tag=${encodeURIComponent($('#htag').value)}`);
  $('#hlist').innerHTML=d.runs.map(r=>`<li class="hitem"><div class="hmain"><div class="hq">${esc(r.prompt)}</div>
    <div class="hmeta"><time>${fmtTime(r.created_at)}</time>${r.tag?`<span class="chip">${esc(r.tag)}</span>`:''}
    ${r.nturns>1?`<span class="chip">${T('rounds',r.nturns)}</span>`:''}${(r.attachments||[]).length?`<span class="chip">📎 ${r.attachments.length}</span>`:''}${(r.last_winner||r.winner)?`<span class="chip win">${winText(r.last_winner||r.winner)}</span>`:''}<span>${esc(r.scores||T('h.unrated'))}</span></div></div>
    <div class="hact"><button class="btn sm" data-a="open" data-id="${esc(r.id)}">${T('h.open')}</button><button class="btn sm ghost" data-a="del" data-id="${esc(r.id)}">${T('h.del')}</button></div></li>`).join('')
    ||`<li class="hitem" style="color:var(--muted)">${T('h.none')}</li>`;
}
$('#hlist').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const id=b.dataset.id;
  if(b.dataset.a==='open')openRun(id).catch(toast);
  else if(confirm(T('h.confirm'))){await post('/api/delete/'+encodeURIComponent(id)).catch(toast);const r=ROUNDS.get(id);if(r){r.el.remove();ROUNDS.delete(id)}loadHistory()}};
let hqT;$('#hq').oninput=()=>{clearTimeout(hqT);hqT=setTimeout(loadHistory,250)};$('#htag').onchange=loadHistory;

/* ---------- Stats ---------- */
async function loadStats(){
  const d=await api(`/api/stats?tag=${encodeURIComponent($('#stag').value)}&by=${$('#sby').value}`);
  $('#stotal').textContent=T('s.total',d.total.n||0,d.total.judged||0);
  const pct=(a,b)=>b?Math.round(a/b*100)+'%':'—',by=$('#sby').value==='model';
  $('#stable').innerHTML='<tr>'+['s.tool','s.n','s.ok','s.avg','s.rated','s.best','s.win','s.secs','s.chars'].map(k=>`<th>${T(k)}</th>`).join('')+'</tr>'+
    (d.tools.map(r=>`<tr><td class="tool" style="--c:${esc(color(r.tool))}"><span class="dot"></span>${esc(tname(r.tool))}${by?`<small>${esc(modelLabel(r.tool,r.model))}${r.effort?'，effort '+esc(r.effort):''}</small>`:''}</td>
    <td>${r.n}</td><td>${pct(r.ok,r.n)}</td><td>${r.score!=null?r.score.toFixed(2)+`<span class="meter"><i style="width:${r.score/5*100}%"></i></span>`:'—'}</td>
    <td>${r.rated}</td><td>${r.wins||0}</td><td>${pct(r.wins||0,r.judged)}</td><td>${r.secs!=null?r.secs.toFixed(1)+'s':'—'}</td><td>${r.chars!=null?Math.round(r.chars):'—'}</td></tr>`).join('')
    ||`<tr><td colspan="9" style="color:var(--muted);text-align:left">${T('s.none')}</td></tr>`);
  renderJudgeStats(d);
}
$('#stag').onchange=loadStats;$('#sby').onchange=loadStats;
function renderJudgeStats(d){
  const box=$('#sjudge');if(!CFG.judge){box.innerHTML='';return}const name=esc(tname(CFG.judge));
  if(!(d.judge||[]).length){box.innerHTML=`<h3>${esc(T('sj.title',tname(CFG.judge)))}</h3><p class="note-s" style="margin:0">${esc(T('sj.empty',tname(CFG.judge)))}</p>`;return}
  const a=d.agree||{};
  box.innerHTML=`<h3>${esc(T('sj.title',tname(CFG.judge)))}<span class="note-s" style="font-weight:400;margin-left:10px">${esc(a.total?T('sj.agree',tname(CFG.judge),a.agree||0,a.total,Math.round((a.agree||0)/a.total*100)):T('sj.agreeEmpty'))}</span></h3>
  <table class="grid"><tr><th>${T('s.tool')}</th><th>${esc(T('sj.avg',tname(CFG.judge)))}</th><th>${T('sj.n')}</th><th>${esc(T('sj.best',tname(CFG.judge)))}</th></tr>${d.judge.map(r=>`<tr><td class="tool" style="--c:${esc(color(r.tool))}"><span class="dot"></span>${esc(tname(r.tool))}</td>
    <td>${r.avg!=null?r.avg.toFixed(2)+`<span class="meter"><i style="width:${r.avg/5*100}%"></i></span>`:'—'}</td><td>${r.n}</td><td>${r.best||0}</td></tr>`).join('')}</table>`;
}

/* ---------- Setup ---------- */
let jobT=0;
async function loadEnv(){
  $('#etable').innerHTML=`<tr><td style="color:var(--muted)">${T('e.checking')}</td></tr>`;
  let d;try{d=await api('/api/doctor')}catch(e){$('#etable').innerHTML=`<tr><td>${esc(e)}</td></tr>`;return}
  $('#envNode').textContent=(d.node?T('e.node',d.node):T('e.noNode'))+T('e.where');
  $('#etable').innerHTML='<tr>'+['e.tool','e.status','e.ver','e.path','e.act'].map(k=>`<th>${T(k)}</th>`).join('')+'</tr>'+d.tools.map(t=>{const job=d.jobs[t.name]||{};
    if(t.api)return `<tr data-openai-row${SHOW_OPENAI?'':' hidden'}><td class="tool" style="--c:${esc(color(t.name))}"><span class="dot"></span>${esc(tname(t.name))}</td>
      <td><span class="state ${t.ready?'ok':'bad'}">${T(t.ready?'e.ok':'lane.apiKey')}</span></td>
      <td>${esc(t.version)}</td><td class="path">${esc(t.path)}</td><td>${esc(T('lane.apiSetup'))}</td></tr>`;
    const hint=t.hint||'';
    const act=t.path?(t.login?`<button class="btn sm" data-a="login" data-t="${esc(t.name)}">${T('e.login')}</button><div class="hint">${esc(hint)}</div>`:'—')
      :(t.install?`<button class="btn sm primary" data-a="install" data-t="${esc(t.name)}" ${job.running?'disabled':''}>${job.running?T('e.installing'):T('e.install')}</button>`:'—');
    return `<tr><td class="tool" style="--c:${esc(color(t.name))}"><span class="dot"></span>${esc(tname(t.name))}${t.role==='judge'?`<small>${T('e.judge')}</small>`:''}</td>
      <td><span class="state ${t.path?'ok':'bad'}">${t.path?T('e.ok'):T('e.missing')}</span></td><td>${esc(t.version||'—')}</td>
      <td class="path">${esc(t.path||T('e.missingExe',t.exe))}</td><td>${act}</td></tr>`}).join('');
  $('#epath').textContent=d.path.split(':').join('\n');renderJobs(d.jobs);
}
function renderJobs(jobs){
  $('#ejobs').innerHTML=Object.entries(jobs).map(([t,j])=>`<h4>${esc(T('e.job',tname(t),j.running?T('e.running'):j.ok?T('e.done'):T('e.failed')))}</h4><pre class="joblog">${esc(j.log)}</pre>`).join('');
  $$('#ejobs .joblog').forEach(p=>p.scrollTop=p.scrollHeight);clearTimeout(jobT);
  if(Object.values(jobs).some(j=>j.running))jobT=setTimeout(async()=>{const j=await api('/api/jobs');Object.values(j).some(x=>x.running)?renderJobs(j):loadEnv()},1500);
}
$('#etable').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const t=b.dataset.t;
  try{if(b.dataset.a==='install'){b.disabled=true;b.textContent=T('e.installing');await post('/api/install/'+t);renderJobs(await api('/api/jobs'))}
  else{await post('/api/login/'+t);flash(b,T('e.opened'))}}catch(err){toast(err);loadEnv()}};
$('#recheck').onclick=loadEnv;

/* ---------- Navigation and startup ---------- */
function show(v){$$('nav button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
  for(const x of ['compare','history','stats','env'])$('#v-'+x).hidden=x!==v;
  if(v==='history')loadHistory();if(v==='stats')loadStats();if(v==='env')loadEnv()}
$$('nav button').forEach(b=>b.onclick=()=>show(b.dataset.v));
async function refreshTags(){try{CFG.tags=(await api('/api/config')).tags}catch(e){}
  $('#tagList').innerHTML=CFG.tags.map(t=>`<option value="${esc(t)}">`).join('');
  for(const id of ['#htag','#stag']){const v=$(id).value;$(id).innerHTML=`<option value="">${T('allTags')}</option>`+CFG.tags.map(t=>`<option>${esc(t)}</option>`).join('');$(id).value=v}}
setInterval(()=>api('/api/ping').catch(()=>{}),60000);
$('#quit').onclick=()=>{if(!confirm(T('quit.confirm')))return;
  post('/api/shutdown').catch(()=>{}).finally(()=>{for(const r of ROUNDS.values())r.es&&r.es.close();
    document.body.innerHTML=`<p style="padding:64px;font-size:15px;line-height:1.8">${T('quit.done')}</p>`})};
(async()=>{
  applyI18n();
  try{CFG=await api('/api/config')}catch(e){toast(e);return}
  const saved=ls.get('arenaSel',{});CUSTOM=ls.get('arenaCustom',{});
  for(const t of CFG.order)SEL[t]={on:CFG.tools[t].ready!==false&&(saved[t]?.on??CFG.tools[t].default_on??true),model:saved[t]?.model??'',effort:saved[t]?.effort??''};
  if(SEL.openai){
    const setting=$('#showOpenai');
    $('#openaiPreference').hidden=false;
    $('#openaiKeyNote').hidden=CFG.tools.openai.ready!==false;
    setting.checked=SHOW_OPENAI;
    if(!SHOW_OPENAI)SEL.openai.on=false;
    setting.onchange=()=>{
      SHOW_OPENAI=setting.checked;
      ls.set('arenaShowOpenai',SHOW_OPENAI);
      SEL.openai.on=false;
      ls.set('arenaSel',SEL);
      const row=$('[data-openai-row]');
      if(row)row.hidden=!SHOW_OPENAI;
      renderLanes();
    };
  }
  JSEL={model:'',auto:false,...ls.get('arenaJudge',{})};
  CHAT=ls.get('arenaChat',true);$('#chatMode').checked=CHAT;$('#chatOpt').title=T('chat.title');
  $('#chatMode').onchange=e=>{CHAT=e.target.checked;ls.set('arenaChat',CHAT)};
  modelsLater(0);ppInit();
  COMPOSER_ATT=makeAttacher($('#atts'),$('#attInput'),$('.composer'),$('#attBtn'));
  renderLanes();renderJudgeOpt();refreshTags();loadFeed().catch(toast);$('#prompt').focus();
})();
</script></body></html>
"""


def kill_all():
    for run in list(RUNS.values()):
        run.cancelled = True
        for p in run.procs.values():
            if p.poll() is None:
                kill(p)


def shutdown():
    time.sleep(0.3)
    kill_all()
    if SRV:
        SRV.shutdown()


def idle_watchdog():
    while True:
        time.sleep(30)
        busy = any(not r.finished_at for r in RUNS.values())
        if not busy and time.time() - LAST_SEEN > IDLE_SECS:
            print(f"Idle for {IDLE_SECS}s, exiting")
            shutdown()
            return


def main():
    global SRV
    with DB_LOCK:
        c = sqlite3.connect(DB_PATH)
        c.executescript(SCHEMA)
        migrate(c)
        c.close()
    load_cfg()
    cleanup_staging()
    threading.Thread(target=refresh_models, daemon=True).start()
    srv = SRV = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    if IDLE_EXIT:
        threading.Thread(target=idle_watchdog, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    print(f"AI Arena running at {url}\nConfig: {CFG_PATH}\nData: {DB_PATH}\nPATH: {PATH_CACHE}\nPress Ctrl+C to quit", flush=True)
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    kill_all()
    print("Stopped")


if __name__ == "__main__":
    main()
