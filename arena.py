#!/usr/bin/env python3
"""AI Arena — 本地对比台：同一问题并行发给 Claude / Codex / Gemini，三列流式对比，带历史与打分。

启动:  双击 AI Arena.app（推荐），或 python3 arena.py   # 打开 http://127.0.0.1:8765
       python3 arena.py --no-open --idle-exit           # 不开浏览器；15 分钟无人使用自动退出
环境变量: ARENA_PORT（端口）、ARENA_CONFIG（配置路径）、ARENA_DB（数据库路径）

首次运行会在脚本旁生成 arena_config.json，可修改各工具的命令、解析方式、颜色，
也可以加入新工具（如 ollama、aider），无需改代码。数据存于 arena.db（SQLite）。
界面在同目录的 arena.html（缺失时用脚本内置版本）。
仅依赖 Python 3.8+ 标准库。
"""
import codecs, copy, glob, json, os, platform, random, re, secrets, shutil, tempfile, shlex, signal, sqlite3, subprocess, sys, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
CFG_PATH = Path(os.environ.get("ARENA_CONFIG", HERE / "arena_config.json"))
DB_PATH = Path(os.environ.get("ARENA_DB", HERE / "arena.db"))
PORT = int(os.environ.get("ARENA_PORT", 8765))
TOKEN = secrets.token_urlsafe(24)  # 每次启动重新生成，防止其他网页调用本地接口
IDLE_EXIT = "--idle-exit" in sys.argv  # 由 macOS App 启动时开启：网页关闭一段时间后自动退出
IDLE_SECS = int(os.environ.get("ARENA_IDLE_SECS", 900))
LAST_SEEN = time.time()
SRV = None

