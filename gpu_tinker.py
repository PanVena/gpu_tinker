#!/usr/bin/env python3
"""GPU Switch (PySide6): стан відеокарт, монітори, процеси у VRAM і звільнення
обраної карти від робочого столу (KDE Plasma, Wayland).

Панель «Звільнити карту» має два незалежні чекбокси:
  1. Вимкнути монітори обраної карти — діє одразу, без виходу з сесії.
  2. Робочий стіл малювати на іншій карті — пише
     ~/.config/plasma-workspace/env/gpu-ai-mode.sh, після чого KWin
     (робочий стіл) на наступному вході в сесію малює на іншій карті.

Панель «Запустити гру» запускає обрану команду на обраній карті:
  - Vulkan-ігри — GPU встановлюється через VK_ICD_VENDOR=allow_amd|allow_nvidia;
  - OpenGL на NVIDIA — через __NV_PRIME_PROFILE (X11);
  - опційно вимикає монітори інших карт і повертає їх після завершення гри.

Процеси у VRAM: для AMD/Nouveau/Intel читаються з /proc/*/fdinfo
(без root видно лише власні процеси), для NVIDIA — через nvidia-smi
(усі процеси, включно з OpenGL/EGL-контекстами).

Залежності: python3-pyside6, pciutils (lspci), kscreen (kscreen-doctor).
Для NVIDIA додатково використовується nvidia-smi, якщо він є.
"""
import glob
import json
import os
import re
import time
import shlex
import shutil
import signal
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QGroupBox,
                                QHeaderView,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QProgressBar, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

DRM = "/sys/class/drm"
ENV_FILE = os.path.expanduser("~/.config/plasma-workspace/env/gpu-ai-mode.sh")
VENDORS = {"0x1002": "AMD", "0x10de": "NVIDIA", "0x8086": "Intel"}
REFRESH_MS = 2000
PROCS_MS = 4000
UNITS = {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}


# ---------------------------------------------------------------- утиліти
def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def norm_pci(s):
    """Нормалізувати PCI-адресу до формату DDDD:BB:DD.F, як у /sys/devices.

    Приймає адреси з lspci/nvidia-smi ('00000000:04:00.0'), drm-pdev
    ('0000:03:00.0' або '/dev/dri/by-path/pci-04:00.0-card')."""
    if not s:
        return None
    m = re.search(r"pci-([0-9a-fA-F]{1,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.(\d)", s)
    if not m:
        m = re.fullmatch(r"([0-9a-fA-F]{1,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.(\d)", s.strip())
    if not m:
        return None
    dom, bus, dev, fn = m.groups()
    try:
        return f"{int(dom, 16):04x}:{bus.lower()}:{dev.lower()}.{fn}"
    except ValueError:
        return None


def gpu_name(pci):
    parts = re.findall(r'"([^"]*)"', run(["lspci", "-s", pci, "-mm"]))
    return parts[2] if len(parts) >= 3 else pci


def short_name(full):
    """Коротка назва для комбо/заголовків: 'GM107GL [Quadro K620]' -> 'Quadro K620'."""
    m = re.search(r"\[([^\]]+)\]", full)
    if m:
        return m.group(1).split("/")[0].strip()
    return full


def nvidia_stats():
    """{'0000:04:00.0': {used,total,util,temp,power}}"""
    res = {}
    q = "pci.bus_id,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"
    for line in run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"]).splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 6:
            pci = norm_pci(p[0])
            if pci:
                res[pci] = {
                    "used": to_int(p[1]), "total": to_int(p[2]),
                    "util": to_float(p[3]), "temp": to_float(p[4]), "power": to_float(p[5])}
    return res


def nvidia_apps():
    """{pci: {pid: (назва, VRAM_МБ)}} — усі процеси NVIDIA з VRAM:
    обчислювальні (C/C+G) та графічні OpenGL/EGL-контексти (G)."""
    res = {}
    if not shutil.which("nvidia-smi"):
        return res
    idx_to_pci = {}
    for line in run(["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"]).splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 2:
            pci = norm_pci(p[1])
            if pci:
                idx_to_pci[p[0]] = pci
    row_re = re.compile(r"^\|\s*(\d+)\s+N/A\s+N/A\s+(\d+)\s+(?:C\+G|C|G)\s+(.+?)\s+([0-9]+)MiB\s*\|$")
    for line in run(["nvidia-smi"]).splitlines():
        m = row_re.match(line)
        if not m:
            continue
        idx, pid, name, used = m.groups()
        pci = idx_to_pci.get(idx)
        if not pci:
            continue
        mb = float(used)
        short = read(f"/proc/{pid}/comm", "") or name.split("/")[-1] or "?"
        d = res.setdefault(pci, {})
        prev = d.get(pid)
        d[pid] = (prev[0], prev[1] + mb) if prev else (short, mb)
    return res


