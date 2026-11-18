#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Обход — центр управления обходом блокировок.

Чёрно-белый интерфейс. Каждый сервис включается отдельным тумблером:

  Discord     → zapret (nfqws), голос и звонки
  YouTube     → zapret, видео и превью
  SoundCloud  → zapret, музыка и обложки
  Telegram    → TgWsProxy, MTProto через WebSocket

У каждого сервиса своя DPI-стратегия: то, что пробивает видео YouTube,
не пробивает голос Discord, и наоборот.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

APP_ID = "io.github.obhod.Obhod"
VERSION = "2.1"

HOME = Path.home()
STATE_DIR = HOME / ".config" / "obhod"
STATE_FILE = STATE_DIR / "state.json"

ZAPRET_DIR = HOME / "zapret-discord-youtube-linux"
ZAPRET_LATEST = ZAPRET_DIR / "zapret-latest"
ZAPRET_LISTS = ZAPRET_LATEST / "lists"
ZAPRET_SERVICE = "zapret_discord_youtube.service"
STRATEGY_NAME = "obhod.bat"

TG_BIN = Path("/usr/local/bin/TgWsProxy")
TG_CFG = HOME / ".config/TgWsProxy/config.json"
TG_LOG = HOME / ".config/TgWsProxy/proxy.log"
DISCORD_LOG = HOME / ".config/discord/logs/renderer_js.log"

HOSTLIST_USER = "list-general-user.txt"
HOSTLIST_EXCLUDE = "list-exclude-user.txt"


# ─────────────────────────── сервисы ────────────────────────────
#
# method — проверенная вручную стратегия для этого сервиса.
# Подбирать «одну на всех» бессмысленно: у голоса Discord и у видео
# YouTube разные DPI-фильтры, и один метод ломает другой.

SERVICES = [
    {
        "id": "discord",
        "title": "Discord",
        "subtitle": "Голосовые каналы и звонки",
        "icon": "audio-headphones-symbolic",
        "domains": ["discord.com", "discord.gg", "discordapp.com",
                    "discordapp.net", "discord.media"],
        "ports": "443,2053,2083,2087,2096,8443",
        "method": ("--dpi-desync=multisplit "
                   "--dpi-desync-split-pos=1"),
        "probe": "voice",
    },
    {
        "id": "youtube",
        "title": "YouTube",
        "subtitle": "Видео, стримы, превью, аватары",
        "icon": "media-playback-start-symbolic",
        "domains": ["youtube.com", "youtu.be", "googlevideo.com", "ytimg.com",
                    "ggpht.com", "googleusercontent.com",
                    "youtube-nocookie.com", "youtubei.googleapis.com"],
        "ports": "443,8443",
        "method": ("--dpi-desync=multidisorder "
                   "--dpi-desync-split-pos=midsld"),
        "probe": "https://www.youtube.com/",
    },
    {
        "id": "soundcloud",
        "title": "SoundCloud",
        "subtitle": "Музыка, волна, обложки",
        "icon": "audio-speakers-symbolic",
        "domains": ["soundcloud.com", "sndcdn.com", "soundcloud.cloud"],
        "ports": "443,8443",
        "method": ("--dpi-desync=multidisorder "
                   "--dpi-desync-split-pos=midsld"),
        "probe": "https://soundcloud.com/",
    },
]

SERVICE_BY_ID = {s["id"]: s for s in SERVICES}

# Домены, которые нужны только для картинок (аватары YouTube, обложки
# SoundCloud). Ими управляет соответствующий сервис, поэтому они лежат
# в его же списке, но для проверки нужен отдельный пробник.
COVER_PROBES = {
    "youtube": "https://yt3.ggpht.com/",
    "soundcloud": "https://i1.sndcdn.com/",
}


