#!/usr/bin/env python3
"""GPU Switch (PySide6): стан відеокарт, монітори, процеси у VRAM і звільнення
обраної карти від робочого столу (KDE Plasma, Wayland).

Панелі карток показують активні режими моніторів;
AMD — керування вентилятором, NVIDIA — ліміт потужності (root — вбуданий діалог KDE),
Intel — метрики через intel_gpu_top (якщо встановлено; без root часто потрібен
CAP_PERFMON/paranoid<2, тоді — вбуданий діалог KDE).

Панель «Звільнити карту» має два незалежні чекбокси:
  1. Вимкнути монітори обраної карти — діє одразу, без виходу з сесії.
  2. Робочий стіл малювати на іншій карті — пише
     ~/.config/plasma-workspace/env/gpu-ai-mode.sh, після чого KWin
     (робочий стіл) на наступному вході в сесію малює на іншій карті.

Процеси у VRAM: для AMD/Nouveau/Intel читаються з /proc/*/fdinfo
(без root видно лише власні процеси; для i915 — resident-пам'ять по регіонах
system/stolen), для NVIDIA — через nvidia-smi (усі процеси, включно з
OpenGL/EGL-контекстами). Завантаження процесів: DRM engine-time з fdinfo,
для NVIDIA — SM utilization через nvidia-smi pmon.

Залежності: python3-pyside6, pciutils (lspci), kscreen (kscreen-doctor).
Для NVIDIA додатково використовується nvidia-smi, якщо він є.
Для Intel метрик — intel_gpu_top (пакет intel-gpu-tools), якщо встановлено.
При запусці програма питає про відсутні інструменти і може встановити обране
через пакетний менеджер (root — діалог KDE).
"""
import glob
import json
import os
import re
import time
import shutil
import signal
import subprocess
import sys

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                                QGroupBox,
                               QHeaderView,
                               QHBoxLayout, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QSlider, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

DRM = "/sys/class/drm"
ENV_FILE = os.path.expanduser("~/.config/plasma-workspace/env/gpu-ai-mode.sh")
VENDORS = {"0x1002": "AMD", "0x10de": "NVIDIA", "0x8086": "Intel"}
REFRESH_MS = 2000
PROCS_MS = 4000
INTEL_MS = 5000
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


def drm_card_pci(n):
    """/dev/dri/cardN -> PCI-адреса (останній сегмент realpath /sys/class/drm/cardN/device)."""
    try:
        real = os.path.realpath(f"/sys/class/drm/card{n}/device")
    except OSError:
        return None
    return norm_pci(real.rsplit("/", 1)[-1])


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
    """{'0000:04:00.0': {name,used,total,util,temp,power,pwr_limit,pwr_min,pwr_max}}"""
    res = {}
    q = ("pci.bus_id,name,memory.used,memory.total,utilization.gpu,temperature.gpu,"
         "power.draw,power.limit,power.min_limit,power.max_limit")
    for line in run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"]).splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 10:
            pci = norm_pci(p[0])
            if pci:
                res[pci] = {
                    "name": p[1], "used": to_int(p[2]), "total": to_int(p[3]),
                    "util": to_float(p[4]), "temp": to_float(p[5]), "power": to_float(p[6]),
                    "pwr_limit": to_float(p[7]), "pwr_min": to_float(p[8]),
                    "pwr_max": to_float(p[9])}
    return res


def nvidia_index(pci):
    """Індекс GPU для nvidia-smi --id за PCI-адресою."""
    for line in run(["nvidia-smi", "--query-gpu=index,pci.bus_id",
                     "--format=csv,noheader"]).splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 2 and norm_pci(p[1]) == pci:
            return p[0]
    return None


def nvidia_pci_map():
    """{індекс nvidia-smi: PCI-адреса}."""
    res = {}
    for line in run(["nvidia-smi", "--query-gpu=index,pci.bus_id",
                     "--format=csv,noheader"]).splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 2:
            pci = norm_pci(p[1])
            if pci:
                res[p[0]] = pci
    return res


def kscreen_modes():
    """{назва виходу: активний режим, напр. '2560×1440@60'} через kscreen-doctor -o."""
    out = run(["kscreen-doctor", "-o"], timeout=10)
    if not out:
        return {}
    txt = re.sub(r"\x1b\[[0-9;]*m", "", out)
    res, name = {}, None
    for line in txt.splitlines():
        s = line.strip()
        m = re.match(r"Output:\s*\d+\s+(\S+)", s)
        if m:
            name = m.group(1)
            continue
        if name and s.startswith("Modes:"):
            mm = re.search(r"(\d+)x(\d+)@([\d.]+)!", s)
            if mm:
                res[name] = f"{mm.group(1)}×{mm.group(2)}@{int(round(float(mm.group(3))))}"
    return res