def hwmon_value(dev, names, scale):
    for n in names:
        for path in glob.glob(f"{dev}/hwmon/hwmon*/{n}"):
            v = to_int(read(path))
            if v is not None:
                return v / scale
    return None


def scan():
    gpus, nv = [], None
    for card_path in sorted(glob.glob(f"{DRM}/card[0-9]*")):
        card = os.path.basename(card_path)
        if "-" in card:
            continue
        dev = os.path.realpath(f"{card_path}/device")
        pci = norm_pci(os.path.basename(dev)) or os.path.basename(dev)
        vendor_id = read(f"{dev}/vendor")
        vendor = VENDORS.get(vendor_id, vendor_id)
        driver = (os.path.basename(os.path.realpath(f"{dev}/driver"))
                  if os.path.exists(f"{dev}/driver") else "—")
        name = gpu_name(pci)
        g = {"card": card, "pci": pci, "vendor": vendor, "driver": driver,
             "name": name, "short": short_name(name),
             "used": None, "total": None, "util": None,
             "temp": None, "power": None,
             "rt": read(f"{dev}/power/runtime_status")}

        if os.path.exists(f"{dev}/mem_info_vram_total"):  # amdgpu
            u, t = to_int(read(f"{dev}/mem_info_vram_used")), to_int(read(f"{dev}/mem_info_vram_total"))
            if u is not None and t is not None:
                g["used"], g["total"] = u // 2**20, t // 2**20
            g["util"] = to_float(read(f"{dev}/gpu_busy_percent"))
            g["temp"] = hwmon_value(dev, ["temp1_input"], 1000)
            g["power"] = hwmon_value(dev, ["power1_average", "power1_input"], 1e6)
        elif vendor == "NVIDIA" and shutil.which("nvidia-smi"):
            if nv is None:
                nv = nvidia_stats()
            g.update(nv.get(pci, {}))

        conns = []
        for c in sorted(glob.glob(f"{DRM}/{card}-*")):
            if read(f"{c}/status") == "connected":
                conns.append((os.path.basename(c)[len(card) + 1:], read(f"{c}/enabled") == "enabled"))
        g["conns"] = conns
        gpus.append(g)
    return gpus


def vram_processes():
    """{pci: [(pid, назва, VRAM_МБ)]} для обох карт.

    amdgpu/nouveau/i915 — з /proc/*/fdinfo (без root видно лише власні процеси);
    NVIDIA — через nvidia-smi (усі: обчислювальні та OpenGL/EGL-контексти)."""
    per_pci = {}
    for pdir in glob.glob("/proc/[0-9]*"):
        pid = int(os.path.basename(pdir))
        try:
            fds = os.listdir(f"{pdir}/fdinfo")
        except OSError:
            continue
        seen = {}
        for fd in fds:
            try:
                with open(f"{pdir}/fdinfo/{fd}") as f:
                    txt = f.read()
            except OSError:
                continue
            if "drm-driver" not in txt:
                continue
            m_pdev = re.search(r"^drm-pdev:\s*(\S+)", txt, re.M)
            pci = norm_pci(m_pdev.group(1)) if m_pdev else None
            if not pci:
                continue
            m_id = re.search(r"^drm-client-id:\s*(\d+)", txt, re.M)
            m_v = re.search(r"^drm-memory-vram:\s*(\d+)\s*(\w+)?", txt, re.M)
            mb = 0.0
            if m_v:
                mb = int(m_v.group(1)) * UNITS.get(m_v.group(2) or "KiB", 1 / 1024)
            seen[(pci, m_id.group(1) if m_id else fd)] = mb
        for (pci, _), mb in seen.items():
            d = per_pci.setdefault(pci, {})
            d[pid] = d.get(pid, 0) + mb

    for pci, apps in nvidia_apps().items():
        d = per_pci.setdefault(pci, {})
        for pid, (name, mb) in apps.items():
            d[pid] = d.get(pid, 0) + mb

    out = {}
    for pci, d in per_pci.items():
        out[pci] = sorted(((int(pid), read(f"/proc/{pid}/comm", "?"), mb) for pid, mb in d.items()),
                          key=lambda x: -x[2])
    return out