CSS = """
@define-color line alpha(@theme_fg_color, 0.14);

.pill {
    border-radius: 999px;
    padding: 1px 9px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.4px;
}
.pill.on   { background: @theme_fg_color; color: @theme_bg_color; }
.pill.off  { background: transparent; color: alpha(@theme_fg_color, 0.45);
             border: 1px solid @line; }
.pill.wait { background: alpha(@theme_fg_color, 0.10);
             color: alpha(@theme_fg_color, 0.75); }

switch:checked          { background-color: @theme_fg_color; }
switch:checked > slider { background-color: @theme_bg_color; }

.count {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
    opacity: 0.55;
}
.mono { font-family: monospace; font-size: 11.5px; }
.row-dim { opacity: 0.55; }
"""


# ─────────────────────────── утилиты ────────────────────────────

def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "таймаут"
    except FileNotFoundError:
        return 127, f"не найдено: {cmd[0]}"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def run_async(fn, on_done):
    def worker():
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            result = exc
        GLib.idle_add(_deliver, on_done, result)
    threading.Thread(target=worker, daemon=True).start()


def _deliver(cb, result):
    cb(result)
    return False


def svc_active(name):
    rc, out = run(["systemctl", "is-active", name], timeout=5)
    return out.strip().splitlines()[0] if out.strip() else "unknown"


def port_open(port, host="127.0.0.1", timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tg_running():
    return run(["pgrep", "-f", str(TG_BIN)], timeout=5)[0] == 0


def tg_config():
    try:
        return json.loads(TG_CFG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def tg_link(cfg):
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 1443)
    secret = cfg.get("secret", "")
    if not secret:
        return None
    if not secret.startswith("dd"):
        secret = "dd" + secret
    return f"tg://proxy?server={host}&port={port}&secret={secret}"


def tail(path, lines=80):
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 64_000))
            return "\n".join(fh.read().splitlines()[-lines:])
    except Exception:  # noqa: BLE001
        return ""


def last_voice_endpoint():
    """Актуальный голосовой сервер Discord из его лога."""
    if not DISCORD_LOG.exists():
        return None
    try:
        with DISCORD_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 150_000))
            text = fh.read()
    except Exception:  # noqa: BLE001
        return None
    found = re.findall(r"wss://([a-z0-9.\-]+\.discord\.media):(\d+)", text)
    return found[-1] if found else None


def probe_url(url, timeout=8):
    """Время TLS-хендшейка в секундах или None, если он не прошёл."""
    rc, out = run(["curl", "-4", "-k", "-s", "-o", "/dev/null",
                   "--max-time", str(timeout),
                   "-w", "%{time_appconnect}", url], timeout=timeout + 3)
    try:
        tls = float(out.strip().split()[0])
    except (ValueError, IndexError):
        return None
    return tls if tls > 0 else None


def probe_service(svc_id):
    svc = SERVICE_BY_ID.get(svc_id, {})
    if svc.get("probe") == "voice":
        ep = last_voice_endpoint()
        if not ep:
            return probe_url("https://discord.media:2053/")
        return probe_url(f"https://{ep[0]}:{ep[1]}/")
    return probe_url(svc.get("probe", ""))


def probe_cover(svc_id):
    url = COVER_PROBES.get(svc_id)
    return probe_url(url) if url else None


# ─────────────────────────── состояние ──────────────────────────

DEFAULT_ENABLED = ["discord", "youtube", "soundcloud", "telegram"]


def load_state():
    state = {"enabled": list(DEFAULT_ENABLED)}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("enabled"), list):
            known = {s["id"] for s in SERVICES} | {"telegram"}
            state["enabled"] = [x for x in data["enabled"] if x in known]
    except Exception:  # noqa: BLE001
        pass
    return state


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ─────────────────────────── zapret ─────────────────────────────