DEFAULT_CFG = {
    "version": 3,
    "judge_timeout": 300,
    "timeout": 600,
    "tools": {
        "claude": {
            "cmd": ["claude", "-p", "{prompt}", "--output-format", "stream-json",
                    "--verbose", "--include-partial-messages"],
            "parser": "claude_stream", "color": "#d97757",
            "package": "@anthropic-ai/claude-code",
            "login": "claude", "hint": "首次运行按提示登录，完成后输入 /exit", "hint_en": "Follow the prompts to sign in, then type /exit",
            "resume_args": ["--resume", "{session}"],
            "model_flag": "--model", "default_label": "默认（账户默认）", "default_label_en": "Default (account default)",
            "effort_args": ["--effort", "{effort}"], "efforts": ["low", "medium", "high", "xhigh", "max"],
            "models": [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"},
                       {"id": "haiku", "label": "Haiku"}, {"id": "fable", "label": "Fable"}]},
        "codex": {"cmd": ["codex", "exec", "--json", "--skip-git-repo-check", "{prompt}"],
                  "parser": "codex_json", "color": "#10a37f",
                  "resume_args": ["resume", "{session}"], "resume_pos": "prompt",  # codex exec [选项] resume <ID> <问题>
                  "package": "@openai/codex",
                  "login": "codex login", "hint": "浏览器会打开，用 ChatGPT 账号登录", "hint_en": "A browser opens; sign in with your ChatGPT account",
                  "model_flag": "-m", "default_label": "默认（推荐模型）", "default_label_en": "Default (recommended)",
                  "effort_args": ["-c", "model_reasoning_effort=\"{effort}\""],
                  "efforts": ["minimal", "low", "medium", "high", "xhigh"],
                  "models": [{"id": "gpt-6.2-sol", "label": "GPT-6.2 Sol"}, {"id": "gpt-6.2-astra", "label": "GPT-6.2 Astra"},
                             {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"}, {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra"},
                             {"id": "gpt-6-luna", "label": "GPT-6 Luna"}]},
        "antigravity": {
            "label": "Antigravity", "cmd": ["agy", "-p", "{prompt}", "--output-format", "stream-json"],
            "parser": "agy_stream", "color": "#7b61ff",
            "resume_args": ["--conversation", "{session}"],
            "pty": True,  # agy -p 在非终端环境下可能卡住，用伪终端运行
            "install_script": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
            "login": "agy", "hint": "按提示用 Google 账号登录，完成后输入 /quit", "hint_en": "Sign in with your Google account, then type /quit",
            "model_flag": "--model", "default_label": "默认（自动选择）", "default_label_en": "Default (auto)",
            "effort_args": ["--effort", "{effort}"], "efforts": ["low", "medium", "high", "xhigh", "max"],
            "models_cmd": ["agy", "models"], "models": []},
        "grok": {"role": "judge",  # 评分模型，不参赛
                 "label": "Grok", "cmd": ["grok", "--no-auto-update", "-p", "{prompt}", "--output-format", "plain"],
                 "color": "#475569", "install_script": "curl -fsSL https://x.ai/cli/install.sh | bash",
                 "login": "grok login", "hint": "浏览器会打开，登录你的 xAI / X 账号", "hint_en": "A browser opens; sign in with your xAI / X account",
                 "model_flag": "-m", "default_label": "默认", "default_label_en": "Default",
                 "models": [{"id": "grok-4.7", "label": "Grok 4.7"}, {"id": "grok-4.5", "label": "Grok 4.5"}]},
        "gemini": {"enabled": False,  # 个人账户自 2026-06-18 起停止服务；有付费 Gemini API key 可改为 true
                   "cmd": ["gemini", "-p", "{prompt}"], "parser": "raw", "color": "#4285f4",
                   "package": "@google/gemini-cli",
                   "login": "gemini", "hint": "选 Login with Google 完成登录，然后输入 /quit", "hint_en": "Choose Login with Google, then type /quit",
                   "model_flag": "-m", "default_label": "默认（Auto 自动路由）", "default_label_en": "Default (Auto routing)",
                   "models": [{"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro"},
                              {"id": "gemini-3-pro-preview", "label": "Gemini 3 Pro"},
                              {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash"},
                              {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
                              {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"}]},
    },
}


# ---------------- 界面语言（前端用 X-Arena-Lang 头传入 zh / en） ----------------
MSG = {
    "detect_timeout": ("（检测超时）", "(check timed out)"),
    "node_use": ("使用已安装的 Node {ver}（{d}）\n", "Using installed Node {ver} ({d})\n"),
    "node_none": ("没有找到 Node 20+（系统里{old}），这些 CLI 需要 Node 20+。\n下载独立的 Node 22 到 {dir}（不影响系统里的 Node）…\n",
                  "No Node 20+ found (system has {old}); these CLIs need Node 20+.\nDownloading a private Node 22 to {dir} (your system Node is untouched)…\n"),
    "node_old_is": ("是 {v}", "{v}"), "node_old_none": ("没有 Node", "no Node"),
    "node_unsupported": ("当前系统不支持自动下载 Node，请手动安装 Node 20+", "Automatic Node download is not supported on this system. Install Node 20+ manually."),
    "node_index_fail": ("获取 Node 版本列表失败：{e}", "Failed to fetch the Node version list: {e}"),
    "node_dl": ("下载 {url}（约 50MB）…\n", "Downloading {url} (about 50 MB)…\n"),
    "node_dl_fail": ("下载 Node 失败：{e}", "Failed to download Node: {e}"),
    "node_ready": ("Node {ver} 就绪\n", "Node {ver} is ready\n"),
    "no_install": ("这个工具没有配置安装方式", "This tool has no install method configured"),
    "installing": ("已经在安装中", "Already installing"),
    "install_ok": ("\n✅ 安装完成，下一步点「登录」完成一次性登录。\n", "\n✅ Installed. Next, click Sign in to finish the one-time sign-in.\n"),
    "install_fail": ("\n安装失败，日志如上。\n", "\nInstall failed. See the log above.\n"),
    "no_login": ("这个工具没有配置登录命令", "This tool has no sign-in command configured"),
    "login_manual": ("请在终端运行：{cmd}", "Run this in Terminal: {cmd}"),
    "login_title": ("▶ {tool} 登录：{hint}", "▶ {tool} sign-in: {hint}"),
    "login_done": ("完成后可以关闭这个窗口，回到 AI Arena 点「重新检测」。", "When you are done, close this window and click Check again in AI Arena."),
    "tag_think": ("[思考] ", "[reasoning] "), "tag_cmd": ("[命令] ", "[command] "), "tag_err": ("[错误] ", "[error] "),
    "tag_usage": ("[用量] ", "[usage] "), "tag_step": ("[步骤] ", "[step] "), "tag_status": ("[状态] ", "[status] "),
    "agy_raw": ("[原始输出] 没能从 agy 的输出里解析出回答，原始内容如下：\n", "[raw output] Could not parse an answer from agy's output. Raw output:\n"),
    "resume_native": ("[续接] 沿用 {name} 自己的会话 {sid}…\n", "[resume] Continuing {name}'s own session {sid}…\n"),
    "resume_transcript": ("[续接] 没有拿到上一轮的会话 ID，改为把之前 {n} 轮问答附在问题前面\n",
                          "[resume] No session ID from the last round, so the previous {n} round(s) of Q&A were added before the question\n"),
    "cmd_missing": ("找不到命令: {exe}\n已搜索的 PATH 见「环境」页。可在那里一键安装，或把完整路径写进 {cfg}。\n",
                    "Command not found: {exe}\nSee the Setup page for the searched PATH. Install it there, or put the full path in {cfg}.\n"),
    "judge_busy": ("已经在评分中", "Already scoring"), "judge_none": ("没有配置评分模型", "No scoring model configured"),
    "judge_missing": ("没有找到 {exe}，请先在「环境」页安装 {label}", "{exe} not found. Install {label} on the Setup page first."),
    "run_missing": ("这一轮不存在", "This round does not exist"),
    "judge_two": ("至少要有两份成功的回答才能评分", "Scoring needs at least two successful answers"),
    "judge_exit": ("{label} 退出码 {code}", "{label} exited with code {code}"),
    "judge_parse": ("没能从 {label} 的回复里解析出评分", "Could not parse scores from {label}'s reply"),
    "judge_timeout": ("{label} 超过 {sec} 秒没有回复", "{label} did not reply within {sec} seconds"),
    "parent_missing": ("要追问的那一轮不存在", "The round you are following up on does not exist"),
    "bad_token": ("token 无效，请刷新页面", "Invalid token. Reload the page."),
    "no_judgment": ("还没有评分", "Not scored yet"), "empty_prompt": ("问题为空", "The question is empty"),
    "no_dir": ("目录不存在: {d}", "Folder does not exist: {d}"),
}


def M(lang, key, **kw):
    zh, en = MSG[key]
    return (en if lang == "en" else zh).format(**kw)


def load_cfg():
    if not CFG_PATH.exists():
        CFG_PATH.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    tools = cfg.setdefault("tools", {})
    if "antigravity" not in tools:  # 一次性迁移：Gemini CLI 个人版停服 → Antigravity CLI
        migrated = {}
        for name, spec in tools.items():
            if name == "gemini":
                migrated["antigravity"] = copy.deepcopy(DEFAULT_CFG["tools"]["antigravity"])
                spec["enabled"] = False
            migrated[name] = spec
        migrated.setdefault("antigravity", copy.deepcopy(DEFAULT_CFG["tools"]["antigravity"]))
        cfg["tools"] = tools = migrated
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if cfg.get("version", 1) < 2:  # v2：Codex / Antigravity 改用 JSON 输出，才能拿到会话 ID 以便追问
        old = {"codex": ["codex", "exec", "--skip-git-repo-check", "{prompt}"], "antigravity": ["agy", "-p", "{prompt}"]}
        for name, old_cmd in old.items():
            sp = tools.get(name)
            if sp and sp.get("cmd") == old_cmd:
                sp["cmd"] = list(DEFAULT_CFG["tools"][name]["cmd"])
                sp["parser"] = DEFAULT_CFG["tools"][name]["parser"]
        cfg["version"] = 2
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if cfg.get("version", 1) < 3:  # v3：加入评分模型 Grok
        tools.setdefault("grok", copy.deepcopy(DEFAULT_CFG["tools"]["grok"]))
        cfg.setdefault("judge_timeout", DEFAULT_CFG["judge_timeout"])
        cfg["version"] = 3
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, spec in tools.items():  # 旧配置补上新字段
        for k, v in DEFAULT_CFG["tools"].get(name, {}).items():
            spec.setdefault(k, copy.deepcopy(v))
    return cfg


def enabled_tools(cfg):  # 参赛工具（不含评分模型）
    return [n for n, sp in cfg["tools"].items() if sp.get("enabled", True) and sp.get("role") != "judge"]


def judge_tool(cfg):
    return next((n for n, sp in cfg["tools"].items() if sp.get("role") == "judge" and sp.get("enabled", True)), None)


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")


class PtyText:
    """把伪终端输出还原成纯文本：去 ANSI、处理 \r 覆盖（进度动画），按行输出。"""

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


MODEL_CACHE = {}  # 由 models_cmd（如 agy models）动态读取的模型列表
SLUG_RE = re.compile(r"\b([a-z][a-z0-9]*(?:[.-][a-z0-9]+)+)\b")


def refresh_models():
    cfg = load_cfg()
    for name in enabled_tools(cfg):
        spec = cfg["tools"][name]
        mc = spec.get("models_cmd")
        exe = mc and which(mc[0])
        if not exe:
            continue
        try:
            r = subprocess.run([exe] + mc[1:], capture_output=True, text=True, timeout=20,
                               env=child_env(), stdin=subprocess.DEVNULL)
        except Exception:
            continue
        models = []
        for ln in ANSI_RE.sub("", r.stdout).splitlines():
            m = SLUG_RE.search(ln)
            if not m or "-" not in m.group(1):
                continue
            rest = (ln[:m.start()] + ln[m.end():]).strip(" \t-*•:|")
            if rest.startswith("(") and rest.endswith(")"):
                rest = rest[1:-1].strip()
            models.append({"id": m.group(1), "label": rest or m.group(1)})
        if models:
            MODEL_CACHE[name] = list({x["id"]: x for x in models}.values())


# ---------------- 命令查找（Finder 启动时 PATH 很短，需要主动搜常见安装位置） ----------------
def _ver_key(p):
    return [int(x) for x in re.findall(r"\d+", p)[:3]]


ARENA_HOME = Path.home() / ".ai-arena"     # 一键安装的位置，无需管理员权限
NODE_DIR = ARENA_HOME / "node"             # 独立 Node（系统 Node < 20 或没有时自动下载）
NPM_PREFIX = ARENA_HOME / "npm-global"     # CLI 安装目录
NODE_PIN = ARENA_HOME / "node-dir"        # 记录选中的 Node 20+ 所在目录，运行 CLI 时放在 PATH 最前
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
        dirs += sorted(glob.glob(pat), key=_ver_key, reverse=True)  # 新版本优先
    joined = os.pathsep.join(dict.fromkeys(d for d in dirs if d))
    npm = shutil.which("npm", path=joined)
    if npm:  # npm 全局安装目录
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
    env.pop("CLAUDECODE", None)  # 允许在 Claude Code 终端里启动
    env.update(NO_COLOR="1", FORCE_COLOR="0", PATH=PATH_CACHE)
    return env


def which(exe):
    return shutil.which(exe, path=PATH_CACHE)


JOBS = {}  # 安装任务: tool -> {"running", "ok", "log"}


def doctor(lang="zh"):
    global PATH_CACHE
    PATH_CACHE = search_path()
    cfg, res = load_cfg(), {}

    def check(name, spec):
        exe = spec["cmd"][0]
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
                     "login": spec.get("login") or "", "hint": spec.get("hint") or "",
                     "hint_en": spec.get("hint_en") or spec.get("hint") or ""}

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


def ensure_node(log, lang="zh"):
    """返回要用的 npm：优先已下载的独立 Node；系统 Node >= 20 就用系统的；否则下载 Node 22。"""
    if (NODE_DIR / "bin" / "npm").exists():
        return str(NODE_DIR / "bin" / "npm")
    first_old = ""
    for d in search_path().split(os.pathsep):  # 按 PATH 顺序找第一个 Node 20+（如 nvm 装的）
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


def start_install(tool, lang="zh"):
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
            if script:  # 官方安装脚本（如 Antigravity CLI，装到 ~/.local/bin）
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
            env["PATH"] = str(Path(npm).parent) + os.pathsep + PATH_CACHE  # 让 npm 用对应的 node
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


def open_login(tool, lang="zh"):
    spec = load_cfg()["tools"].get(tool) or {}
    cmd = spec.get("login")
    if not cmd:
        return M(lang, "no_login")
    if sys.platform != "darwin":
        return M(lang, "login_manual", cmd=cmd)
    script = Path(tempfile.gettempdir()) / f"arena-login-{tool}.command"
    hint = (spec.get("hint_en") if lang == "en" else None) or spec.get("hint", "")
    title, done = M(lang, "login_title", tool=tool, hint=hint), M(lang, "login_done")
    script.write_text(f"#!/bin/bash\nexport PATH={shlex.quote(PATH_CACHE)}\necho {shlex.quote(title)}\n"
                      f"echo\n{cmd}\necho\necho {shlex.quote(done)}\n", encoding="utf-8")
    script.chmod(0o755)
    subprocess.Popen(["/usr/bin/open", str(script)])  # .command 用「终端」打开，无需额外授权
    return ""


# ---------------- 数据库 ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at REAL, prompt TEXT, cwd TEXT,
  tag TEXT, tools TEXT, winner TEXT, models TEXT DEFAULT '', efforts TEXT DEFAULT '',
  thread TEXT DEFAULT '', parent TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS outputs(run_id TEXT, tool TEXT, status TEXT, seconds REAL, output TEXT,
  log TEXT, cmd TEXT, model TEXT DEFAULT '', actual_model TEXT DEFAULT '', effort TEXT DEFAULT '',
  session TEXT DEFAULT '', PRIMARY KEY(run_id, tool));
CREATE TABLE IF NOT EXISTS ratings(run_id TEXT, tool TEXT, score INTEGER, note TEXT,
  PRIMARY KEY(run_id, tool));
CREATE TABLE IF NOT EXISTS judgments(run_id TEXT PRIMARY KEY, judge TEXT, model TEXT, status TEXT, best TEXT,
  reason TEXT, mapping TEXT, result TEXT, raw TEXT, seconds REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS judge_scores(run_id TEXT, tool TEXT, overall REAL, detail TEXT, comment TEXT,
  PRIMARY KEY(run_id, tool));
"""
DB_LOCK = threading.Lock()


def migrate(c):  # 旧数据库补列
    for table, col in (("runs", "models"), ("outputs", "model"), ("outputs", "actual_model"),
                       ("runs", "efforts"), ("outputs", "effort"), ("runs", "thread"), ("runs", "parent"),
                       ("outputs", "session")):
        if col not in [r[1] for r in c.execute(f"PRAGMA table_info({table})")]:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT ''")
    c.commit()


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


# ---------------- 运行管理 ----------------
class Run:
    def __init__(self, rid, tools):
        self.id, self.events, self.cond = rid, [], threading.Condition()
        self.pending, self.procs = set(tools), {}
        self.cancelled, self.finished_at = False, None
        self.judge, self.judge_model, self.lang = False, "", "zh"

    def emit(self, ev):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()


RUNS = {}


class ClaudeStream:
    """解析 `claude -p --output-format stream-json` 的 JSONL；非 JSON 行原样输出。"""

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
    def __init__(self, lang="zh"):
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
    """解析 `codex exec --json` 的 JSONL（thread.started / item.* / turn.*）。"""

    def __init__(self, lang="zh"):
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
    """在嵌套的 dict / list 里找第一个非空字符串字段。"""
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
    """解析 `agy -p … --output-format stream-json` 的 NDJSON（init / step_update / result）。
    字段位置按文档优先，找不到时在整条事件里递归查找；仍没解析出文字时，把原始输出写进过程日志。"""

    def __init__(self, lang="zh"):
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


PARSERS = {"claude_stream": lambda lang="zh": ClaudeStream(), "codex_json": CodexJson, "agy_stream": AgyStream}


def build_transcript(history, prompt, lang="zh"):
    en = lang == "en"
    parts = ["Below is our conversation so far. Answer the new question at the end with it in mind.\n" if en
             else "下面是我们之前的对话记录，请在此基础上回答最后的新问题。\n"]
    for i, (q_, a_) in enumerate(history, 1):
        a_ = (a_ or ("(no answer)" if en else "（没有回答）")).strip()
        if len(a_) > 6000:
            a_ = a_[:6000] + ("\n…(truncated)" if en else "\n……（后面省略）")
        parts.append(f"[Round {i}: my question]\n{q_}\n\n[Round {i}: your answer]\n{a_}\n" if en
                     else f"【第 {i} 轮 我的问题】\n{q_}\n\n【第 {i} 轮 你的回答】\n{a_}\n")
    parts.append(f"[New question]\n{prompt}" if en else f"【新问题】\n{prompt}")
    return "\n".join(parts)


def kill(p):
    try:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGTERM)
        else:
            p.terminate()
    except Exception:
        pass


MODEL_RE = re.compile(r"(?m)^\s*model:\s*([\w.\-:/@]+)")  # 如 Codex 在 stderr 打印的 "model: gpt-…"


def exec_tool(run, name, spec, prompt, cwd, timeout, model="", effort="", session="", history=None):
    base, extra, note = list(spec["cmd"]), [], ""
    if model and spec.get("model_flag"):
        extra += [spec["model_flag"], model]
    if effort and spec.get("effort_args"):
        extra += [a.replace("{effort}", effort) for a in spec["effort_args"]]
    rargs = [a.replace("{session}", session) for a in spec.get("resume_args") or []] if session else []
    if rargs and spec.get("resume_pos") != "prompt":
        extra += rargs
    if extra:
        i = base.index("{prompt}") if "{prompt}" in base else len(base)
        if i > 0 and base[i - 1].startswith("-"):  # 如 agy -p {prompt}：插到 -p 前面，避免拆开 "-p 问题"
            i -= 1
        base[i:i] = extra
    if rargs and spec.get("resume_pos") == "prompt":  # 子命令形式，紧挨在问题前面
        i = base.index("{prompt}") if "{prompt}" in base else len(base)
        base[i:i] = rargs
    lang = getattr(run, "lang", "zh")
    if rargs:
        note = M(lang, "resume_native", name=name, sid=session[:12])
    elif history:
        prompt = build_transcript(history, prompt, lang)
        note = M(lang, "resume_transcript", n=len(history))
    cmd = [prompt if a == "{prompt}" else a.replace("{prompt}", prompt) for a in base]
    shown = " ".join(shlex.quote(a) for a in base)
    out, log, t0, status = [], [], time.time(), "ok"
    actual, errhead, sess = [""], [""], [""]
    run.emit({"tool": name, "type": "start", "cmd": shown, "model": model, "effort": effort})
    if note:
        log.append(note)
        run.emit({"tool": name, "type": "log", "data": note})
    env = child_env()
    exe = which(cmd[0])
    use_pty = bool(spec.get("pty")) and os.name == "posix"
    out_fd = None
    try:
        if not exe:
            raise FileNotFoundError(cmd[0])
        if use_pty:
            import pty, termios, fcntl, struct
            out_fd, slave = pty.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[1] &= ~termios.OPOST  # 不把 \n 改成 \r\n
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 200, 400, 0, 0))  # 宽一些，避免折行
            env["TERM"] = "dumb"
            try:
                p = subprocess.Popen([exe] + cmd[1:], stdout=slave, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                     cwd=cwd, env=env, start_new_session=True)
            finally:
                os.close(slave)
        else:
            p = subprocess.Popen([exe] + cmd[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=(os.name == "posix"))
    except FileNotFoundError:
        p, status = None, "未安装"
        log.append(M(lang, "cmd_missing", exe=cmd[0], cfg=CFG_PATH.name))
    except Exception as e:
        p, status = None, "启动失败"
        log.append(f"{e}\n")
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
                except OSError:  # 伪终端在子进程退出后返回 EIO
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
            status = "超时"
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
            status = "已取消"
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
            if run.judge and not run.cancelled:  # 自动评分
                threading.Thread(target=start_judge, args=(run.id, run.judge_model, run.lang), daemon=True).start()
        run.cond.notify_all()


# ---------------- 评分（Grok 等第三方模型匿名评审） ----------------
JUDGING = set()
JUDGE_KEYS = ("accuracy", "completeness", "clarity", "usefulness")


def build_judge_prompt(question, earlier, answers, lang="zh"):
    if lang == "en":
        return build_judge_prompt_en(question, earlier, answers)
    lines = ["你是一位严格、公正的评审。下面是同一个问题的几份匿名回答，标为 A、B、C……，顺序是随机打乱的。",
             "请只根据内容评审，不要猜测或考虑它们来自哪个模型。不要使用任何工具，不要读写文件。", ""]
    if earlier:
        lines.append("这是多轮对话中的一轮。在此之前，提问者依次问过：")
        lines += [f"{i}. {x}" for i, x in enumerate(earlier, 1)]
        lines += ["（每位回答者都能看到自己之前的对话。）", ""]
    lines += ["【问题】", question, ""]
    for lab, text in answers:
        t = text.strip()
        if len(t) > 12000:
            t = t[:12000] + "\n……（后面省略）"
        lines += [f"【回答 {lab}】", t, ""]
    lines += ["评分标准（每项 1 到 5 分，5 最好）：",
              "- accuracy 准确性：事实是否正确，有没有错误或编造",
              "- completeness 完整性：是否覆盖问题的关键方面",
              "- clarity 清晰度：结构和表达是否清楚易读",
              "- usefulness 实用性：对提问者是否真正有帮助",
              "- overall 总分 1 到 5：综合判断，不是简单平均",
              "",
              "只输出一个 JSON 对象，不要输出任何其他文字。格式：",
              '{"scores":{"A":{"accuracy":4,"completeness":4,"clarity":5,"usefulness":4,"overall":4,"comment":"一句中文点评"}},'
              '"best":"A","reason":"一两句中文，说明为什么选它"}',
              'scores 里要包含每一份回答。如果难分高下，best 填 "tie"。']
    return "\n".join(lines)


def build_judge_prompt_en(question, earlier, answers):
    lines = ["You are a strict, fair reviewer. Below are several anonymous answers to the same question, labeled "
             "Answer A, Answer B, Answer C…, in random order.",
             "Judge on content only. Do not guess or consider which model wrote each one. Do not use any tools or read or write files.", ""]
    if earlier:
        lines.append("This is one round of a multi-turn conversation. Before this, the user asked, in order:")
        lines += [f"{i}. {x}" for i, x in enumerate(earlier, 1)]
        lines += ["(Each answerer can see its own earlier conversation.)", ""]
    lines += ["[Question]", question, ""]
    for lab, text in answers:
        t = text.strip()
        if len(t) > 12000:
            t = t[:12000] + "\n…(truncated)"
        lines += [f"[Answer {lab}]", t, ""]
    lines += ["Scoring criteria (1 to 5 each, 5 is best):",
              "- accuracy: are the facts correct, with no errors or fabrication",
              "- completeness: does it cover the key aspects of the question",
              "- clarity: is it well structured and easy to read",
              "- usefulness: does it genuinely help the person who asked",
              "- overall 1 to 5: your overall judgment, not a simple average",
              "",
              "Output only one JSON object and no other text. Format:",
              '{"scores":{"A":{"accuracy":4,"completeness":4,"clarity":5,"usefulness":4,"overall":4,"comment":"one-sentence comment in English"}},'
              '"best":"A","reason":"one or two sentences in English on why it is best"}',
              'Include every answer in scores. Refer to answers as "Answer A", "Answer B" and so on. If they are too close to call, set best to "tie".']
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


def judgment(rid):
    j = q("SELECT * FROM judgments WHERE run_id=?", (rid,), one=True)
    if j:
        j["mapping"] = json.loads(j.get("mapping") or "{}")
        j["result"] = json.loads(j.get("result") or "{}")
        j["scores"] = q("SELECT tool, overall, detail, comment FROM judge_scores WHERE run_id=?", (rid,))
        for x in j["scores"]:
            x["detail"] = json.loads(x["detail"] or "{}")
        j["running"] = rid in JUDGING
    return j


def start_judge(rid, model="", lang="zh"):
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
    random.shuffle(outs)  # 打乱顺序并匿名，避免位置和品牌偏差
    labels = "ABCDEFGH"
    mapping = {labels[i]: o["tool"] for i, o in enumerate(outs)}
    prompt = build_judge_prompt(run["prompt"], earlier, [(labels[i], o["output"]) for i, o in enumerate(outs)], lang)
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
        try:
            r = subprocess.run([exe] + cmd[1:], capture_output=True, text=True, timeout=cfg.get("judge_timeout", 300),
                               env=child_env(), stdin=subprocess.DEVNULL, cwd=tempfile.mkdtemp(prefix="arena-judge-"))
            raw = (r.stdout or "") + ("\n[stderr]\n" + r.stderr if r.stderr.strip() else "")
            o = parse_judge(r.stdout) if r.returncode == 0 else None
            if not o:
                reason = M(lang, "judge_exit", label=label, code=r.returncode) if r.returncode else M(lang, "judge_parse", label=label)
            else:
                rows = []
                for lab, tool in mapping.items():
                    sc = o["scores"].get(lab) or o["scores"].get(f"回答 {lab}") or o["scores"].get(f"Answer {lab}") or {}
                    try:
                        overall = float(sc.get("overall"))
                    except (TypeError, ValueError):
                        overall = None
                    detail = {k: sc.get(k) for k in JUDGE_KEYS if sc.get(k) is not None}
                    rows.append((rid, tool, overall, json.dumps(detail), str(sc.get("comment") or "")))
                b = str(o.get("best") or "").strip().upper()
                best = "tie" if b in ("TIE", "平手") else mapping.get(b.replace("回答", "").replace("ANSWER", "").strip(), "")
                reason, result, status = str(o.get("reason") or ""), o, "ok"
                for row in rows:
                    q("INSERT OR REPLACE INTO judge_scores(run_id, tool, overall, detail, comment) VALUES(?,?,?,?,?)", row)
        except subprocess.TimeoutExpired:
            reason = M(lang, "judge_timeout", label=label, sec=cfg.get('judge_timeout', 300))
        except Exception as e:
            reason = str(e)
        q("UPDATE judgments SET status=?, best=?, reason=?, result=?, raw=?, seconds=? WHERE run_id=?",
          (status, best, reason, json.dumps(result, ensure_ascii=False), raw[-20000:], round(time.time() - t0, 1), rid))
        JUDGING.discard(rid)

    threading.Thread(target=work, daemon=True).start()
    return ""


def start_run(prompt, tools, cwd, tag, models=None, efforts=None, parent=None, judge=False, judge_model="", lang="zh"):
    cfg = load_cfg()
    on = enabled_tools(cfg)
    thread, parent_id, sessions, histories = "", "", {}, {}
    if parent:  # 追问：沿用上一轮的工具、模型、effort、目录，并找各工具的会话 ID
        p = q("SELECT * FROM runs WHERE id=?", (parent,), one=True)
        if not p:
            raise ValueError(M(lang, "parent_missing"))
        thread, parent_id = p.get("thread") or p["id"], p["id"]
        q("UPDATE runs SET thread=? WHERE id=? AND (thread IS NULL OR thread='')", (thread, thread))
        tools = json.loads(p["tools"] or "[]")
        models, efforts = json.loads(p.get("models") or "{}"), json.loads(p.get("efforts") or "{}")
        cwd, tag = p.get("cwd") or None, p.get("tag") or ""
        for t in tools:
            rows = q("""SELECT r.prompt, o.output, o.session, o.status FROM runs r
                        LEFT JOIN outputs o ON o.run_id=r.id AND o.tool=?
                        WHERE r.id=? OR r.thread=? ORDER BY r.created_at""", (t, thread, thread))
            histories[t] = [(x["prompt"], x["output"] or "") for x in rows]
            ok = [x["session"] for x in rows if x["session"] and x["status"] == "ok"]
            sessions[t] = ok[-1] if ok else ""
    tools = [t for t in tools if t in on] or on
    for rid0 in [k for k, r in RUNS.items() if r.finished_at and time.time() - r.finished_at > 1800]:
        RUNS.pop(rid0, None)  # 完成 30 分钟后从内存移除，之后从数据库回放
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    models = {t: str((models or {}).get(t) or "").strip() for t in tools}
    efforts = {t: e if re.fullmatch(r"[a-z]{1,12}", e := str((efforts or {}).get(t) or "").strip()) else ""
               for t in tools}
    q("INSERT INTO runs(id, created_at, prompt, cwd, tag, tools, winner, models, efforts, thread, parent) "
      "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rid, time.time(), prompt, cwd or "", tag, json.dumps(tools), "",
                                        json.dumps(models), json.dumps(efforts), thread or rid, parent_id))
    run = Run(rid, tools)
    run.judge, run.judge_model, run.lang = bool(judge), str(judge_model or ""), lang
    RUNS[rid] = run
    for t in tools:
        threading.Thread(target=exec_tool, args=(run, t, cfg["tools"][t], prompt, cwd, cfg.get("timeout", 600),
                                                 models[t], efforts[t], sessions.get(t, ""),
                                                 None if sessions.get(t) else histories.get(t)), daemon=True).start()
    return rid, tools, models, efforts


def run_detail(rid, with_turns=True):
    r = q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    if not r:
        return None
    r["tools"] = json.loads(r["tools"] or "[]")
    r["models"] = json.loads(r.get("models") or "{}")
    r["efforts"] = json.loads(r.get("efforts") or "{}")
    r["outputs"] = q("SELECT * FROM outputs WHERE run_id=?", (rid,))
    r["ratings"] = q("SELECT tool, score, note FROM ratings WHERE run_id=?", (rid,))
    live = RUNS.get(rid)
    r["running"] = bool(live and not live.finished_at)
    r["judgment"] = judgment(rid)
    r["auto_judge"] = bool(live and live.judge)
    if with_turns:
        root = r.get("thread") or rid
        ids = [x["id"] for x in q("SELECT id FROM runs WHERE id=? OR thread=? ORDER BY created_at", (root, root))]
        r["turns"] = [run_detail(i, False) if i != rid else {k: v for k, v in r.items() if k != "turns"} for i in ids]
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
        self.lang = "en" if (self.headers.get("X-Arena-Lang") or "").lower().startswith("en") else "zh"
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self._json({"error": "bad host"}, 403)  # 防 DNS rebinding
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
            page_file = HERE / "arena.html"  # 同目录有 arena.html 就用它，方便单独改界面
            src = page_file.read_text(encoding="utf-8") if page_file.exists() else PAGE
            body = src.replace("__TOKEN__", TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p.startswith("/api/judge/"):
            j = judgment(p.rsplit("/", 1)[1])
            self._json(j) if j else self._json({"error": M(self.lang, "no_judgment")}, 404)
        elif p == "/api/doctor":
            self._json(doctor(self.lang))
        elif p == "/api/jobs":
            self._json(JOBS)
        elif p == "/api/ping":
            self._json({"ok": True})
        elif p == "/api/config":
            cfg = load_cfg()
            tags = [r["tag"] for r in q("SELECT DISTINCT tag FROM runs WHERE tag<>'' ORDER BY tag")]
            self._json({"tools": {k: {"color": v.get("color", "#888"), "label": v.get("label") or k.capitalize(),
                                      "models": MODEL_CACHE.get(k) or v.get("models", []),
                                      "flag": bool(v.get("model_flag")), "default_label": v.get("default_label", "默认"),
                                      "default_label_en": v.get("default_label_en") or "Default",
                                      "efforts": v.get("efforts", []) if v.get("effort_args") else []}
                                  for k, v in cfg["tools"].items()},
                        "order": enabled_tools(cfg), "judge": judge_tool(cfg), "tags": tags})
        elif p.startswith("/api/stream/"):
            self._sse(p.rsplit("/", 1)[1])
        elif p.startswith("/api/run/"):
            d = run_detail(p.rsplit("/", 1)[1])
            self._json(d) if d else self._json({"error": "not found"}, 404)
        elif p == "/api/history":
            kw, tag = qs.get("q", ""), qs.get("tag", "")
            limit = max(1, min(int(qs.get("limit") or 200), 500))
            rows = q("""SELECT runs.id, created_at, prompt, tag, winner, tools, models, efforts, cwd,
                 (SELECT group_concat(tool||' '||score, '，') FROM ratings r WHERE r.run_id=runs.id AND score>0) scores,
                 (SELECT COUNT(*) FROM runs c WHERE c.thread=runs.id) nturns,
                 (SELECT c.winner FROM runs c WHERE c.id=runs.id OR c.thread=runs.id ORDER BY c.created_at DESC LIMIT 1) last_winner,
                 (SELECT group_concat(tool||':'||status, '|') FROM outputs o WHERE o.run_id=
                    (SELECT c.id FROM runs c WHERE c.id=runs.id OR c.thread=runs.id ORDER BY c.created_at DESC LIMIT 1)) st
                 FROM runs WHERE (thread IS NULL OR thread='' OR thread=runs.id)
                   AND (prompt LIKE ? OR EXISTS(SELECT 1 FROM runs c WHERE c.thread=runs.id AND c.prompt LIKE ?))
                   AND (?='' OR tag=?) ORDER BY created_at DESC LIMIT ?""",
                     (f"%{kw}%", f"%{kw}%", tag, tag, limit))
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
        try:
            b = self._body()
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        if p == "/api/run":
            prompt = (b.get("prompt") or "").strip()
            if not prompt:
                return self._json({"error": M(self.lang, "empty_prompt")}, 400)
            jflag, jmodel = bool(b.get("judge")), str(b.get("judge_model") or "")
            if b.get("parent"):
                try:
                    rid, tools, models, efforts = start_run(prompt, [], None, "", parent=str(b["parent"]),
                                                            judge=jflag, judge_model=jmodel, lang=self.lang)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"id": rid, "tools": tools, "models": models, "efforts": efforts, "parent": b["parent"]})
            cwd = os.path.expanduser(b.get("cwd") or "").strip() or None
            if cwd and not os.path.isdir(cwd):
                return self._json({"error": M(self.lang, "no_dir", d=cwd)}, 400)
            models = b.get("models") if isinstance(b.get("models"), dict) else {}
            efforts = b.get("efforts") if isinstance(b.get("efforts"), dict) else {}
            rid, tools, models, efforts = start_run(prompt, b.get("tools") or [], cwd, (b.get("tag") or "").strip(),
                                                    models, efforts, judge=jflag, judge_model=jmodel, lang=self.lang)
            self._json({"id": rid, "tools": tools, "models": models, "efforts": efforts, "tag": b.get("tag") or ""})
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
        elif p.startswith("/api/delete/"):  # 删除首问时连同整条对话一起删除
            rid = p.rsplit("/", 1)[1]
            for x in q("SELECT id FROM runs WHERE id=? OR thread=?", (rid, rid)):
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
            if not run:  # 已不在内存：从数据库回放
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


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Arena</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22%3E%3Cdefs%3E%3ClinearGradient id=%22ag%22 x1=%220%22 y1=%220%22 x2=%221%22 y2=%221%22%3E%3Cstop offset=%220%22 stop-color=%22%234C8DFF%22/%3E%3Cstop offset=%22.55%22 stop-color=%22%236D6AF7%22/%3E%3Cstop offset=%221%22 stop-color=%22%23A47CF2%22/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width=%2232%22 height=%2232%22 rx=%229%22 fill=%22url%28%23ag%29%22/%3E%3Crect x=%228%22 y=%2210%22 width=%224%22 height=%2214%22 rx=%222%22 fill=%22%23fff%22/%3E%3Crect x=%2214%22 y=%2215%22 width=%224%22 height=%229%22 rx=%222%22 fill=%22%23fff%22 fill-opacity=%22.88%22/%3E%3Crect x=%2220%22 y=%2212.5%22 width=%224%22 height=%2211.5%22 rx=%222%22 fill=%22%23fff%22 fill-opacity=%22.76%22/%3E%3Ccircle cx=%2224.6%22 cy=%227.4%22 r=%221.7%22 fill=%22%23fff%22/%3E%3C/svg%3E">
<!-- 内嵌 marked 12.0.2（MIT）与 DOMPurify 3.1.6（Apache-2.0/MPL-2.0），离线也能排版 -->
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

/* 顶栏 */
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

/* 通用按钮 */
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

/* 输入区 */
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

/* 对比流 */
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
.rfoot{display:flex;gap:8px;align-items:center;margin-top:12px;color:var(--muted);font-size:12.5px}
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
.fhint{margin:6px 4px 0;color:var(--muted);font-size:12px}
.judgeopt{display:flex;align-items:center;gap:6px;padding:2px 4px 2px 10px;border-radius:9px;background:color-mix(in srgb,var(--soft) 85%,transparent)}
.judgeopt select{border:0;background:transparent;color:var(--muted);padding:3px 4px;border-radius:6px;font-size:12.5px;cursor:pointer}
.judgeopt label{display:flex;align-items:center;gap:4px;cursor:pointer;color:var(--ink)}
.jd{padding:7px 12px 8px;font-size:12.5px;line-height:1.6;color:var(--muted);border-top:1px dashed var(--line);background:color-mix(in srgb,var(--soft) 55%,transparent)}
.jd b{color:var(--ink);margin-right:6px}
.jd .jx{font-size:11.5px;margin-left:6px;opacity:.85}
.jres{font-size:12.5px;color:var(--muted);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
.jres b{color:var(--ink)}
.jres.err{color:var(--bad)}
.jres .agree{color:var(--ok);margin-left:6px}
#sjudge{margin-top:22px}
#sjudge h3{font-size:14px;margin:0 0 10px}

/* 单列 */
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

/* 回答排版 */
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

/* 阅读模式 */
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

/* 其他页面 */
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
    <button data-v="compare" class="on" data-i18n="nav.compare">对比</button><button data-v="history" data-i18n="nav.history">历史</button>
    <button data-v="stats" data-i18n="nav.stats">统计</button><button data-v="env" data-i18n="nav.env">环境</button>
  </nav>
  <span class="sp"></span><span id="conn" hidden data-i18n="conn">后台服务未连接，请重新打开 AI Arena</span>
  <div class="langsw" role="group" aria-label="Language / 语言"><button data-l="zh" lang="zh-CN">中文</button><button data-l="en" lang="en">EN</button></div>
  <button class="btn sm ghost" id="quit" title="关闭后台服务" data-i18n="quit" data-i18n-title="quit.title">退出</button>
</div></header>

<main class="wrap">
<section id="v-compare">
  <div class="composer">
    <div class="cin">
      <textarea id="prompt" rows="1" placeholder="问一个问题，选中的模型会同时作答" data-i18n-ph="prompt.ph"></textarea>
      <button class="btn primary" id="go" data-i18n="send">发送</button>
    </div>
    <div class="lanes" id="lanes"></div>
    <div class="copts">
      <input class="field" id="tag" placeholder="标签，如 写作" list="tagList" aria-label="标签" data-i18n-ph="tag.ph" data-i18n-aria="tag"><datalist id="tagList"></datalist>
      <input class="field" id="cwd" placeholder="工作目录（可选）" aria-label="工作目录" data-i18n-ph="cwd.ph" data-i18n-aria="cwd">
      <span class="judgeopt" id="judgeOpt" hidden></span>
      <span class="sp"></span><span><kbd>⌘</kbd> <kbd>Enter</kbd> <span data-i18n="toSend">发送</span></span>
    </div>
  </div>
  <div class="feedhead"><h2 data-i18n="recent">最近的对比</h2><span class="sp"></span><button class="btn sm ghost" id="collapseAll" data-i18n="collapseAll">全部折叠</button></div>
  <div id="feed"></div>
  <p id="empty" hidden data-i18n="empty">还没有对比记录。在上面输入一个问题，选中的模型会同时作答。</p>
</section>

<section id="v-history" class="page" hidden>
  <div class="bar"><input class="field" id="hq" placeholder="搜索问题" style="flex:1" data-i18n-ph="hq.ph" data-i18n-aria="hq.ph"><select class="field" id="htag"></select></div>
  <ul class="list" id="hlist"></ul>
</section>

<section id="v-stats" class="page" hidden>
  <div class="bar"><select class="field" id="stag"></select>
    <select class="field" id="sby"><option value="" data-i18n="sby.tool">按工具</option><option value="model" data-i18n="sby.model">按工具、模型和 effort</option></select>
    <span id="stotal" class="note-s" style="margin:0"></span></div>
  <table class="grid" id="stable"></table>
  <div id="sjudge"></div>
  <p class="note-s" data-i18n="stats.note">胜率是获评「最佳」的次数除以该工具参加并已评判的轮数。平均分只算打过分的回答，平均耗时和字数只算成功的回答。</p>
</section>

<section id="v-env" class="page" hidden>
  <div class="bar"><button class="btn" id="recheck" data-i18n="recheck">重新检测</button><span id="envNode" class="note-s" style="margin:0"></span></div>
  <table class="grid env" id="etable"></table>
  <div id="ejobs"></div>
  <details class="note-s"><summary data-i18n="env.path">搜索过的 PATH</summary><pre id="epath" class="joblog"></pre></details>
  <p class="note-s" data-i18n="env.note">没找到的工具：一键安装，再点「登录」在弹出的终端里登录一次，最后点「重新检测」。</p>
</section>
</main>

<div id="reader" hidden role="dialog" aria-modal="true" aria-label="阅读模式" data-i18n-aria="reader">
  <div class="rp">
    <div class="rph"><div id="rtabs" style="display:flex;gap:2px;flex-wrap:wrap"></div><span class="sp"></span>
      <span class="rhint" data-i18n="reader.hint">← → 切换  Esc 关闭</span>
      <button class="ib" id="rraw" title="原文 / 排版" data-i18n-title="raw"></button><button class="ib" id="rcopy" title="复制回答" data-i18n-title="copyAns"></button><button class="ib" id="rclose" title="关闭" data-i18n-title="close"></button></div>
    <div id="rq"></div>
    <div id="rdoc" tabindex="0"></div>
  </div>
</div>
<div id="toast" hidden role="status"></div>

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
  code:'<path d="M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5"/>',close:'<path d="M4 4l8 8M12 4l-8 8"/>'};
/* ---------- 语言 ---------- */
const I18N={zh:{
 'nav.compare':'对比','nav.history':'历史','nav.stats':'统计','nav.env':'环境','conn':'后台服务未连接，请重新打开 AI Arena',
 'quit':'退出','quit.title':'关闭后台服务','prompt.ph':'问一个问题，选中的模型会同时作答','send':'发送','toSend':'发送',
 'tag':'标签','tag.ph':'标签，如 写作','cwd':'工作目录','cwd.ph':'工作目录（可选）','recent':'最近的对比','collapseAll':'全部折叠',
 'empty':'还没有对比记录。在上面输入一个问题，选中的模型会同时作答。','hq.ph':'搜索问题','sby.tool':'按工具','sby.model':'按工具、模型和 effort',
 'stats.note':'胜率是获评「最佳」的次数除以该工具参加并已评判的轮数。平均分只算打过分的回答，平均耗时和字数只算成功的回答。',
 'recheck':'重新检测','env.path':'搜索过的 PATH','env.note':'没找到的工具：一键安装，再点「登录」在弹出的终端里登录一次，最后点「重新检测」。',
 'reader':'阅读模式','reader.hint':'← → 切换  Esc 关闭','raw':'原文 / 排版','copyAns':'复制回答','close':'关闭',
 'err.offline':'连不上后台服务，请重新打开 AI Arena','err.req':'请求失败 {0}','copyFail':'复制失败','default':'默认',
 'lane.on':'参加这轮对比','lane.modelAria':'{0} 使用的模型','lane.model':'模型','lane.noModel':'这个工具没有配置模型参数','lane.custom':'自定义模型 ID…',
 'lane.effAria':'{0} 的 effort','lane.effTitle':'Effort：模型思考的投入程度。越高越深入，也越慢、越耗额度','lane.effDefault':'Effort 默认',
 'lane.customPrompt':'输入 {0} 的模型 ID（会作为模型参数传给它的命令行）',
 'st.queued':'排队中','st.run':'生成中','st.ok':'完成','st.err':'出错 {0}',
 'tie':'平手','bestOf':'最佳 {0}','stop':'停止','rerun':'重跑','rerun.title':'把首个问题作为新对话再问一次',
 'fq.ph':'接着追问，各工具会沿用自己的上下文回答','fq':'追问','fgo':'追问','fhint':'沿用上一轮的工具、模型和 effort。',
 'rounds':'{0} 轮','roundN':'第 {0} 轮','loading':'正在载入…','allFull':'全部展开全文','allFold':'全部收起',
 'more':'展开全文','less':'收起','stars':'{0} 星','note.ph':'写点备注','note.aria':'{0} 的备注','best':'最佳','log':'过程日志','logEmpty':'（暂无）',
 'c.notFound':'没有找到这个工具。','c.install':'到「环境」页安装','c.thinking':'正在思考','c.noOut':'没有输出，原因见下方「过程日志」。',
 'copy':'复制','copied':'已复制','m.actual':'实际使用：{0}','m.chosen':'（选择的是 {0}）','m.model':'模型：{0}','m.effort':'Effort：{0}','cost':'　约 ${0}',
 'j.autoTitle':'每轮回答完成后，自动把回答匿名交给 {0} 评分','j.auto':'{0} 自动评分','j.modelAria':'评分使用的模型',
 'j.ask':'让 {0} 评分','j.busy':'{0} 评分中…','j.again':'重新评分','j.retry':'重试评分','j.sending':'匿名打乱后交给 {0} 评审中…',
 'j.fail':'评分失败：{0}','j.unknown':'未知原因','j.none':'未选出','j.agree':'和你的选择一致','j.best':'{0} 认为最佳：','j.sep':'。',
 'k.accuracy':'准确','k.completeness':'完整','k.clarity':'清晰','k.usefulness':'实用',
 't.busy':'上一轮还在生成，等它结束再追问','t.pickTool':'至少选中一个工具',
 'h.unrated':'未评分','h.open':'打开','h.del':'删除','h.none':'没有找到记录','h.confirm':'删除这条对话（包括所有追问）？',
 's.total':'共 {0} 轮，已评判 {1} 轮','s.tool':'工具','s.n':'参加','s.ok':'成功率','s.avg':'平均分','s.rated':'打分数','s.best':'最佳','s.win':'胜率','s.secs':'平均耗时','s.chars':'平均字数','s.none':'还没有数据',
 'sj.title':'{0} 评分','sj.empty':'还没有 {0} 的评分。在输入区勾选「{0} 自动评分」，或在任意一轮下面点「让 {0} 评分」。',
 'sj.agree':'你和 {0} 选的最佳一致：{1} / {2}（{3}%）','sj.agreeEmpty':'你和它都选过最佳的轮次越多，一致率越有参考价值',
 'sj.avg':'{0} 平均分','sj.n':'评过的回答','sj.best':'{0} 选为最佳',
 'e.checking':'正在检测，需要几秒…','e.node':'Node {0}。','e.noNode':'没检测到 Node.js。','e.where':'一键安装会装到 ~/.ai-arena，不需要管理员权限。',
 'e.tool':'工具','e.status':'状态','e.ver':'版本','e.path':'位置','e.act':'操作','e.login':'登录','e.installing':'安装中…','e.install':'一键安装','e.judge':'评分',
 'e.ok':'已安装','e.missing':'没找到','e.missingExe':'没找到 {0}','e.job':'{0} 安装日志：{1}','e.running':'进行中','e.done':'成功','e.failed':'失败','e.opened':'已打开终端',
 'allTags':'全部标签','quit.confirm':'关闭 AI Arena 后台服务？正在进行的对比会被停止。',
 'quit.done':'AI Arena 已关闭，可以关掉这个标签页。<br>下次双击 AI Arena 就能重新打开。'
},en:{
 'nav.compare':'Compare','nav.history':'History','nav.stats':'Stats','nav.env':'Setup','conn':'The background service is not running. Reopen AI Arena.',
 'quit':'Quit','quit.title':'Stop the background service','prompt.ph':'Ask a question and the selected models answer side by side','send':'Send','toSend':'to send',
 'tag':'Tag','tag.ph':'Tag, e.g. writing','cwd':'Working folder','cwd.ph':'Working folder (optional)','recent':'Recent comparisons','collapseAll':'Collapse all',
 'empty':'No comparisons yet. Ask a question above and the selected models will answer side by side.','hq.ph':'Search questions','sby.tool':'By tool','sby.model':'By tool, model and effort',
 'stats.note':'Win rate is the number of times a tool was picked as best, divided by the judged rounds it took part in. Average score counts rated answers only; average time and length count successful answers only.',
 'recheck':'Check again','env.path':'Searched PATH','env.note':'For a missing tool, click Install, then Sign in and finish the one-time sign-in in the Terminal window, then click Check again.',
 'reader':'Reader','reader.hint':'← → switch   Esc close','raw':'Raw / formatted','copyAns':'Copy answer','close':'Close',
 'err.offline':'Cannot reach the background service. Reopen AI Arena.','err.req':'Request failed {0}','copyFail':'Copy failed','default':'Default',
 'lane.on':'Include in this round','lane.modelAria':'{0} model','lane.model':'Model','lane.noModel':'This tool has no model option configured','lane.custom':'Custom model ID…',
 'lane.effAria':'{0} effort','lane.effTitle':'Effort: how hard the model thinks. Higher goes deeper but is slower and uses more quota','lane.effDefault':'Effort default',
 'lane.customPrompt':'Enter a model ID for {0} (passed to its CLI as the model option)',
 'st.queued':'Queued','st.run':'Generating','st.ok':'Done','st.err':'Error {0}',
 'tie':'Tie','bestOf':'Best: {0}','stop':'Stop','rerun':'Rerun','rerun.title':'Ask the first question again as a new conversation',
 'fq.ph':'Ask a follow-up. Each tool continues its own conversation','fq':'Follow-up','fgo':'Follow up','fhint':'Uses the same tools, models and effort as the last round.',
 'rounds':'{0} rounds','roundN':'Round {0}','loading':'Loading…','allFull':'Expand all','allFold':'Collapse all',
 'more':'Show full answer','less':'Show less','stars':'{0} stars','note.ph':'Add a note','note.aria':'Note for {0}','best':'Best','log':'Process log','logEmpty':'(empty)',
 'c.notFound':'This tool was not found. ','c.install':'Install it on the Setup page','c.thinking':'Thinking','c.noOut':'No output. See the process log below.',
 'copy':'Copy','copied':'Copied','m.actual':'Actual model: {0}','m.chosen':' (selected: {0})','m.model':'Model: {0}','m.effort':'Effort: {0}','cost':'   ≈ ${0}',
 'j.autoTitle':'After each round, send the answers anonymously to {0} for scoring','j.auto':'{0} auto-score','j.modelAria':'Scoring model',
 'j.ask':'Ask {0} to score','j.busy':'{0} is scoring…','j.again':'Score again','j.retry':'Retry scoring','j.sending':'Shuffled and sent anonymously to {0}…',
 'j.fail':'Scoring failed: {0}','j.unknown':'unknown reason','j.none':'none','j.agree':'matches your pick','j.best':'{0} picks as best: ','j.sep':'. ',
 'k.accuracy':'Accuracy','k.completeness':'Completeness','k.clarity':'Clarity','k.usefulness':'Usefulness',
 't.busy':'The last round is still running. Wait for it to finish before following up.','t.pickTool':'Select at least one tool',
 'h.unrated':'Not rated','h.open':'Open','h.del':'Delete','h.none':'No matching conversations','h.confirm':'Delete this conversation, including all follow-ups?',
 's.total':'{0} rounds, {1} judged','s.tool':'Tool','s.n':'Rounds','s.ok':'Success','s.avg':'Avg score','s.rated':'Rated','s.best':'Best','s.win':'Win rate','s.secs':'Avg time','s.chars':'Avg length','s.none':'No data yet',
 'sj.title':'{0} scores','sj.empty':'No {0} scores yet. Turn on "{0} auto-score" in the composer, or click "Ask {0} to score" under any round.',
 'sj.agree':'Your pick matched {0}: {1} / {2} ({3}%)','sj.agreeEmpty':'The more rounds you both pick a best for, the more meaningful the match rate',
 'sj.avg':'{0} avg score','sj.n':'Answers scored','sj.best':'Picked best by {0}',
 'e.checking':'Checking, this takes a few seconds…','e.node':'Node {0}. ','e.noNode':'Node.js not found. ','e.where':'Install puts tools in ~/.ai-arena and needs no admin rights.',
 'e.tool':'Tool','e.status':'Status','e.ver':'Version','e.path':'Location','e.act':'Action','e.login':'Sign in','e.installing':'Installing…','e.install':'Install','e.judge':'judge',
 'e.ok':'Installed','e.missing':'Not found','e.missingExe':'Not found: {0}','e.job':'{0} install log: {1}','e.running':'running','e.done':'succeeded','e.failed':'failed','e.opened':'Terminal opened',
 'allTags':'All tags','quit.confirm':'Stop the AI Arena background service? Running comparisons will be stopped.',
 'quit.done':'AI Arena has stopped. You can close this tab.<br>Double-click AI Arena to open it again.'
}};
const STATUS_EN={'未安装':'Not installed','超时':'Timed out','已取消':'Canceled','启动失败':'Failed to start','已中断':'Interrupted'};
let LANG=(()=>{try{const v=localStorage.getItem('arenaLang');if(v==='zh'||v==='en')return v}catch(e){}return /^zh/i.test(navigator.language||'')?'zh':'en'})();
const T=(k,...a)=>String((I18N[LANG]&&I18N[LANG][k])??I18N.zh[k]??k).replace(/\{(\d)\}/g,(m,i)=>a[+i]??'');
const statusText=s=>LANG==='en'?(STATUS_EN[s]||s):s;
function applyI18n(){
  document.documentElement.lang=LANG==='en'?'en':'zh-CN';
  $$('[data-i18n]').forEach(e=>e.textContent=T(e.dataset.i18n));
  $$('[data-i18n-ph]').forEach(e=>e.placeholder=T(e.dataset.i18nPh));
  $$('[data-i18n-title]').forEach(e=>e.title=T(e.dataset.i18nTitle));
  $$('[data-i18n-aria]').forEach(e=>e.setAttribute('aria-label',T(e.dataset.i18nAria)));
  $$('.langsw button').forEach(b=>{b.classList.toggle('on',b.dataset.l===LANG);b.setAttribute('aria-pressed',b.dataset.l===LANG)});
}
$$('.langsw button').forEach(b=>b.onclick=()=>{if(b.dataset.l===LANG)return;try{localStorage.setItem('arenaLang',b.dataset.l)}catch(e){}location.reload()});
function md(t){t=clean(t);return (window.marked&&window.DOMPurify)?DOMPurify.sanitize(marked.parse(t)):'<pre class="rawtxt">'+esc(t)+'</pre>'}
const ls={get(k,d){try{return JSON.parse(localStorage.getItem(k))??d}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
function toast(m){const el=$('#toast');el.textContent=String(m);el.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(()=>el.hidden=true,3500)}
function offline(on){$('#conn').hidden=on;$('#go').disabled=!on}
function api(path,opt={}){return fetch(path,{...opt,headers:{'Content-Type':'application/json','X-Arena-Token':TOKEN,'X-Arena-Lang':LANG}})
  .catch(()=>{offline(false);throw T('err.offline')})
  .then(async r=>{offline(true);const j=await r.json().catch(()=>({}));if(!r.ok)throw j.error||T('err.req',r.status);return j})}
const post=(p,b={})=>api(p,{method:'POST',body:JSON.stringify(b)});
function fmtTime(ts){const d=new Date(ts*1000),n=new Date(),hm=d.toLocaleTimeString(LANG==='en'?'en-GB':'zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
  if(d.toDateString()===n.toDateString())return hm;
  return LANG==='en'?`${d.toLocaleDateString('en-US',{month:'short',day:'numeric'})} ${hm}`:`${d.getMonth()+1}月${d.getDate()}日 ${hm}`}
function flash(b,t){if(b.dataset.f)return;b.dataset.f=1;const o=b.innerHTML;b.textContent=t;setTimeout(()=>{b.innerHTML=o;delete b.dataset.f},1100)}
function copyText(t,b){navigator.clipboard.writeText(t).then(()=>b&&flash(b,'✓')).catch(()=>toast(T('copyFail')))}

/* ---------- 配置与模型选择 ---------- */
let CFG={tools:{},order:[],tags:[]},SEL={},CUSTOM={};
const color=t=>CFG.tools[t]?.color||'#8893a3';
const EFF_ZH={minimal:'最少',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高'};
const effLabel=e=>e?(LANG==='en'?e:`${e} ${EFF_ZH[e]||''}`.trim()):'';
const dlabel=t=>{const v=CFG.tools[t]||{};return (LANG==='en'?v.default_label_en:v.default_label)||T('default')};
function modelLabel(t,id){if(!id)return dlabel(t);const m=(CFG.tools[t]?.models||[]).find(x=>x.id===id);return m?m.label:id}
function renderLanes(){
  $('#lanes').style.gridTemplateColumns=`repeat(${CFG.order.length},minmax(0,1fr))`;
  $('#lanes').innerHTML=CFG.order.map(t=>{const v=CFG.tools[t],sel=SEL[t];
    const opts=[{id:'',label:dlabel(t)},...v.models,...(CUSTOM[t]||[]).map(id=>({id,label:id}))];
    return `<div class="lane${sel.on?'':' off'}" style="--c:${esc(v.color)}" data-t="${esc(t)}">
      <button class="lsw" role="switch" aria-checked="${sel.on}" title="${esc(T('lane.on'))}"><span class="box"></span>${esc(tname(t))}</button>
      <select class="mdl${sel.model?' set':''}" aria-label="${esc(T('lane.modelAria',tname(t)))}" title="${esc(T('lane.model'))}" ${v.flag?'':`disabled title="${esc(T('lane.noModel'))}"`}>
        ${opts.map(o=>`<option value="${esc(o.id)}"${o.id===sel.model?' selected':''}>${esc(o.label)}</option>`).join('')}
        ${v.flag?`<option value="__custom">${esc(T('lane.custom'))}</option>`:''}</select>
      ${(v.efforts||[]).length?`<select class="eff${sel.effort?' set':''}" aria-label="${esc(T('lane.effAria',tname(t)))}" title="${esc(T('lane.effTitle'))}">
        <option value="">${esc(T('lane.effDefault'))}</option>${v.efforts.map(e=>`<option value="${esc(e)}"${e===sel.effort?' selected':''} title="${esc(LANG==='en'?e:(EFF_ZH[e]||''))}">Effort ${esc(e)}</option>`).join('')}</select>`:''}</div>`}).join('');
}
$('#lanes').addEventListener('click',e=>{const b=e.target.closest('.lsw');if(!b)return;const t=b.closest('.lane').dataset.t;
  SEL[t].on=!SEL[t].on;ls.set('arenaSel',SEL);renderLanes()});
$('#lanes').addEventListener('change',e=>{const s=e.target.closest('select');if(!s)return;const t=s.closest('.lane').dataset.t;
  if(s.classList.contains('eff')){SEL[t].effort=s.value;ls.set('arenaSel',SEL);renderLanes();return}
  if(s.value==='__custom'){const id=(prompt(T('lane.customPrompt',tname(t)))||'').trim();
    if(id){CUSTOM[t]=[...new Set([...(CUSTOM[t]||[]),id])];ls.set('arenaCustom',CUSTOM);SEL[t].model=id}}
  else SEL[t].model=s.value;
  ls.set('arenaSel',SEL);renderLanes()});

/* ---------- 对话线程与每一轮 ---------- */
// 一张卡片 = 一个对话线程（th）；线程里每次提问 = 一轮（r），每轮单独打分、计入统计
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
    <span class="rmeta"><span class="nturn"></span><span class="winc"></span><span class="tagc"></span><time></time></span>
    <span class="ract"><button class="btn sm stop" hidden>${T('stop')}</button><button class="btn sm ghost rerun" title="${esc(T('rerun.title'))}">${T('rerun')}</button></span>
  </header>
  <div class="rbody" hidden><div class="turns"></div>
    <div class="follow"><textarea class="fq" rows="1" placeholder="${esc(T('fq.ph'))}" aria-label="${esc(T('fq'))}"></textarea><button class="btn primary fgo">${T('fgo')}</button></div>
    <p class="fhint">${T('fhint')} <kbd>⌘</kbd> <kbd>Enter</kbd> ${T('toSend')}</p></div>`;
  $('.rq',el).textContent=meta.prompt;$('time',el).textContent=fmtTime(meta.created_at);
  if(meta.tag)$('.tagc',el).innerHTML=`<span class="chip">${esc(meta.tag)}</span>`;
  (meta.st||'').split('|').filter(Boolean).forEach(x=>{const i=x.indexOf(':'),seg=$(`.lanebar i[data-t="${CSS.escape(x.slice(0,i))}"]`,el);if(seg)seg.className=segCls(x.slice(i+1))});
  const w=meta.last_winner??meta.winner;if(w)$('.winc',el).innerHTML=`<span class="chip win">${winText(w)}</span>`;
  if((meta.nturns||0)>1)$('.nturn',el).innerHTML=`<span class="chip">${T('rounds',meta.nturns)}</span>`;
  const rh=$('.rh',el);
  rh.onclick=e=>{if(!e.target.closest('button'))setOpen(th,!th.open)};
  rh.onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&e.target===rh){e.preventDefault();setOpen(th,!th.open)}};
  $('.rerun',el).onclick=()=>send(meta.prompt,th.tools,meta.models||{},meta.efforts||{},meta.tag||'',meta.cwd||'');
  $('.stop',el).onclick=()=>th.turns.filter(r=>r.running).forEach(r=>post('/api/cancel/'+encodeURIComponent(r.id)).catch(toast));
  const fq=$('.fq',el);
  fq.oninput=()=>{fq.style.height='auto';fq.style.height=Math.min(fq.scrollHeight,innerHeight*.3)+'px'};
  fq.onkeydown=e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();follow(th)}};
  $('.fgo',el).onclick=()=>follow(th);
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
  el.innerHTML=`<div class="th2" tabindex="0" role="button" aria-expanded="true"><span class="chev sm">${svg(I.chev)}</span><span class="tn">${T('roundN',idx+1)}</span><span class="tq"></span><span class="tsum"></span></div>
    <div class="tbody"><div class="cols" style="grid-template-columns:repeat(${r.tools.length},minmax(0,1fr))"></div>
    <div class="rfoot"><button class="btn sm ghost allfull">${T('allFull')}</button>${CFG.judge?`<button class="btn sm ghost jbtn">${esc(T('j.ask',tname(CFG.judge)))}</button><span class="jres"></span>`:''}<span class="sp"></span><button class="btn sm ghost tie">${T('tie')}</button></div></div>`;
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
function fillTurn(r,d){   // 已完成的轮次直接用数据库里的结果；还在跑的才接流
  r.winner=d.winner||'';r.autoJudge=!!d.auto_judge;
  if(d.judgment){renderJudge(r,d.judgment);if(d.judgment.running)pollJudge(r)}
  for(const x of d.ratings||[])if(r.S[x.tool]){Object.assign(r.S[x.tool],{score:x.score,note:x.note||''});$('.note',r.S[x.tool].el).value=x.note||'';paintStars(r,x.tool)}
  paintWinner(r);
  if(d.running){openStream(r);return}
  const got=new Set();
  for(const o of d.outputs||[])if(r.S[o.tool]){got.add(o.tool);Object.assign(r.S[o.tool],{out:o.output||'',log:o.log||'',status:o.status,secs:o.seconds,
    actual:o.actual_model||'',model:o.model||r.S[o.tool].model,effort:o.effort||r.S[o.tool].effort});$('.ch b',r.S[o.tool].el).title=o.cmd||''}
  for(const t of r.tools){if(!got.has(t))r.S[t].status='已中断';paint(r,t)}
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
    <button class="ib raw" title="${esc(T('raw'))}" aria-pressed="false">${svg(I.code)}</button><button class="ib cp" title="${esc(T('copyAns'))}">${svg(I.copy)}</button><button class="ib ex" title="${esc(T('reader'))}">${svg(I.expand)}</button></header>
  <div class="body" tabindex="0"></div><button class="more">${T('more')}</button>
  <div class="cf"><span class="stars">${[1,2,3,4,5].map(i=>`<button class="star" data-v="${i}" aria-label="${esc(T('stars',i))}">★</button>`).join('')}</span>
    <input class="note" placeholder="${esc(T('note.ph'))}" aria-label="${esc(T('note.aria',tname(t)))}"><button class="btn sm ghost win">${T('best')}</button></div>
  <div class="jd" hidden></div>
  <details class="log"><summary>${T('log')}<span class="cost"></span></summary><pre></pre></details>`;
  $('.raw',c).onclick=()=>{s.raw=!s.raw;paint(r,t)};
  $('.cp',c).onclick=e=>copyText(clean(s.out),e.currentTarget);
  $('.ex',c).onclick=()=>openReader(r,t);
  $('.more',c).onclick=()=>{s.full=!s.full;paint(r,t);if(!s.full)c.scrollIntoView({block:'nearest'})};
  $$('.star',c).forEach(b=>b.onclick=()=>rate(r,t,s.score===+b.dataset.v?0:+b.dataset.v));
  $('.note',c).onchange=e=>{s.note=e.target.value;rate(r,t,s.score)};
  $('.win',c).onclick=()=>setWinner(r,r.winner===t?'':t);
  parent.appendChild(c);paint(r,t);
}
function content(s){
  if(s.out)return s.raw?`<pre class="rawtxt">${esc(clean(s.out))}</pre>`:`<div class="md">${md(s.out)}</div>`;
  if(s.status==='未安装')return `<p class="ph">${T('c.notFound')}<a href="#" data-go="env">${T('c.install')}</a></p>`;
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

/* ---------- 评分（匿名交给第三方模型） ---------- */
let JSEL={model:'',auto:false};
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
function deanon(t,map){  // 把 Grok 点评里的匿名标签（回答 A、A 比 C）换回工具名
  const nm=l=>map&&map[l]?tname(map[l]):null;
  return String(t||'').replace(/(?:回答|Answer|Response)\s*([A-H])\b/g,(m,l)=>nm(l)||m).replace(/\b([A-H])'s\b/g,(m,l)=>nm(l)?nm(l)+"'s":m)
    .replace(/(^|[^A-Za-z0-9])([A-H])(?=\s*(?:[\u4e00-\u9fff，。、：；！？,.;:!?）)]|$))/g,(m,p,l)=>nm(l)?p+nm(l):m);
}
function renderJudge(r,j){
  r.judge=j;const name=esc(tname(CFG.judge||'grok')),b=$('.jbtn',r.el),res=$('.jres',r.el);if(!res)return;
  const busy=j.running||j.status==='running';
  b.disabled=busy;b.textContent=busy?T('j.busy',tname(CFG.judge)):j.status==='ok'?T('j.again'):j.status?T('j.retry'):T('j.ask',tname(CFG.judge));
  res.classList.toggle('err',j.status==='error');res.title='';
  for(const t of r.tools){const jd=$('.jd',r.S[t].el);jd.hidden=true;jd.innerHTML=''}
  if(busy){res.textContent=T('j.sending',tname(CFG.judge||'grok'));return}
  if(j.status==='error'){res.textContent=T('j.fail',j.reason||T('j.unknown'));res.title=(j.raw||'').slice(-2000);return}
  if(j.status!=='ok'){res.textContent='';return}
  for(const x of j.scores||[]){const s=r.S[x.tool];if(!s)continue;const jd=$('.jd',s.el);jd.hidden=false;
    const det=Object.entries(x.detail||{}).map(([k,v])=>(T('k.'+k)||k)+' '+v).join('  ');
    jd.innerHTML=`<b>${name} ${x.overall!=null?x.overall:'—'}/5</b>${esc(deanon(x.comment,j.mapping))}${det?`<span class="jx">${esc(det)}</span>`:''}`}
  const best=j.best==='tie'?T('tie'):j.best?tname(j.best):T('j.none');
  const agree=r.winner&&j.best?(r.winner===j.best?`<span class="agree">${T('j.agree')}</span>`:''):'';
  const why=deanon(j.reason,j.mapping);res.innerHTML=`${esc(T('j.best',tname(CFG.judge||'grok')))}<b>${esc(best)}</b>${agree}${why?esc(T('j.sep'))+esc(why):''}`;res.title=why;
}

async function send(prompt,tools,models,efforts,tag,cwd){
  try{const d=await post('/api/run',{prompt,tools,models,efforts,tag,cwd,judge:!!(CFG.judge&&JSEL.auto),judge_model:JSEL.model});
    for(const x of ROUNDS.values())if(x.open)setOpen(x,false);
    const meta={id:d.id,prompt,tag:d.tag,created_at:Date.now()/1000,tools:d.tools,models:d.models||{},efforts:d.efforts||{},winner:'',cwd};
    const th=makeRound(meta);th.built=true;
    $('#empty').hidden=true;feed.prepend(th.el);
    const r=makeTurn(th,meta,0);r.autoJudge=!!(CFG.judge&&JSEL.auto);setOpen(th,true);paintHead(th);openStream(r);
    window.scrollTo({top:0,behavior:'smooth'});return true}
  catch(e){toast(e);return false}
}
async function follow(th){
  const fq=$('.fq',th.el),p=fq.value.trim(),last=lastTurn(th);if(!p)return fq.focus();if(!last)return;
  if(th.turns.some(r=>r.running))return toast(T('t.busy'));
  try{const d=await post('/api/run',{prompt:p,parent:last.id,judge:!!(CFG.judge&&JSEL.auto),judge_model:JSEL.model});
    const r=makeTurn(th,{id:d.id,prompt:p,tools:d.tools,models:d.models||{},efforts:d.efforts||{},winner:''},th.turns.length);
    r.autoJudge=!!(CFG.judge&&JSEL.auto);
    foldOld(th);paintHead(th);openStream(r);fq.value='';fq.style.height='auto';
    r.el.scrollIntoView({behavior:'smooth',block:'start'})}
  catch(e){toast(e)}
}
function submitComposer(){
  const p=$('#prompt').value.trim();if(!p)return $('#prompt').focus();
  const tools=CFG.order.filter(t=>SEL[t].on);if(!tools.length)return toast(T('t.pickTool'));
  send(p,tools,Object.fromEntries(tools.map(t=>[t,SEL[t].model||''])),Object.fromEntries(tools.map(t=>[t,SEL[t].effort||''])),
    $('#tag').value.trim(),$('#cwd').value.trim())
    .then(ok=>{if(ok){$('#prompt').value='';autosize()}});
}
function autosize(){const p=$('#prompt');p.style.height='auto';p.style.height=Math.min(p.scrollHeight,innerHeight*.4)+'px'}
$('#prompt').addEventListener('input',autosize);
$('#prompt').addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();submitComposer()}});
$('#go').onclick=submitComposer;
$('#collapseAll').onclick=()=>{for(const th of ROUNDS.values())if(th.open)setOpen(th,false)};
const pj=(v,d)=>typeof v==='string'?(()=>{try{return JSON.parse(v||'null')??d}catch(e){return d}})():(v||d);
const norm=m=>({...m,tools:pj(m.tools,[]),models:pj(m.models,{}),efforts:pj(m.efforts,{})});
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

/* ---------- 阅读模式 ---------- */
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

/* ---------- 历史 ---------- */
async function loadHistory(){
  const d=await api(`/api/history?q=${encodeURIComponent($('#hq').value)}&tag=${encodeURIComponent($('#htag').value)}`);
  $('#hlist').innerHTML=d.runs.map(r=>`<li class="hitem"><div class="hmain"><div class="hq">${esc(r.prompt)}</div>
    <div class="hmeta"><time>${fmtTime(r.created_at)}</time>${r.tag?`<span class="chip">${esc(r.tag)}</span>`:''}
    ${r.nturns>1?`<span class="chip">${T('rounds',r.nturns)}</span>`:''}${(r.last_winner||r.winner)?`<span class="chip win">${winText(r.last_winner||r.winner)}</span>`:''}<span>${esc(r.scores||T('h.unrated'))}</span></div></div>
    <div class="hact"><button class="btn sm" data-a="open" data-id="${esc(r.id)}">${T('h.open')}</button><button class="btn sm ghost" data-a="del" data-id="${esc(r.id)}">${T('h.del')}</button></div></li>`).join('')
    ||`<li class="hitem" style="color:var(--muted)">${T('h.none')}</li>`;
}
$('#hlist').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const id=b.dataset.id;
  if(b.dataset.a==='open')openRun(id).catch(toast);
  else if(confirm(T('h.confirm'))){await post('/api/delete/'+encodeURIComponent(id)).catch(toast);const r=ROUNDS.get(id);if(r){r.el.remove();ROUNDS.delete(id)}loadHistory()}};
let hqT;$('#hq').oninput=()=>{clearTimeout(hqT);hqT=setTimeout(loadHistory,250)};$('#htag').onchange=loadHistory;

/* ---------- 统计 ---------- */
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

/* ---------- 环境 ---------- */
let jobT=0;
async function loadEnv(){
  $('#etable').innerHTML=`<tr><td style="color:var(--muted)">${T('e.checking')}</td></tr>`;
  let d;try{d=await api('/api/doctor')}catch(e){$('#etable').innerHTML=`<tr><td>${esc(e)}</td></tr>`;return}
  $('#envNode').textContent=(d.node?T('e.node',d.node):T('e.noNode'))+T('e.where');
  $('#etable').innerHTML='<tr>'+['e.tool','e.status','e.ver','e.path','e.act'].map(k=>`<th>${T(k)}</th>`).join('')+'</tr>'+d.tools.map(t=>{const job=d.jobs[t.name]||{};
    const hint=LANG==='en'?(t.hint_en||t.hint):t.hint;
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

/* ---------- 导航与启动 ---------- */
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
  for(const t of CFG.order)SEL[t]={on:saved[t]?.on??true,model:saved[t]?.model??'',effort:saved[t]?.effort??''};
  JSEL={model:'',auto:false,...ls.get('arenaJudge',{})};
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