def set_output(name, enable):
    try:
        r = subprocess.run(["kscreen-doctor", f"output.{name}.{'enable' if enable else 'disable'}"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr + r.stdout).strip()


# ------------------------------------------------------------ керування KWin
def kwin_env():
    """Значення KWIN_DRM_DEVICES у запущеному kwin_wayland ('' якщо не задано, None якщо kwin не знайдено)."""
    pids = run(["pidof", "kwin_wayland"]).split()
    if not pids:
        return None
    try:
        with open(f"/proc/{pids[0]}/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if item.startswith(b"KWIN_DRM_DEVICES="):
                    return item.split(b"=", 1)[1].decode()
    except OSError:
        return None
    return ""


def kwin_draw_pci():
    """PCI-адреса карти, на якій зараз малює KWin (None якщо не встановлено/не знайдено)."""
    env = kwin_env()
    if not env:
        return None
    for part in env.split(":"):
        pci = norm_pci(part)
        if pci:
            return pci
    return None


def configured_ai_pci():
    m = re.search(r"^# ai-gpu=(\S+)", read(ENV_FILE), re.M)
    return m.group(1) if m else None


def write_env(ai_pci, order):
    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    with open(ENV_FILE, "w") as f:
        f.write(
            "# Створено gpu_tinker.py. Видаліть цей файл, щоб повернути стандартну поведінку.\n"
            f"# ai-gpu={ai_pci}\n"
            f"for p in {' '.join(order)}; do\n"
            '  d=$(readlink -f "/dev/dri/by-path/pci-$p-card")\n'
            '  [ -e "$d" ] && L="${L:+$L:}$d"\n'
            "done\n"
            '[ -n "$L" ] && export KWIN_DRM_DEVICES="$L"\n'
            "unset L p d\n")


def logout():
    r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.kde.Shutdown",
                        "--object-path", "/Shutdown", "--method", "org.kde.Shutdown.logout"],
                       capture_output=True, text=True)
    if r.returncode != 0 and os.environ.get("XDG_SESSION_ID"):
        subprocess.run(["loginctl", "terminate-session", os.environ["XDG_SESSION_ID"]])