STRATEGY_HEAD = """@echo off
chcp 65001 > nul
:: сгенерировано приложением Обход — не редактировать вручную
cd /d "%~dp0"
set "BIN=%~dp0bin\\"
set "LISTS=%~dp0lists\\"
cd /d %BIN%

start "zapret: obhod" /min "%BIN%winws.exe" --wf-tcp=80,443,2053,2083,2087,2096,8443 --wf-udp=443,19294-19344,50000-50100 ^
--filter-udp=443 --hostlist="%LISTS%list-general.txt" --hostlist="%LISTS%list-general-user.txt" --hostlist="%LISTS%list-google.txt" --hostlist-exclude="%LISTS%list-exclude.txt" --hostlist-exclude="%LISTS%list-exclude-user.txt" --ipset-exclude="%LISTS%ipset-exclude.txt" --ipset-exclude="%LISTS%ipset-exclude-user.txt" --dpi-desync=fake --dpi-desync-repeats=6 --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin" --new ^
--filter-udp=19294-19344,50000-50100 --filter-l7=discord,stun --dpi-desync=fake --dpi-desync-fake-discord="%BIN%quic_initial_dbankcloud_ru.bin" --dpi-desync-fake-stun="%BIN%quic_initial_dbankcloud_ru.bin" --dpi-desync-repeats=6 --new ^
"""

STRATEGY_TAIL = """--filter-tcp=80 --hostlist="%LISTS%list-general.txt" --hostlist="%LISTS%list-general-user.txt" --hostlist-exclude="%LISTS%list-exclude.txt" --hostlist-exclude="%LISTS%list-exclude-user.txt" --dpi-desync=fake --dpi-desync-repeats=6 --dpi-desync-fooling=ts --dpi-desync-fake-http="%BIN%tls_clienthello_max_ru.bin"
"""


def service_rule(svc):
    """Одно TCP-правило под конкретный сервис."""
    domains = ",".join(svc["domains"])
    return (
        f'--filter-tcp={svc["ports"]} '
        f'--hostlist-domains={domains} '
        f'--ipset-exclude="%LISTS%ipset-exclude.txt" '
        f'--ipset-exclude="%LISTS%ipset-exclude-user.txt" '
        f'{svc["method"]} --new ^\n'
    )


def build_strategy(enabled_ids):
    """Правила только для включённых сервисов — выключенные не трогаем."""
    rules = "".join(service_rule(s) for s in SERVICES if s["id"] in enabled_ids)
    return STRATEGY_HEAD + rules + STRATEGY_TAIL