def nvidia_apps():
    """{pci: {pid: (назва, VRAM_МБ)}} — усі процеси NVIDIA з VRAM:
    обчислювальні (C/C+G) та графічні OpenGL/EGL-контексти (G)."""
    res = {}
    if not shutil.which("nvidia-smi"):
        return res
    idx_to_pci = nvidia_pci_map()
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


def amdgpu_names():
    """{pci: точна назва GPU} через amdgpu_top -d -J (якщо встановлено)."""
    res = {}
    if not shutil.which("amdgpu_top"):
        return res
    try:
        r = subprocess.run(["amdgpu_top", "-d", "-J"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return res
    if r.returncode != 0:
        return res
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return res
    for dev in data if isinstance(data, list) else [data]:
        if not isinstance(dev, dict):
            continue
        pci = norm_pci(str(dev.get("PCI", "")))
        name = dev.get("DeviceName")
        if pci and name:
            res[pci] = str(name)
    return res


def intel_top_cmd(card, n=True, secs=8):
    """Команда для одного зразка `intel_gpu_top -J` карти card.

    -n 1 — чистий вихід після одного зразка (коректний JSON); старі версії
    не мають -n, тоді timeout обриває стрим і парситься перший повний об'єкт."""
    top = shutil.which("intel_gpu_top")
    t = shutil.which("timeout") or "/usr/bin/timeout"
    cmd = [t, str(secs), top, "-J", "-d", f"drm:/dev/dri/{card}"]
    if n:
        cmd += ["-n", "1"]
    return cmd


def intel_sample(cmd):
    """Перший JSON-зразок `intel_gpu_top -J` як dict; None — не вдалося."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout or ""
    i = out.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(out[i:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def intel_metrics(sample):
    """{'util': %, 'freq': МГц, 'power': Вт, 'bw': (read, write, unit)} з JSON-зразка."""
    m = {}
    busy = [to_float(e.get("busy")) for e in (sample.get("engines") or {}).values()
            if isinstance(e, dict)]
    busy = [b for b in busy if b is not None]
    if busy:
        m["util"] = max(busy)
    f = sample.get("frequency") or {}
    freq = to_float(f.get("actual"))
    if freq is None:
        freq = to_float(f.get("requested"))
    if freq is not None:
        m["freq"] = freq
    p = sample.get("power") or {}
    pw = to_float(p.get("GPU"))
    if pw is None:
        pw = to_float(p.get("Package"))
    if pw is not None:
        m["power"] = pw
    imc = sample.get("imc-bandwidth") or {}
    rd, wr = to_float(imc.get("reads")), to_float(imc.get("writes"))
    unit = str(imc.get("unit") or "").strip()
    if rd is not None and wr is not None:
        m["bw"] = (rd, wr, unit)
    return m


def hwmon_value(dev, names, scale):
    for n in names:
        for path in glob.glob(f"{dev}/hwmon/hwmon*/{n}"):
            v = to_int(read(path))
            if v is not None:
                return v / scale
    return None


def scan():
    gpus, nv, modes = [], None, kscreen_modes()
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
        if vendor == "AMD":  # lspci дає неоднозначну назву (XT/XTX/GRE), беремо точну
            name = amdgpu_names().get(pci, name)
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
            g["fan"] = hwmon_value(dev, ["fan1_input"], 1)
            g["fan_max"] = hwmon_value(dev, ["fan1_max"], 1)
            g["fan_target"] = hwmon_value(dev, ["fan1_target"], 1)
        elif vendor == "NVIDIA" and shutil.which("nvidia-smi"):
            if nv is None:
                nv = nvidia_stats()
            st = nv.get(pci, {})
            g.update(st)
            if st.get("name"):  # точна назва від nvidia-smi
                g["short"] = short_name(st["name"])

        conns = []
        for c in sorted(glob.glob(f"{DRM}/{card}-*")):
            if read(f"{c}/status") == "connected":
                conns.append((os.path.basename(c)[len(card) + 1:], read(f"{c}/enabled") == "enabled"))
        g["conns"] = conns
        g["modes"] = modes
        gpus.append(g)
    return gpus


def vram_processes(with_engine_times=False):
    """Процеси у VRAM; за потреби повертає також накопичені часи GPU-движків.

    amdgpu/nouveau — з /proc/*/fdinfo (drm-memory-vram); i915 — resident-пам'ять
    по регіонах (system/stolen/local); без root видно лише власні процеси;
    NVIDIA — через nvidia-smi (усі: обчислювальні та OpenGL/EGL-контексти)."""
    per_pci = {}
    process_engine_times = {}
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
            else:
                # i915: drm-memory-vram немає — пам'ять лічується по регіонах (system/stolen/local)
                for m_r in re.finditer(r"^drm-resident-\S+:\s*(\d+)\s*(\w+)?", txt, re.M):
                    mb += int(m_r.group(1)) * UNITS.get(m_r.group(2) or "KiB", 1 / 1024)
            engine_times = {}
            for m_engine in re.finditer(
                    r"^drm-engine-([^\s:]+):\s*(\d+)(?:\s*(\w+))?", txt, re.M):
                unit = m_engine.group(3) or "ns"
                if unit in ("ns", "nsec"):
                    engine_times[m_engine.group(1)] = int(m_engine.group(2))
            seen[(pci, m_id.group(1) if m_id else fd)] = (mb, engine_times)
        for (pci, _), (mb, engine_times) in seen.items():
            d = per_pci.setdefault(pci, {})
            d[pid] = d.get(pid, 0) + mb
            engines = process_engine_times.setdefault((pci, pid), {})
            for engine, elapsed_ns in engine_times.items():
                engines[engine] = engines.get(engine, 0) + elapsed_ns

    for pci, apps in nvidia_apps().items():
        d = per_pci.setdefault(pci, {})
        for pid, (name, mb) in apps.items():
            d[pid] = d.get(pid, 0) + mb

    out = {}
    for pci, d in per_pci.items():
        out[pci] = sorted(((int(pid), read(f"/proc/{pid}/comm", "?"), mb) for pid, mb in d.items()),
                          key=lambda x: -x[2])
    return (out, process_engine_times) if with_engine_times else out


_PROCESS_ENGINE_PREVIOUS = {}
_PROCESS_ENGINE_SAMPLE_AT = None


def process_engine_utilization(engine_times, now=None):
    """Процесний % GPU за приростом DRM engine-time між двома зразками."""
    global _PROCESS_ENGINE_PREVIOUS, _PROCESS_ENGINE_SAMPLE_AT
    now = time.monotonic() if now is None else now
    previous, previous_at = _PROCESS_ENGINE_PREVIOUS, _PROCESS_ENGINE_SAMPLE_AT
    _PROCESS_ENGINE_PREVIOUS = engine_times
    _PROCESS_ENGINE_SAMPLE_AT = now
    if previous_at is None:
        return {}
    interval_ns = (now - previous_at) * 1_000_000_000
    if interval_ns <= 0:
        return {}

    utilization = {}
    for key, engines in engine_times.items():
        old_engines = previous.get(key)
        if old_engines is None:
            continue
        deltas = [value - old_engines[engine] for engine, value in engines.items()
                  if engine in old_engines]
        if not deltas or any(delta < 0 for delta in deltas):
            continue
        utilization[key] = min(100.0, sum(deltas) * 100 / interval_ns)
    return utilization


def nvidia_process_utilization(output, idx_to_pci):
    """{(pci, pid): SM utilization %} з одного зразка `nvidia-smi pmon`."""
    utilization = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        pci = idx_to_pci.get(fields[0])
        sm = to_float(fields[3])
        if pci and sm is not None:
            key = (pci, int(fields[1]))
            utilization[key] = max(utilization.get(key, 0.0), min(100.0, sm))
    return utilization


def set_output(name, enable):
    try:
        r = subprocess.run(["kscreen-doctor", f"output.{name}.{'enable' if enable else 'disable'}"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr + r.stdout).strip()


# ------------------------------------------------------------ керування KWin
def kwin_env():
    """Значення KWIN_DRM_DEVICES у сесії ('' якщо не задано, None якщо не знайдено)."""
    for pid in run(["pidof", "kwin_wayland"]).split():
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                for item in f.read().split(b"\0"):
                    if item.startswith(b"KWIN_DRM_DEVICES="):
                        return item.split(b"=", 1)[1].decode()
        except OSError:
            continue
    v = os.environ.get("KWIN_DRM_DEVICES")
    if v:
        return v
    me = os.getuid()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != me:
                continue
            with open(f"/proc/{name}/environ", "rb") as f:
                for item in f.read().split(b"\0"):
                    if item.startswith(b"KWIN_DRM_DEVICES="):
                        return item.split(b"=", 1)[1].decode()
        except OSError:
            continue
    return None


def kwin_draw_pci():
    """PCI-адреса карти, на якій зараз малює KWin (None якщо не встановлено/не знайдено)."""
    env = kwin_env()
    if not env:
        return None
    for part in env.split(":"):
        m = re.fullmatch(r"/dev/dri/card(\d+)", part.strip())
        pci = drm_card_pci(m.group(1)) if m else norm_pci(part)
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


def missing_deps(gpus):
    """[(пакет, інструмент, опис)] — не встановлені інструменти, без яких
    функціональність програми обмежена (залежно від виявлених карт)."""
    deps = []
    if not shutil.which("kscreen-doctor"):
        deps.append(("kscreen", "kscreen-doctor", "вмикання/вимикання моніторів"))
    if not shutil.which("lspci"):
        deps.append(("pciutils", "lspci", "назви GPU"))
    for g in gpus:
        if g["vendor"] == "NVIDIA" and not shutil.which("nvidia-smi"):
            deps.append(("nvidia", "nvidia-smi", "метрики та процеси NVIDIA (пропрієтарний драйвер)"))
            break
    for g in gpus:
        if g["vendor"] == "Intel" and not shutil.which("intel_gpu_top"):
            deps.append(("intel-gpu-tools", "intel_gpu_top", "метрики Intel"))
            break
    return deps


class DepsDialog(QDialog):
    """Стартовий діалог: вибір, які відсутні інструменти встановити."""

    def __init__(self, parent, deps):
        super().__init__(parent)
        self.setWindowTitle("Відсутні інструменти")
        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "Не встановлено інструменти, без яких функціональність обмежена\n"
            "(монітори, метрики GPU, процеси у VRAM). Оберіть, що встановити:"))
        self.checks = {}
        for pkg, tool, desc in deps:
            chk = QCheckBox(f"{tool} — {desc} (пакет: {pkg})")
            chk.setChecked(True)
            self.checks[pkg] = chk
            root.addWidget(chk)
        row = QHBoxLayout()
        btn = QPushButton("Встановити обране (root — діалог KDE)")
        btn.clicked.connect(self.accept)
        row.addWidget(btn)
        row.addStretch(1)
        cancel = QPushButton("Скасувати")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        root.addLayout(row)

    def selected(self):
        return [p for p, c in self.checks.items() if c.isChecked()]


def install_packages(pkgs):
    """Встановити пакети через пакетний менеджер (root — діалог KDE). True/False."""
    pm, args = None, []
    for name, a in (("dnf", ["install", "-y"]), ("yum", ["install", "-y"]),
                    ("apt-get", ["install", "-y"]),
                    ("zypper", ["install", "--non-interactive"])):
        if shutil.which(name):
            pm, args = name, a
            break
    if not pm:
        QMessageBox.critical(None, "Не вдалося встановити",
                             "Не знайдено жодного пакетного менеджера (dnf/yum/apt/zypper).")
        return False
    cmd = ["pkexec", pm] + args + pkgs
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        QMessageBox.critical(None, "Не вдалося встановити", str(e))
        return False
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if not err:  # відмова/скасування у діалозі KDE — без текстової помилки
            QMessageBox.information(None, "Скасовано", "Встановлення скасовано.")
        else:
            tail = "\n".join(err.splitlines()[-5:])
            QMessageBox.critical(None, "Не вдалося встановити", f"{pm} повернула помилку:\n{tail}")
        return False
    QMessageBox.information(None, "Установлено", "Установлено: " + ", ".join(pkgs))
    return True


# ------------------------------------------------------------------ GUI
class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GPU Switch")
        self.resize(1280, 700)
        self.setMinimumSize(1000, 690)
        self.data, self.widgets = [], {}
        self.sig = None
        self.ai_pci = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ---- панель «звільнити карту»
        box = QGroupBox("Звільнити карту для обчислень")
        broot = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Карта, яку звільнити:"))
        self.combo = QComboBox()
        row.addWidget(self.combo, 1)
        broot.addLayout(row)

        self.chk_monitors = QCheckBox("Вимкнути монітори цієї карти")
        self.lbl_monitors = QLabel("Діє одразу, без виходу з сесії.")
        self.lbl_monitors.setObjectName("desc")
        r1 = QHBoxLayout()
        r1.addWidget(self.chk_monitors)
        r1.addWidget(self.lbl_monitors)
        r1.addStretch(1)
        broot.addLayout(r1)
        self.chk_desktop = QCheckBox("Робочий стіл малювати на іншій карті (KWin)")
        self.lbl_desktop = QLabel("Почне діяти після повторного входу.")
        self.lbl_desktop.setObjectName("desc")
        r2 = QHBoxLayout()
        r2.addWidget(self.chk_desktop)
        r2.addWidget(self.lbl_desktop)
        r2.addStretch(1)
        broot.addLayout(r2)
        btns = QHBoxLayout()
        self.btn_apply = QPushButton("Застосувати зміни")
        self.btn_reset = QPushButton("Скасувати: увімкнути всі монітори і видалити налаштування")
        btns.addWidget(self.btn_apply)
        btns.addWidget(self.btn_reset)
        broot.addLayout(btns)
        self.mode_label = QLabel()
        self.mode_label.setWordWrap(True)
        broot.addWidget(self.mode_label)
        root.addWidget(box)

        # ---- контейнер карток (біля-біля)
        self.container = QHBoxLayout()
        root.addLayout(self.container)

        # ---- процеси у VRAM
        pbox = QGroupBox(
            "Процеси GPU: VRAM та завантаження % (NVIDIA — усі; AMD без root — лише ваші)")
        proot = QVBoxLayout(pbox)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Карта", "Процес", "PID", "VRAM, МБ", "GPU, %"])
        self.tree.headerItem().setToolTip(
            4, "AMD/Intel: за часом GPU-движків у fdinfo; NVIDIA: SM utilization з nvidia-smi pmon. "
               "Якщо NVIDIA не завантажена, для її процесів показується 0%. «—» під час "
               "навантаження означає, що драйвер не надав окремого показника процесу.")
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(100)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(0, 150)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(2, 70)
        self.tree.setColumnWidth(3, 90)
        self.tree.setColumnWidth(4, 75)
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
        self.combo.currentIndexChanged.connect(self.on_combo)

        self.root_procs = None
        self.root_ts = 0.0
        self.checks_synced = False
        self.process_util = {}
        self.nvidia_proc_util = {}
        self._nvidia_pmon_warned = False
        self.nvidia_pmon = QProcess(self)
        self.nvidia_pmon.finished.connect(self.on_nvidia_pmon_finished)
        self.nvidia_pmon.errorOccurred.connect(self.on_nvidia_pmon_error)

        self._theme = self._build_theme()
        self.setStyleSheet(self._qss())

        if not shutil.which("kscreen-doctor"):  # на старті вже пропонували встановити
            self.set_status("kscreen-doctor не встановлено — монітори не вимикатимуться "
                           "(пакет kscreen)", "warn")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(REFRESH_MS)
        self.timer_procs = QTimer(self)
        self.timer_procs.timeout.connect(self.refresh_procs)
        self.timer_procs.start(PROCS_MS)
        self.intel_cache = {}          # останні метрики intel_gpu_top по картах
        self._intel_hinted = False     # підказку про CAP_PERFMON показуємо один раз
        self.timer_intel = QTimer(self)
        self.timer_intel.timeout.connect(self.tick_intel)
        self.timer_intel.start(INTEL_MS)
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
        }

    def _qss(self):
        t = self._theme
        return f"""
        QGroupBox {{
            border: 1px solid {t['border']};
            border-radius: 6px;
            margin-top: 10px;
            padding: 8px 8px 6px 8px;
        }}
        QGroupBox::title {{
            subcontrol-origin: padding;
            subcontrol-position: top left;
            left: 12px;
            padding: 1px 6px;
            background: {t['panel_bg']};
            border-radius: 5px;
            color: {t['text']};
        }}
        QGroupBox[role="free"]::title {{ color: {t['free']}; font-weight: bold; }}
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
        self.apply(scan())

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
            elif g["vendor"] == "Intel":  # i915: без власного VRAM, спільна RAM системи
                w["vram"].setText("VRAM: спільна з RAM системи")
                w["bar"].setValue(0)
            else:
                w["vram"].setText("VRAM: недоступно")
                w["bar"].setValue(0)
            parts = []
            if g["util"] is not None:
                parts.append(f"Навантаження: {g['util']:.0f}%")
            if g["temp"] is not None:
                parts.append(f"{g['temp']:.0f} °C")
            if g["power"] is not None:
                parts.append(f"{g['power']:.0f} Вт")
            if g["rt"]:
                parts.append(f"стан: {g['rt']}")
            w["metrics"].setText(" · ".join(parts) or "метрики недоступні")
            util = g["util"]
            if util is None and g["vendor"] == "Intel":
                util = (self.intel_cache.get(g["card"]) or {}).get("util")  # з intel_gpu_top
            w["util_bar"].setValue(
                int(max(0.0, min(100.0, util))) if util is not None else 0)
            if w.get("fan_lbl") is not None and g.get("fan") is not None:
                tgt = g.get("fan_target")
                extra = f" (ціль {tgt:.0f} об/хв)" if tgt else " (авто)"
                w["fan_lbl"].setText(f"Вентилятор: {g['fan']:.0f} об/хв{extra}")
            if w.get("pwr_slider") is not None and g.get("pwr_limit") is not None:
                w["pwr_lbl"].setText(
                    f"Потужність: ≤ {g['pwr_limit']:.0f} Вт "
                    f"(діапазон {g['pwr_min']:.0f}–{g['pwr_max']:.0f})")
            for cname, enabled in g["conns"]:
                cw = w["conns"][cname]
                mode = (g.get("modes") or {}).get(cname)
                cw["lbl"].setText(f"{cname} · {mode}" if mode else cname)
                cw["state"].setText("увімкнено" if enabled else "вимкнено")
                cw["state"].setStyleSheet(
                    f"color: {'#2e7d32' if enabled else '#b71c1c'};")
                cw["btn"].setText("Вимкнути" if enabled else "Увімкнути")
        if not self.checks_synced:  # синхронізація лише на старті, щоб не скидати вибір користувача
            self.sync_checks(ai)
            self.checks_synced = True
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
            w["util_bar"] = QProgressBar()
            w["util_bar"].setMaximum(100)
            v.addWidget(w["util_bar"])
            if g.get("fan") is not None and g.get("fan_max"):
                fr = QHBoxLayout()
                w["fan_lbl"] = QLabel()
                fr.addWidget(w["fan_lbl"])
                fr.addStretch(1)
                sld = QSlider(Qt.Orientation.Horizontal)
                sld.setRange(0, int(g["fan_max"]))
                sld.setValue(int(g.get("fan_target") or g["fan"]))
                sld.setFixedWidth(130)
                fr.addWidget(sld)
                fb = QPushButton("Застосувати (root — діалог KDE)")
                fb.clicked.connect(lambda _=False, c=card: self.apply_fan(c))
                fr.addWidget(fb)
                v.addLayout(fr)
                w["fan_slider"] = sld
            if g.get("pwr_limit") is not None and g.get("pwr_max"):
                pr = QHBoxLayout()
                w["pwr_lbl"] = QLabel()
                pr.addWidget(w["pwr_lbl"])
                pr.addStretch(1)
                sld = QSlider(Qt.Orientation.Horizontal)
                sld.setRange(int(g["pwr_min"]), int(g["pwr_max"]))
                sld.setValue(int(g["pwr_limit"]))
                sld.setFixedWidth(130)
                pr.addWidget(sld)
                pb = QPushButton("Застосувати (root — діалог KDE)")
                pb.clicked.connect(lambda _=False, c=card: self.apply_power(c))
                pr.addWidget(pb)
                v.addLayout(pr)
                w["pwr_slider"] = sld
            if g["vendor"] == "Intel":
                ir = QHBoxLayout()
                w["intel_lbl"] = QLabel("Метрики: запитати через intel_gpu_top")
                ir.addWidget(w["intel_lbl"])
                ir.addStretch(1)
                ib = QPushButton("Оновити метрики (root, якщо треба)")
                ib.clicked.connect(lambda _=False, c=card: self.refresh_intel_metrics(c))
                ir.addWidget(ib)
                v.addLayout(ir)
            if not g["conns"]:
                v.addWidget(QLabel("Підключених моніторів немає"))
            for cname, _ in g["conns"]:
                row = QHBoxLayout()
                lbl = QLabel()
                lbl.setMinimumWidth(185)
                state = QLabel()
                state.setMinimumWidth(110)
                btn = QPushButton()
                btn.clicked.connect(lambda _=False, n=cname: self.toggle_one(n))
                row.addWidget(lbl)
                row.addWidget(state)
                row.addStretch()
                row.addWidget(btn)
                v.addLayout(row)
                w["conns"][cname] = {"lbl": lbl, "state": state, "btn": btn}
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
            vram = f" · {round(g['total'] / 1024)} ГБ" if g["total"] else ""
            return f"{g['short']}{vram} ({g['card']})"

        self.combo_map = [(g["pci"], item(g)) for g in self.data]
        pcis = [p for p, _ in self.combo_map]
        if self.ai_pci not in pcis:
            saved = configured_ai_pci()
            if saved in pcis:
                self.ai_pci = saved
            elif self.data:
                self.ai_pci = max(self.data, key=lambda g: g["total"] or 0)["pci"]
        self.combo.blockSignals(True)
        self.combo.clear()
        for _, text in self.combo_map:
            self.combo.addItem(text)
        if self.ai_pci in pcis:
            self.combo.setCurrentIndex(pcis.index(self.ai_pci))
        self.combo.blockSignals(False)

    def on_combo(self, idx):
        if 0 <= idx < len(getattr(self, "combo_map", [])):
            self.ai_pci = self.combo_map[idx][0]

    def sync_checks(self, ai):
        """Синхронізує чекбокси з фактичним станом (на старті та після дій)."""
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
            _, engine_times = vram_processes(with_engine_times=True)
        else:
            procs, engine_times = vram_processes(with_engine_times=True)
        self.process_util = process_engine_utilization(engine_times)
        self.process_util.update(self.nvidia_proc_util)
        idle_nvidia_pcis = {
            g["pci"] for g in self.data
            if g["vendor"] == "NVIDIA" and g.get("util") == 0
        }
        for pci in idle_nvidia_pcis:
            for pid, _name, _mb in procs.get(pci, []):
                self.process_util[(pci, pid)] = 0.0
        names = {g["pci"]: g["short"] for g in self.data}
        sel = self.tree.currentItem()
        sel_key = sel.text(2) if sel else None  # PID — третя колонка
        self.tree.clear()
        for pci, lst in procs.items():
            for pid, name, mb in lst:
                if mb < 1:  # шум: fd відкритий, але VRAM фактично нульовий
                    continue
                utilization = self.process_util.get((pci, pid))
                text = f"{utilization:.0f}" if utilization is not None else "—"
                item = QTreeWidgetItem(
                    [names.get(pci, pci), name, str(pid), f"{mb:.0f}", text])
                item.setData(0, Qt.ItemDataRole.UserRole, pci)
                for column in (2, 3, 4):
                    item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
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
                item = QTreeWidgetItem(
                    [names.get(pci, pci), note, "—", f"{diff:.0f}", "—"])
                for column in (2, 3, 4):
                    item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
                self.tree.addTopLevelItem(item)
        self.start_nvidia_pmon()

    def start_nvidia_pmon(self):
        if (not any(g["vendor"] == "NVIDIA" for g in self.data)
                or not shutil.which("nvidia-smi")
                or self.nvidia_pmon.state() != QProcess.ProcessState.NotRunning):
            return
        self.nvidia_pmon.start("nvidia-smi", ["pmon", "-c", "1", "-s", "u"])

    def on_nvidia_pmon_finished(self, exit_code, _exit_status):
        if exit_code == 0:
            output = bytes(self.nvidia_pmon.readAllStandardOutput()).decode(
                "utf-8", errors="replace")
            self.nvidia_proc_util = nvidia_process_utilization(output, nvidia_pci_map())
        else:
            error = bytes(self.nvidia_pmon.readAllStandardError()).decode(
                "utf-8", errors="replace").strip()
            self.nvidia_proc_util = {}
            if not self._nvidia_pmon_warned:
                message = error.splitlines()[-1] if error else f"код завершення {exit_code}"
                self.set_status(f"Не вдалося прочитати завантаження процесів NVIDIA: {message}",
                                "warn")
                self._nvidia_pmon_warned = True
        nvidia_pcis = {g["pci"] for g in self.data if g["vendor"] == "NVIDIA"}
        self.process_util = {key: value for key, value in self.process_util.items()
                             if key[0] not in nvidia_pcis}
        self.process_util.update(self.nvidia_proc_util)
        self.update_process_utilization_cells()

    def on_nvidia_pmon_error(self, _error):
        if not self._nvidia_pmon_warned:
            self.set_status(
                f"Не вдалося запустити nvidia-smi pmon: {self.nvidia_pmon.errorString()}",
                "warn")
            self._nvidia_pmon_warned = True

    def update_process_utilization_cells(self):
        idle_nvidia_pcis = {
            g["pci"] for g in self.data
            if g["vendor"] == "NVIDIA" and g.get("util") == 0
        }
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            pci = item.data(0, Qt.ItemDataRole.UserRole)
            pid = to_int(item.text(2))
            utilization = self.process_util.get((pci, pid)) if pci and pid else None
            if pci in idle_nvidia_pcis and pid is not None:
                utilization = 0.0
            item.setText(4, f"{utilization:.0f}" if utilization is not None else "—")

    # ---- вентилятор (AMD) і ліміт потужності (NVIDIA)
    def _pkexec(self, cmd):
        """Запустити команду через pkexec (вбуданий діалог KDE). None — не вдалося."""
        if not shutil.which("pkexec"):
            self.set_status("pkexec не знайдено", "warn")
            return None
        self.set_status("Очікуємо підтвердження у діалозі KDE…")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            self.set_status("Не вдалося запустити команду", "warn")
            return None
        return r

    def apply_fan(self, card):
        g = next((x for x in self.data if x["card"] == card), None)
        w = self.widgets.get(card, {})
        if not g or "fan_slider" not in w:
            return
        path = next(iter(glob.glob(f"{DRM}/{card}/device/hwmon/hwmon*/fan1_target")), None)
        if not path:
            self.set_status("Не знайдено fan1_target", "warn")
            return
        val = w["fan_slider"].value()
        sh = shutil.which("sh") or "/bin/sh"
        r = self._pkexec(["pkexec", sh, "-c", f"echo {val} > {path}"])
        if r is None:
            return
        if r.returncode == 0:
            self.set_status(f"Ціль вентилятора: {val} об/хв", "ok")
            QTimer.singleShot(1200, self.tick)
        else:
            self.set_status("Не вдалося встановити швидкість вентилятора", "warn")

    def apply_power(self, card):
        g = next((x for x in self.data if x["card"] == card), None)
        w = self.widgets.get(card, {})
        if not g or "pwr_slider" not in w:
            return
        smi = shutil.which("nvidia-smi")
        idx = nvidia_index(g["pci"])
        if not smi or idx is None:
            self.set_status("Не знайдено nvidia-smi/індекс GPU", "warn")
            return
        val = w["pwr_slider"].value()
        r = self._pkexec(["pkexec", smi, f"--id={idx}", f"--power-limit={val}"])
        if r is None:
            return
        if r.returncode == 0:
            self.set_status(f"Ліміт потужності: {val} Вт", "ok")
            QTimer.singleShot(1200, self.tick)
        else:
            err = (r.stderr or r.stdout).strip().splitlines()
            self.set_status(f"Не вдалося встановити ліміт ({err[0][:80] if err else 'помилка'})", "warn")

    # ---- метрики Intel (intel_gpu_top)
    def refresh_intel_metrics(self, card):
        g = next((x for x in self.data if x["card"] == card), None)
        w = self.widgets.get(card, {})
        if not g or "intel_lbl" not in w:
            return
        if not shutil.which("intel_gpu_top"):
            self.set_status("intel_gpu_top не встановлено — метрики недоступні. "
                            "Встановіть: sudo dnf install intel-gpu-tools", "warn")
            return
        self.set_status(f"Читаємо метрики {g['short']} (intel_gpu_top)…")
        sample = intel_sample(intel_top_cmd(card))
        if sample is None:  # старі версії без -n — timeout обриває стрим, парсимо перший зразок
            sample = intel_sample(intel_top_cmd(card, n=False, secs=5))
        if sample is None and shutil.which("setcap"):
            # Без root зазвичай блокує perf_event_paranoid/CAP_PERFMON: разово надаємо
            # CAP_PERFMON бінарнику (root — діалог KDE), далі метрики читаються без root.
            r = self._pkexec(["pkexec", "setcap", "cap_perfmon+ep", shutil.which("intel_gpu_top")])
            if r is not None and r.returncode == 0:
                sample = intel_sample(intel_top_cmd(card))
                if sample is None:
                    sample = intel_sample(intel_top_cmd(card, n=False, secs=5))
                if sample is not None:
                    self.set_status("CAP_PERFMON надано — далі метрики читаються без root", "ok")
        if sample is None:
            # Якщо setcap не допомогло — повне root-виконання (діалог KDE)
            r = self._pkexec(["pkexec"] + intel_top_cmd(card))
            if r is None:
                return
            out = r.stdout or ""
            i = out.find("{")
            sample = None
            if i >= 0:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(out[i:])
                    sample = obj if isinstance(obj, dict) else None
                except ValueError:
                    pass
            if sample is None:
                err = (r.stderr or "").strip().splitlines()
                self.set_status(f"intel_gpu_top не дав даних ({err[0][:80] if err else 'помилка'})", "warn")
                return
        m = intel_metrics(sample)
        if not m:
            self.set_status("intel_gpu_top не дав жодних метрик", "warn")
            return
        self.intel_cache[card] = m
        self._apply_intel_metrics(g, w, m)
        self.set_status(f"Метрики {g['short']}: " + w["intel_lbl"].text(), "ok")

    def _apply_intel_metrics(self, g, w, m):
        """Оновити рядок метрик Intel і бар заповненості."""
        parts = []
        if m.get("util") is not None:
            parts.append(f"Навантаження: {m['util']:.0f}%")
        if m.get("freq") is not None:
            parts.append(f"{m['freq']:.0f} МГц")
        if m.get("power") is not None:
            parts.append(f"{m['power']:.1f} Вт")
        bw = m.get("bw")
        if bw:
            rd, wr, unit = bw
            parts.append(f"ПАМ: {rd:.0f}/{wr:.0f} {unit}")
        w["intel_lbl"].setText(" · ".join(parts) or "метрики: —")
        if m.get("util") is not None:
            w["util_bar"].setValue(int(max(0.0, min(100.0, m["util"]))))

    def tick_intel(self):
        """Автоматичне читання метрик Intel кожні INTEL_MS без root (CAP_PERFMON)."""
        if not shutil.which("intel_gpu_top"):
            return
        for g in self.data:
            if g["vendor"] != "Intel":
                continue
            w = self.widgets.get(g["card"], {})
            if "intel_lbl" not in w:
                continue
            sample = intel_sample(intel_top_cmd(g["card"]))
            if sample is None:
                # без CAP_PERFMON не читаємо — діалогами не спамимо, підказка один раз
                if not self._intel_hinted:
                    self.set_status("Метрики Intel не читаються без root — натисніть "
                                   "«Оновити метрики» (разовий діалог KDE видасть CAP_PERFMON)",
                                   "warn")
                    self._intel_hinted = True
                continue
            m = intel_metrics(sample)
            if not m:
                continue
            self.intel_cache[g["card"]] = m
            self._apply_intel_metrics(g, w, m)

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
        ai = next((g for g in self.data if g["pci"] == self.ai_pci), None)
        self.sync_checks(ai)
        if env_changed:
            self.status.setText("Налаштування записано.")
            if QMessageBox.question(
                    self, "Перелогін",
                    "Щоб робочий стіл перемалювався на іншу карту, потрібен вихід з сесії. "
                    "Спершу збережіть роботу у відкритих програмах. Вийти зараз?") \
                    == QMessageBox.StandardButton.Yes:
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
        ai = next((g for g in self.data if g["pci"] == self.ai_pci), None)
        self.sync_checks(ai)
        if QMessageBox.question(
                self, "Перелогін",
                "Щоб робочий стіл повернувся на головну карту, потрібен вихід з сесії. Вийти зараз?") \
                == QMessageBox.StandardButton.Yes:
            logout()

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
    deps = missing_deps(scan())  # стартовий діалог: що встановити з відсутнього
    if deps:
        dlg = DepsDialog(None, deps)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            pkgs = dlg.selected()
            if pkgs:
                install_packages(pkgs)
    w = App()
    w.show()
    sys.exit(app.exec())