# ------------------------------------------------------------------ GUI
class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GPU Switch")
        self.resize(1180, 860)
        self.setMinimumSize(980, 780)
        self.data, self.widgets = [], {}
        self.sig = None
        self.ai_pci = None

        root = QVBoxLayout(self)
        top_row = QHBoxLayout()

        # ---- панель «звільнити карту»
        box = QGroupBox("Звільнити карту для обчислень")
        broot = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Карта, яку звільнити:"))
        self.combo = QComboBox()
        row.addWidget(self.combo, 1)
        broot.addLayout(row)

        self.chk_monitors = QCheckBox("Вимкнути монітори цієї карти")
        self.lbl_monitors = QLabel("Діє одразу, вихід із сесії не потрібен.")
        self.lbl_monitors.setObjectName("desc")
        self.chk_desktop = QCheckBox("Робочий стіл малювати на іншій карті (KWin переключить GPU)")
        self.lbl_desktop = QLabel("Почне діяти після виходу з сесії та повторного входу.")
        self.lbl_desktop.setObjectName("desc")
        for w in (self.chk_monitors, self.lbl_monitors, self.chk_desktop, self.lbl_desktop):
            broot.addWidget(w)
        btns = QHBoxLayout()
        self.btn_apply = QPushButton("Застосувати зміни")
        self.btn_reset = QPushButton("Скасувати: увімкнути всі монітори і видалити налаштування")
        btns.addWidget(self.btn_apply)
        btns.addWidget(self.btn_reset)
        broot.addLayout(btns)
        self.mode_label = QLabel()
        self.mode_label.setWordWrap(True)
        broot.addWidget(self.mode_label)
        top_row.addWidget(box, 1)

        # ---- панель запуску гри на обраній карті
        gbox = QGroupBox("Запустити гру на окремій відеокарті")
        gr = QVBoxLayout(gbox)
        grow = QHBoxLayout()
        self.edit_cmd = QLineEdit()
        self.edit_cmd.setPlaceholderText("Команда або шлях до гри, напр.: steam -app 1203960")
        grow.addWidget(QLabel("Команда:"))
        grow.addWidget(self.edit_cmd, 1)
        gr.addLayout(grow)
        grow2 = QHBoxLayout()
        grow2.addWidget(QLabel("Цільова карта:"))
        self.combo_game = QComboBox()
        grow2.addWidget(self.combo_game, 1)
        gr.addLayout(grow2)
        self.chk_other_off = QCheckBox(
            "Вимкнути монітори інших карт (гра буде видно лише на екранах цієї карти)")
        gr.addWidget(self.chk_other_off)
        glaunch = QHBoxLayout()
        self.btn_launch = QPushButton("Запустити гру")
        glaunch.addWidget(self.btn_launch)
        glaunch.addStretch()
        gr.addLayout(glaunch)
        gnote = QLabel(
            "Vulkan-ігри: GPU встановлюється через VK_ICD_VENDOR; OpenGL на NVIDIA — через "
            "__NV_PRIME_PROFILE (X11). Якщо чекбокс увімкнено, монітори інших карт вимкнуться, "
            "а після завершення гри повернуться автоматично.")
        gnote.setWordWrap(True)
        gnote.setObjectName("desc")
        gr.addWidget(gnote)
        top_row.addWidget(gbox, 1)
        root.addLayout(top_row)

        # ---- контейнер карток (біля-біля)
        self.container = QHBoxLayout()
        root.addLayout(self.container)

        # ---- процеси у VRAM
        pbox = QGroupBox("Процеси, що тримають VRAM (NVIDIA — усі, включно з OpenGL/EGL; "
                         "AMD — лише ваші без root)")
        proot = QVBoxLayout(pbox)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Карта", "Процес", "PID", "VRAM, МБ"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(230)
        self.tree.setColumnWidth(0, 150)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(2, 70)
        self.tree.setColumnWidth(3, 90)
        proot.addWidget(self.tree)
        brow = QHBoxLayout()
        self.btn_root = QPushButton("Повний список (root — діалог KDE)")
        brow.addWidget(self.btn_root)
        brow.addStretch(1)
        self.btn_kill = QPushButton("Завершити вибраний процес")
        brow.addWidget(self.btn_kill)
        proot.addLayout(brow)
        root.addWidget(pbox, 1)

        # ---- статусний рядок
        self.status = QLabel()
        self.status.setObjectName("status")
        root.addWidget(self.status)

        self.btn_apply.clicked.connect(self.apply_changes)
        self.btn_reset.clicked.connect(self.reset_all)
        self.btn_kill.clicked.connect(self.kill_selected)
        self.btn_root.clicked.connect(self.do_root_scan)
        self.btn_launch.clicked.connect(self.launch_game)
        self.combo.currentIndexChanged.connect(self.on_combo)
        self.combo_game.currentIndexChanged.connect(self.on_combo_game)

        self.game_pci = None
        self.game_proc = None
        self.restore_after_game = False
        self.root_procs = None
        self.root_ts = 0.0

        self._theme = self._build_theme()
        self.setStyleSheet(self._qss())

        if not shutil.which("kscreen-doctor"):
            QMessageBox.warning(self, "kscreen-doctor не знайдено",
                                "Без kscreen-doctor монітори не вимикатимуться.\n"
                                "Встановіть: sudo dnf install kscreen")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(REFRESH_MS)
        self.timer_procs = QTimer(self)
        self.timer_procs.timeout.connect(self.refresh_procs)
        self.timer_procs.start(PROCS_MS)
        self.tick()
        self.refresh_procs()

    # ---- тема (кольори з палітри системи)
    def _build_theme(self):
        pal = self.palette()
        win = pal.color(QPalette.ColorRole.Window)
        mid = pal.color(QPalette.ColorRole.Mid)
        midlight = pal.color(QPalette.ColorRole.Midlight)
        text = pal.color(QPalette.ColorRole.Text)
        hl = pal.color(QPalette.ColorRole.Highlight)
        hl_text = pal.color(QPalette.ColorRole.HighlightedText)
        dark = (win.red() + win.green() + win.blue()) / 3 < 128
        panel_bg = win.lighter(108) if dark else win.darker(107)
        return {
            "dark": dark,
            "border": mid.name(),          # рамки — системний колір
            "panel_bg": panel_bg.name(),
            "muted": midlight.name(),
            "text": text.name(),
            "highlight": hl.name(),        # підсвітка — системний Highlight
            "hl_text": hl_text.name(),
            "ok": "#81c995" if dark else "#2e7d32",
            "warn": "#ffab40" if dark else "#e6890b",
            "err": "#ff6e6e" if dark else "#c62828",
            "free": "#4fc3f7" if dark else "#1565c0",
            "game": "#ffb74d" if dark else "#ef6c00",
        }

    def _qss(self):
        t = self._theme
        return f"""
        QGroupBox {{
            border: 1px solid {t['border']};
            border-radius: 8px;
            margin-top: 12px;
            padding: 12px 10px 10px 10px;
        }}
        QGroupBox::title {{
            subcontrol-origin: padding;
            subcontrol-position: top left;
            left: 14px;
            padding: 2px 8px;
            background: {t['panel_bg']};
            border-radius: 5px;
            color: {t['text']};
        }}
        QGroupBox[role="free"]::title {{ color: {t['free']}; font-weight: bold; }}
        QGroupBox[role="game"]::title {{ color: {t['game']}; font-weight: bold; }}
        QGroupBox[selected="true"] {{ border: 2px solid {t['highlight']}; }}
        QGroupBox[selected="true"]::title {{ color: {t['highlight']}; }}
        QLabel#desc {{ color: {t['muted']}; font-size: 12px; }}
        QLabel#status {{ font-weight: bold; }}
        QTreeWidget {{
            alternate-background-color: {t['panel_bg']};
            gridline-color: {t['border']};
        }}
        QTreeView::item:selected {{ background: {t['highlight']}; color: {t['hl_text']}; }}
        QProgressBar {{
            border: 1px solid {t['border']};
            border-radius: 5px;
            text-align: center;
            min-height: 18px;
        }}
        """

    def set_status(self, text, kind=None):
        t = self._theme
        color = {"ok": t["ok"], "warn": t["warn"], "err": t["err"]}.get(kind, t["muted"])
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    # ---- оновлення
    def tick(self):
        self.watch_game()
        self.apply(scan())

    def watch_game(self):
        """Якщо запущена з програмою гра завершилася — повернути монітори."""
        if self.game_proc is None:
            return
        rc = self.game_proc.poll()
        if rc is None:
            return
        self.game_proc = None
        restore, self.restore_after_game = self.restore_after_game, False
        if restore:
            for g in self.data:
                self.change([n for n, e in g["conns"] if not e], True)
            self.status.setText(f"Гру завершено (код {rc}), монітори повернено")
        else:
            self.status.setText(f"Гру завершено (код {rc})")

    def apply(self, data):
        self.data = data
        sig = tuple((g["card"], tuple(c[0] for c in g["conns"])) for g in data)
        if sig != self.sig:
            self.rebuild()
            self.sig = sig
            self.fill_combo()
        ai = next((g for g in data if g["pci"] == self.ai_pci), None)
        for g in data:
            w = self.widgets[g["card"]]
            w["info"].setText(f"{g['name']} · {g['vendor']} · драйвер: {g['driver']} · PCI {g['pci']}")
            if g["used"] is not None and g["total"]:
                pct = 100 * g["used"] / g["total"]
                w["vram"].setText(f"VRAM: {g['used']} / {g['total']} МБ ({pct:.0f}%)")
                w["bar"].setValue(int(pct))
            else:
                w["vram"].setText("VRAM: недоступно")
                w["bar"].setValue(0)
            parts = []
            if g["util"] is not None:
                parts.append(f"навантаження {g['util']:.0f}%")
            if g["temp"] is not None:
                parts.append(f"{g['temp']:.0f} °C")
            if g["power"] is not None:
                parts.append(f"{g['power']:.0f} Вт")
            if g["rt"]:
                parts.append(f"стан: {g['rt']}")
            w["metrics"].setText(" · ".join(parts) or "метрики недоступні")
            for cname, enabled in g["conns"]:
                cw = w["conns"][cname]
                cw["state"].setText("увімкнено" if enabled else "вимкнено")
                cw["state"].setStyleSheet(
                    f"color: {'#2e7d32' if enabled else '#b71c1c'};")
                cw["btn"].setText("Вимкнути" if enabled else "Увімкнути")
        self.sync_checks(ai)
        self.update_mode_label()

    def rebuild(self):
        while self.container.count():
            item = self.container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.widgets = {}
        for g in self.data:
            card = g["card"]
            box = QGroupBox(f"{g['short']} ({card})")
            v = QVBoxLayout(box)
            w = {"conns": {}}
            w["info"] = QLabel()
            v.addWidget(w["info"])
            w["vram"] = QLabel()
            v.addWidget(w["vram"])
            w["bar"] = QProgressBar()
            w["bar"].setMaximum(100)
            v.addWidget(w["bar"])
            w["metrics"] = QLabel()
            v.addWidget(w["metrics"])
            if not g["conns"]:
                v.addWidget(QLabel("Підключених моніторів немає"))
            for cname, _ in g["conns"]:
                row = QHBoxLayout()
                lbl = QLabel(cname)
                lbl.setMinimumWidth(130)
                state = QLabel()
                state.setMinimumWidth(110)
                btn = QPushButton()
                btn.clicked.connect(lambda _=False, n=cname: self.toggle_one(n))
                row.addWidget(lbl)
                row.addWidget(state)
                row.addStretch()
                row.addWidget(btn)
                v.addLayout(row)
                w["conns"][cname] = {"state": state, "btn": btn}
            if g["conns"]:
                bar = QHBoxLayout()
                b1 = QPushButton("Вимкнути всі виходи карти")
                b1.clicked.connect(lambda _=False, c=card: self.toggle_card(c, False))
                b2 = QPushButton("Увімкнути всі")
                b2.clicked.connect(lambda _=False, c=card: self.toggle_card(c, True))
                bar.addWidget(b1)
                bar.addWidget(b2)
                v.addLayout(bar)
            w["box"] = box
            self.widgets[card] = w
            self.container.addWidget(box, 1)

    def fill_combo(self):
        def item(g):
            vram = f" · {g['total'] // 1024} ГБ" if g["total"] else ""
            return f"{g['short']}{vram} ({g['card']})"

        self.combo_map = [(g["pci"], item(g)) for g in self.data]
        pcis = [p for p, _ in self.combo_map]
        if self.ai_pci not in pcis:
            saved = configured_ai_pci()
            if saved in pcis:
                self.ai_pci = saved
            elif self.data:
                self.ai_pci = max(self.data, key=lambda g: g["total"] or 0)["pci"]
        for combo, attr in ((self.combo, "ai_pci"), (self.combo_game, "game_pci")):
            combo.blockSignals(True)
            combo.clear()
            for _, t in self.combo_map:
                combo.addItem(t)
            val = getattr(self, attr)
            if val not in pcis:
                setattr(self, attr, self.ai_pci)
                val = self.ai_pci
            if val in pcis:
                combo.setCurrentIndex(pcis.index(val))
            combo.blockSignals(False)

    def on_combo(self, idx):
        if 0 <= idx < len(getattr(self, "combo_map", [])):
            self.ai_pci = self.combo_map[idx][0]

    def on_combo_game(self, idx):
        if 0 <= idx < len(getattr(self, "combo_map", [])):
            self.game_pci = self.combo_map[idx][0]

    def sync_checks(self, ai):
        """Чекбокси відображають фактичний стан системи."""
        if ai and ai["conns"]:
            self.chk_monitors.setChecked(not any(e for _, e in ai["conns"]))
        else:
            self.chk_monitors.setChecked(False)
        self.chk_desktop.setChecked(
            os.path.exists(ENV_FILE) and configured_ai_pci() == (ai["pci"] if ai else None))

    def update_mode_label(self):
        ai = next((g for g in self.data if g["pci"] == self.ai_pci), None)
        ai_name = ai["short"] if ai else "обрана карта"
        draw = kwin_draw_pci()
        cfg = os.path.exists(ENV_FILE) and configured_ai_pci() == (ai["pci"] if ai else None)
        if ai and draw and draw != ai["pci"]:
            other = next((g for g in self.data if g["pci"] == draw), None)
            txt = f"Робочий стіл малює на {other['short'] if other else draw}. " \
                  f"{ai_name} вільна для обчислень."
            if not cfg:
                txt += " Файл налаштувань видалено — після наступного входу режим буде стандартним."
        elif cfg and ai:
            txt = (f"Після перелогіну робочий стіл малюватиме на іншій карті; у цій сесії "
                   f"він досі на {ai_name}. Потрібен вихід і повторний вхід.")
        else:
            txt = "Стандартний режим: робочий стіл малює на головній карті, яку обрала система."
        self.mode_label.setText(txt)

    ROOT_TTL = 300  # с, протягом яких результат root-сканування вважається свіжим

    def _root_scan(self):
        """Сканує процеси через pkexec (вбудований діалог KDE). None — не вдалося."""
        if not shutil.which("pkexec"):
            self.set_status("pkexec не знайдено", "warn")
            return None
        self.set_status("Очікуємо підтвердження у діалозі KDE…")
        try:
            r = subprocess.run(["pkexec", sys.executable, os.path.abspath(__file__), "--procs-json"],
                              capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            self.set_status("Не вдалося запустити root-сканування", "warn")
            return None
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()
            msg = err[0][:120] if err else "pkexec повернув помилку"
            self.set_status(f"Root-сканування не виконано ({msg})", "warn")
            return None
        try:
            data = json.loads(r.stdout)
        except ValueError:
            self.set_status("Не вдалося прочитати результат root-сканування", "warn")
            return None
        self.root_procs, self.root_ts = data, time.monotonic()
        self.set_status(f"Повний список отримано (root), свіжий {self.ROOT_TTL // 60} хв.", "ok")
        return data

    def do_root_scan(self):
        if self._root_scan():
            self.refresh_procs()

    def refresh_procs(self):
        if self.root_procs is not None and time.monotonic() - self.root_ts < self.ROOT_TTL:
            procs = self.root_procs
        else:
            procs = vram_processes()
        names = {g["pci"]: g["short"] for g in self.data}
        sel = self.tree.currentItem()
        sel_key = sel.text(2) if sel else None  # PID — третя колонка
        self.tree.clear()
        for pci, lst in procs.items():
            for pid, name, mb in lst:
                if mb < 1:  # шум: fd відкритий, але VRAM фактично нульовий
                    continue
                item = QTreeWidgetItem([names.get(pci, pci), name, str(pid), f"{mb:.0f}"])
                item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight)
                item.setTextAlignment(3, Qt.AlignmentFlag.AlignRight)
                self.tree.addTopLevelItem(item)
                if sel_key and item.text(2) == sel_key:
                    self.tree.setCurrentItem(item)
        # VRAM, що не вдається атрибутивати до видимих процесів
        root_on = (self.root_procs is not None and time.monotonic() - self.root_ts < self.ROOT_TTL)
        for g in self.data:
            pci = g["pci"]
            if g["used"] is None:
                continue
            listed = sum(mb for _, _, mb in procs.get(pci, []))
            diff = g["used"] - listed
            if diff > 8:
                if not root_on and g["vendor"] != "NVIDIA":
                    note = "інші (не видно без root)"
                else:
                    note = "не атрибутовано (ядро/драйвер)"
                item = QTreeWidgetItem([names.get(pci, pci), note, "—", f"{diff:.0f}"])
                item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight)
                item.setTextAlignment(3, Qt.AlignmentFlag.AlignRight)
                self.tree.addTopLevelItem(item)

    # ---- дії над моніторами
    def enabled_outputs(self):
        return [n for g in self.data for n, e in g["conns"] if e]

    def toggle_one(self, name):
        for g in self.data:
            for n, e in g["conns"]:
                if n == name:
                    self.change([name], not e)
                    return

    def toggle_card(self, card, enable):
        for g in self.data:
            if g["card"] == card:
                self.change([n for n, e in g["conns"] if e != enable], enable)

    def change(self, names, enable):
        if not names:
            return True
        if not enable and not [n for n in self.enabled_outputs() if n not in names]:
            QMessageBox.critical(self, "Не можна",
                                 "Це вимкнуло б усі монітори. Залиште хоча б один увімкненим.")
            return False
        errors = []
        for n in names:
            ok, msg = set_output(n, enable)
            if not ok:
                errors.append(f"{n}: {msg or 'помилка'}")
        self.status.setText(("Помилка: " + "; ".join(errors)) if errors
                            else f"{'Увімкнено' if enable else 'Вимкнено'}: {', '.join(names)}")
        QTimer.singleShot(800, lambda: self.apply(scan()))
        return not errors

    # ---- застосування чекбоксів
    def apply_changes(self):
        ai = next((g for g in self.data if g["pci"] == self.ai_pci), None)
        if not ai:
            return
        want_mon_off = self.chk_monitors.isChecked()
        want_desktop = self.chk_desktop.isChecked()
        env_changed = False

        cur_all_off = not any(e for _, e in ai["conns"])
        if ai["conns"] and want_mon_off != cur_all_off:
            names = [n for n, e in ai["conns"] if e == want_mon_off]
            self.change(names, not want_mon_off)

        cfg = os.path.exists(ENV_FILE) and configured_ai_pci() == ai["pci"]
        if want_desktop and not cfg:
            others = [g for g in self.data if g is not ai]
            if not any(g["conns"] for g in others):
                QMessageBox.critical(self, "Не можна",
                                     "До інших карт не підключено жодного монітора, "
                                     "тож робочому столу нема де малювати.")
                return
            order = [g["pci"] for g in sorted(others, key=lambda g: g["total"] or 0)] + [ai["pci"]]
            write_env(ai["pci"], order)
            env_changed = True
        elif not want_desktop and os.path.exists(ENV_FILE):
            try:
                os.remove(ENV_FILE)
            except FileNotFoundError:
                pass
            env_changed = True

        self.data = scan()
        self.apply(self.data)
        if env_changed:
            self.status.setText("Налаштування записано.")
            if QMessageBox.question(
                    self, "Перелогін",
                    "Щоб робочий стіл перемалювався на іншу карту, потрібен вихід з сесії. "
                    "Спершу збережіть роботу у відкритих програмах. Вийти зараз?"):
                logout()

    def reset_all(self):
        if QMessageBox.question(
                self, "Скасувати",
                "Увімкнути всі монітори і видалити файл налаштувань?") != QMessageBox.StandardButton.Yes:
            return
        try:
            os.remove(ENV_FILE)
        except FileNotFoundError:
            pass
        for g in self.data:
            self.change([n for n, e in g["conns"] if not e], True)
        self.data = scan()
        self.apply(self.data)
        if QMessageBox.question(
                self, "Перелогін",
                "Щоб робочий стіл повернувся на головну карту, потрібен вихід з сесії. Вийти зараз?"):
            logout()

    # ---- запуск гри на обраній карті
    def launch_game(self):
        cmd_text = self.edit_cmd.text().strip()
        if not cmd_text:
            return
        g = next((g for g in self.data if g["pci"] == self.game_pci), None)
        if not g:
            return
        if self.chk_other_off.isChecked():
            mine_off = [n for n, e in g["conns"] if not e]
            others_on = [n for gg in self.data if gg is not g for n, e in gg["conns"] if e]
            if not any(e for _, e in g["conns"]) and not mine_off:
                QMessageBox.critical(self, "Не можна",
                                     "До цієї карти не підключено жодного монітора.")
                return
            self.change(mine_off, True)
            if not self.change(others_on, False):
                return
        env = dict(os.environ)
        vendor = g["vendor"].lower()
        if vendor in ("amd", "nvidia"):
            env["VK_ICD_VENDOR"] = f"allow_{vendor}"
        if g["vendor"] == "NVIDIA":
            env["__NV_PRIME_PROFILE"] = "pci:" + ":".join(g["pci"].split(":")[1:])
        try:
            self.game_proc = subprocess.Popen(shlex.split(cmd_text), env=env, start_new_session=True)
        except OSError as e:
            self.status.setText(f"Не вдалося запустити гру: {e}")
            return
        self.restore_after_game = self.chk_other_off.isChecked()
        self.status.setText(f"Гру запущено на {g['name']} (PID {self.game_proc.pid})")

    def kill_selected(self):
        item = self.tree.currentItem()
        if not item:
            return
        pid, name = item.text(2), item.text(1)
        if QMessageBox.question(self, "Завершити процес",
                                f"Завершити {name} (PID {pid})?\nНезбережені дані буде втрачено.") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            os.kill(int(pid), signal.SIGTERM)
            self.status.setText(f"Надіслано SIGTERM процесу {name} ({pid})")
        except OSError as e:
            self.status.setText(f"Не вдалося завершити {name}: {e}")
        QTimer.singleShot(1500, self.refresh_procs)


if __name__ == "__main__":
    if "--procs-json" in sys.argv:
        # Режим для root-сканування (pkexec): виводить JSON процесів у VRAM і завершується.
        print(json.dumps(vram_processes()))
        sys.exit(0)
    app = QApplication(sys.argv)
    w = App()
    w.show()
    sys.exit(app.exec())