def write_strategy(enabled_ids):
    path = ZAPRET_LATEST / STRATEGY_NAME
    if not path.parent.exists():
        raise FileNotFoundError("не найден каталог zapret-latest")

    text = build_strategy(enabled_ids)
    old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if old == text:
        return False

    if path.exists():
        shutil.copy2(path, str(path) + ".bak")
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(text)

    conf = ZAPRET_DIR / "conf.env"
    lines, found = [], False
    if conf.exists():
        for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("strategy="):
                lines.append(f"strategy={STRATEGY_NAME}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"strategy={STRATEGY_NAME}")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def sync_lists(enabled_ids):
    """Домены включённых сервисов — в user-хостлист, выключенных — в исключения."""
    ZAPRET_LISTS.mkdir(parents=True, exist_ok=True)
    user, excl = [], []
    for svc in SERVICES:
        target = user if svc["id"] in enabled_ids else excl
        target.extend(svc["domains"])
    (ZAPRET_LISTS / HOSTLIST_USER).write_text(
        "\n".join(user) + ("\n" if user else ""), encoding="utf-8")
    (ZAPRET_LISTS / HOSTLIST_EXCLUDE).write_text(
        "\n".join(excl) + ("\n" if excl else ""), encoding="utf-8")


def have_passwordless():
    return Path("/etc/polkit-1/rules.d/49-obhod.rules").exists()


def polkit_rule_text():
    user = os.environ.get("USER") or HOME.name
    return f'''// Разрешает пользователю {user} управлять только службой zapret.
polkit.addRule(function(action, subject) {{
    if (action.id != "org.freedesktop.systemd1.manage-units")
        return null;
    var unit = action.lookup("unit");
    if (unit != "{ZAPRET_SERVICE}")
        return null;
    if (subject.user != "{user}" || !subject.local || !subject.active)
        return null;
    var verb = action.lookup("verb");
    if (verb == "start" || verb == "stop" || verb == "restart")
        return polkit.Result.YES;
    return null;
}});
'''


def install_polkit_rule():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(polkit_rule_text())
        tmp = fh.name
    rc, out = run(["pkexec", "install", "-m", "644", tmp,
                   "/etc/polkit-1/rules.d/49-obhod.rules"], timeout=120)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return rc, out


def control_service(action):
    """start / stop / restart. Пароль спрашивается только если правила нет."""
    if have_passwordless() or action == "restart":
        rc, out = run(["systemctl", action, ZAPRET_SERVICE], timeout=60)
        if rc == 0:
            return rc, out
    if shutil.which("pkexec"):
        return run(["pkexec", "systemctl", action, ZAPRET_SERVICE], timeout=180)
    return run(["sudo", "-n", "systemctl", action, ZAPRET_SERVICE], timeout=60)


def apply_configuration(enabled_ids):
    """Записать стратегию и списки; перезапустить службу, если что-то менялось."""
    changed = write_strategy(enabled_ids)
    sync_lists(enabled_ids)
    if changed:
        control_service("restart")
    return changed


# ─────────────────────────── окно ──────────────────────────────

class ObhodWindow(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title="Обход",
                         default_width=620, default_height=860)
        self.set_size_request(480, 520)

        self.state = load_state()
        self._busy = False
        self._guard = {}
        self.rows = {}
        self._closing = False

        self.connect("close-request", self._on_close)

        self._install_css()
        self._build()

        GLib.timeout_add_seconds(5, self._tick)
        self.refresh()
        GLib.timeout_add(600, lambda: (self._startup_apply(), False)[1])
        GLib.timeout_add(900, lambda: (self.check_all(silent=True), False)[1])
        GLib.timeout_add(1500, lambda: (self._load_logs(), False)[1])

    def _on_close(self, *_):
        self._closing = True
        return False

    def _install_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── сборка ─────────────────────────────────────────────────

    def _build(self):
        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)

        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Обход", subtitle="")
        header.set_title_widget(self.title_widget)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                  tooltip_text="Меню")
        menu = Gtk.PopoverMenu()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        for label, action in (
            ("Применить настройки", "apply"),
            ("Разрешить управление без пароля", "polkit"),
            ("О программе", "about"),
        ):
            item = Gtk.Button(label=label)
            item.add_css_class("flat")
            item.connect("clicked", lambda _b, a=action: self._on_menu(a))
            box.append(item)
        menu.set_child(box)
        menu_btn.set_popover(menu)
        header.pack_end(menu_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        page.add(self._build_services())
        page.add(self._build_telegram())
        page.add(self._build_tools())
        page.add(self._build_logs())
        toolbar.set_content(page)

        self.set_content(self.toasts)

    def _build_services(self):
        group = Adw.PreferencesGroup(
            title="Сервисы",
            description="Каждый обход включается отдельно")
        for svc in SERVICES:
            row = Adw.ActionRow(title=svc["title"], subtitle=svc["subtitle"],
                                icon_name=svc["icon"])
            row.set_subtitle_lines(1)

            pill = Gtk.Label(label="—", valign=Gtk.Align.CENTER)
            pill.add_css_class("pill")
            pill.add_css_class("wait")
            row.add_suffix(pill)

            switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            switch.connect("state-set", self._on_service_state_set, svc["id"])
            row.add_suffix(switch)
            row.set_activatable_widget(switch)

            self.rows[svc["id"]] = {"row": row, "pill": pill, "switch": switch}
            group.add(row)
        return group

    def _build_telegram(self):
        group = Adw.PreferencesGroup(
            title="Telegram",
            description="MTProto-мост через WebSocket")

        self.tg_row = Adw.ActionRow(title="TG WS Proxy", subtitle="выключен",
                                    icon_name="send-to-symbolic")
        self.tg_row.set_subtitle_lines(1)
        self.tg_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.tg_switch.connect("state-set", self._on_tg_state_set)
        self.tg_row.add_suffix(self.tg_switch)
        self.tg_row.set_activatable_widget(self.tg_switch)
        group.add(self.tg_row)

        self.tg_link_row = Adw.ActionRow(title="Прокси-ссылка", subtitle="—",
                                         icon_name="insert-link-symbolic")
        self.tg_link_row.set_use_markup(False)
        self.tg_link_row.set_subtitle_lines(1)
        self.tg_link_row.add_css_class("mono")
        copy = Gtk.Button(icon_name="edit-copy-symbolic",
                          tooltip_text="Скопировать", valign=Gtk.Align.CENTER)
        copy.connect("clicked", lambda *_: self._copy_tg_link())
        open_btn = Gtk.Button(icon_name="external-link-symbolic",
                              tooltip_text="Открыть в Telegram",
                              valign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda *_: self._open_tg_link())
        self.tg_link_row.add_suffix(copy)
        self.tg_link_row.add_suffix(open_btn)
        group.add(self.tg_link_row)
        return group

    def _build_tools(self):
        group = Adw.PreferencesGroup(title="Обслуживание")

        self.strategy_row = Adw.ActionRow(
            title="Стратегии DPI",
            subtitle="у каждого сервиса своя",
            icon_name="preferences-system-symbolic")
        self.strategy_row.set_subtitle_lines(1)
        apply_btn = Gtk.Button(label="Применить", valign=Gtk.Align.CENTER)
        apply_btn.connect("clicked", lambda *_: self.apply_now())
        self.strategy_row.add_suffix(apply_btn)
        group.add(self.strategy_row)

        check = Adw.ActionRow(title="Проверка", subtitle="TLS до каждого сервиса",
                              icon_name="network-transmit-receive-symbolic")
        run_btn = Gtk.Button(label="Проверить", valign=Gtk.Align.CENTER)
        run_btn.connect("clicked", lambda *_: self.check_all())
        check.add_suffix(run_btn)
        group.add(check)
        return group

    def _build_logs(self):
        group = Adw.PreferencesGroup(title="Журнал")
        row = Adw.ActionRow(title="Последние события",
                            subtitle="zapret и TgWsProxy",
                            icon_name="utilities-terminal-symbolic")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic",
                             tooltip_text="Обновить", valign=Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self._load_logs())
        row.add_suffix(refresh)
        group.add(row)

        wrap = Gtk.ScrolledWindow()
        wrap.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        wrap.set_min_content_height(190)
        wrap.set_margin_top(6)
        wrap.set_margin_bottom(8)
        wrap.set_margin_start(8)
        wrap.set_margin_end(8)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False,
                                     monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.add_css_class("mono")
        self.log_view.set_top_margin(8)
        self.log_view.set_bottom_margin(8)
        self.log_view.set_left_margin(10)
        self.log_view.set_right_margin(10)
        wrap.set_child(self.log_view)
        group.add(wrap)
        return group

    # ── мелочи UI ──────────────────────────────────────────────

    def _set_pill(self, pill, text, cls):
        for c in ("on", "off", "wait"):
            pill.remove_css_class(c)
        pill.add_css_class(cls)
        pill.set_label(text)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _on_menu(self, action):
        if action == "apply":
            self.apply_now()
        elif action == "polkit":
            self._install_polkit()
        elif action == "about":
            self._about()

    # ── состояние ──────────────────────────────────────────────

    def _tick(self):
        if not self._busy and not self._closing:
            self.refresh()
        return True

    def refresh(self):
        run_async(self._collect, self._apply)

    def _collect(self):
        return {
            "svc": svc_active(ZAPRET_SERVICE),
            "tg": tg_running(),
            "tg_port": port_open(int(tg_config().get("port", 1443))),
            "cfg": tg_config(),
        }

    def _apply(self, st):
        if isinstance(st, Exception):
            self._busy = False
            return

        enabled = set(self.state.get("enabled", []))

        for svc in SERVICES:
            wid = svc["id"]
            row = self.rows[wid]
            self._guard[wid] = True
            if not self._closing:
                row["switch"].set_active(wid in enabled)
                row["switch"].set_sensitive(not self._busy)
            self._guard[wid] = False

        tg_on = st["tg"] and st["tg_port"]
        self._guard["telegram"] = True
        if not self._closing:
            self.tg_switch.set_active(st["tg"])
            self.tg_switch.set_sensitive(not self._busy)
        self._guard["telegram"] = False

        if tg_on:
            self.tg_row.set_subtitle(
                f"Работает на порту {st['cfg'].get('port', 1443)}")
        else:
            self.tg_row.set_subtitle("Выключен — Telegram напрямую")

        self.tg_link_row.set_subtitle(tg_link(st["cfg"]) or "—")

        total = len(SERVICES) + 1
        active = sum(1 for s in SERVICES if s["id"] in enabled)
        active += 1 if tg_on else 0
        suffix = "" if st["svc"] == "active" else "  ·  служба выключена"
        self.title_widget.set_subtitle(f"{active} из {total} активно{suffix}")

    def _startup_apply(self):
        """Привести .bat и списки в соответствие с сохранённым состоянием."""
        if self._closing:
            return
        enabled = self.state.get("enabled", [])

        def work():
            return apply_configuration(enabled)

        def done(res):
            if isinstance(res, Exception):
                return
            if res:
                self.refresh()
                GLib.timeout_add(1200, lambda: (self.check_all(silent=True), False)[1])

        run_async(work, done)

    # ── переключатели сервисов ─────────────────────────────────

    def _on_service_state_set(self, switch, state, svc_id):
        if self._guard.get(svc_id):
            return False          # программная установка — просто применяем
        if self._busy or self._closing:
            return True

        want = bool(state)
        enabled = set(self.state.get("enabled", []))
        if want:
            enabled.add(svc_id)
        else:
            enabled.discard(svc_id)
        self.state["enabled"] = sorted(enabled)
        save_state(self.state)

        self._busy = True
        switch.set_sensitive(False)

        def work():
            write_strategy(enabled)
            sync_lists(enabled)
            return control_service("restart")

        def done(res):
            self._busy = False
            rc = res[0] if isinstance(res, tuple) else 1
            if rc != 0:
                self.toast("Нужно разрешение — меню → «Разрешить без пароля»")
            else:
                self.toast(f"{SERVICE_BY_ID[svc_id]['title']}: "
                           + ("обход включён" if want else "обход выключен"))
            self.refresh()
            GLib.timeout_add(1200, lambda: (self.check_all(silent=True), False)[1])

        run_async(work, done)
        return True

    # ── Telegram ───────────────────────────────────────────────

    def _on_tg_state_set(self, switch, state):
        if self._guard.get("telegram"):
            return False
        if self._busy or self._closing:
            return True

        want = bool(state)
        self._busy = True
        switch.set_sensitive(False)

        def work():
            if want:
                if not TG_BIN.exists():
                    return (False, f"не найден {TG_BIN}")
                subprocess.Popen([str(TG_BIN)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 start_new_session=True, cwd=str(HOME))
                for _ in range(20):
                    GLib.usleep(150_000)
                    if port_open(int(tg_config().get("port", 1443))):
                        return (True, "")
                return (False, "порт не открылся")
            run(["pkill", "-f", str(TG_BIN)], timeout=10)
            return (True, "")

        def done(res):
            self._busy = False
            ok, err = res if isinstance(res, tuple) else (False, str(res))
            self.toast("Telegram: прокси включён" if ok and want else
                       ("Telegram: прокси выключен" if ok else f"Telegram: {err}"))
            self.refresh()

        run_async(work, done)
        return True

    def _copy_tg_link(self):
        link = tg_link(tg_config())
        if not link:
            self.toast("Ссылка не найдена")
            return
        Gdk.Display.get_default().get_clipboard().set(link)
        self.toast("Ссылка скопирована")

    def _open_tg_link(self):
        link = tg_link(tg_config())
        if not link:
            self.toast("Ссылка не найдена")
            return
        if run(["xdg-open", link], timeout=10)[0] != 0:
            self._copy_tg_link()
            self.toast("tg:// не открылся — ссылка скопирована")

    # ── проверка ───────────────────────────────────────────────

    def check_all(self, silent=False):
        ids = [s["id"] for s in SERVICES]

        def work():
            return {i: (probe_service(i), probe_cover(i)) for i in ids}

        def done(res):
            if isinstance(res, Exception):
                return
            ok_count = 0
            for svc_id, (tls, cover) in res.items():
                pill = self.rows[svc_id]["pill"]
                if tls:
                    ok_count += 1
                    text = f"{tls*1000:.0f} мс"
                    if cover is None:
                        self._set_pill(pill, text, "on")
                    elif cover:
                        self._set_pill(pill, f"{text} · ✓", "on")
                    else:
                        self._set_pill(pill, f"{text} · обложки ✗", "wait")
                else:
                    self._set_pill(pill, "блок", "off")
            if not silent:
                self.toast(f"Проходят {ok_count} из {len(ids)}")

        run_async(work, done)

    # ── применение настроек ────────────────────────────────────

    def apply_now(self):
        if self._busy:
            return
        self._busy = True
        self.strategy_row.set_subtitle("Применяю…")
        enabled = set(self.state.get("enabled", []))

        def work():
            changed = write_strategy(enabled)
            sync_lists(enabled)
            return changed, control_service("restart")

        def done(res):
            self._busy = False
            if isinstance(res, Exception):
                self.toast(f"Ошибка: {res}")
                self.refresh()
                return
            changed, (rc, _out) = res
            if rc != 0:
                self.strategy_row.set_subtitle("Не удалось перезапустить службу")
                self.toast("Нужно разрешение — меню → «Разрешить без пароля»")
            else:
                self.strategy_row.set_subtitle(
                    "Правила обновлены" if changed else "Правила уже применены")
                self.toast("Готово — проверяю сервисы")
                GLib.timeout_add(1500, lambda: (self.check_all(), False)[1])
            self.refresh()

        run_async(work, done)

    # ── polkit ─────────────────────────────────────────────────

    def _install_polkit(self):
        if self._busy:
            return
        self._busy = True
        self.toast("Спрошу пароль администратора…")

        def done(res):
            self._busy = False
            rc = res[0] if isinstance(res, tuple) else 1
            if rc == 0:
                self.toast("Готово — управление без пароля включено")
            else:
                self.toast("Не удалось — попробуй ещё раз")

        run_async(install_polkit_rule, done)

    # ── журнал ─────────────────────────────────────────────────

    def _load_logs(self):
        def work():
            rc, zap = run(["journalctl", "-u", ZAPRET_SERVICE, "-n", "40",
                           "--no-pager", "-o", "cat"], timeout=10)
            zap_lines = [ln for ln in zap.splitlines() if ln.strip()][-22:]
            tg_lines = tail(TG_LOG, 22).splitlines()
            chunks = []
            if zap_lines:
                chunks.append("── zapret ──")
                chunks += zap_lines
            if tg_lines:
                chunks += ["", "── TgWsProxy ──"] + tg_lines
            return "\n".join(chunks) or "Журнал пуст."

        def done(text):
            if isinstance(text, Exception):
                text = f"Ошибка: {text}"
            self.log_view.get_buffer().set_text(text)

        run_async(work, done)

    def _about(self):
        dlg = Adw.AboutDialog(
            application_name="Обход",
            application_icon=APP_ID,
            version=VERSION,
            developer_name="для личного использования",
            comments="Discord, YouTube, SoundCloud — zapret (DPI).\n"
                     "Telegram — TgWsProxy (WebSocket).",
        )
        dlg.present(self)


class ObhodApp(Adw.Application):

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=0)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = ObhodWindow(self)
        win.present()


def main():
    return ObhodApp().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
