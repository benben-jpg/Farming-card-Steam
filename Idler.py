import ctypes
import json
import os
import re
import subprocess
import sys
import time
import html
import urllib.error
import urllib.request

MAX_PARALLEL = 32          # batas keras jumlah game sekaligus
LOW_HOURS = 2.0            # game < 2 jam main belum bisa drop card
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# --------------------------------------------------------------------------
# Bagian idle (proses anak)
# --------------------------------------------------------------------------

def find_library():
    if sys.platform.startswith("win"):
        name = "steam_api64.dll" if sys.maxsize > 2**32 else "steam_api.dll"
    elif sys.platform == "darwin":
        name = "libsteam_api.dylib"
    else:
        name = "libsteam_api.so"
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        sys.exit(f"[!] {name} tidak ditemukan di {HERE}")
    return path


def run_child(app_id: str):
    """Proses anak: init Steam API untuk satu AppID dan tahan sampai dimatikan."""
    os.environ["SteamAppId"] = app_id
    os.environ["SteamGameId"] = app_id
    lib = ctypes.CDLL(find_library())
    lib.SteamAPI_Init.restype = ctypes.c_bool
    if not lib.SteamAPI_Init():
        print(f"[{app_id}] SteamAPI_Init gagal "
              "(Steam belum jalan / game tidak dimiliki?)", flush=True)
        sys.exit(1)
    try:
        while True:
            time.sleep(60)
    finally:
        lib.SteamAPI_Shutdown()


def start_idle(app_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--child", app_id]
    )


# --------------------------------------------------------------------------
# Bagian deteksi card drop (membaca halaman badge milik akun sendiri)
# --------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_FILE):
        sys.exit("[!] config.json tidak ditemukan. Salin config.example.json "
                 "menjadi config.json lalu isi cookie kamu.")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("sessionid", "steamLoginSecure"):
        if not cfg.get(key) or cfg[key].startswith("ISI_"):
            sys.exit(f"[!] '{key}' di config.json belum diisi.")
    return cfg


def fetch_badge_page(cfg, page: int) -> str:
    url = f"https://steamcommunity.com/my/badges/?l=english&p={page}"
    req = urllib.request.Request(url, headers={
        "Cookie": (f"sessionid={cfg['sessionid']}; "
                   f"steamLoginSecure={cfg['steamLoginSecure']}"),
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            final_url = resp.geturl()
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        sys.exit(f"[!] Gagal mengakses Steam Community: {e}")
    if "/login" in final_url or "g_steamID = false" in body:
        sys.exit("[!] Cookie tidak valid / kedaluwarsa. Login ulang di browser "
                 "lalu salin sessionid dan steamLoginSecure yang baru.")
    return body


ROW_SPLIT = re.compile(r'<div class="badge_row\b')


def parse_games(body: str):
    games = []
    for block in ROW_SPLIT.split(body)[1:]:
        m_id = re.search(r"/gamecards/(\d+)", block)
        m_drop = re.search(r"(\d+)\s+card drops? remaining", block)
        if not m_id or not m_drop:
            continue
        m_name = re.search(r'<div class="badge_title">\s*(.*?)\s*(?:&nbsp;|<)',
                           block, re.S)
        m_hrs = re.search(r"([\d,.]+)\s*hrs on record", block)
        hours = float(m_hrs.group(1).replace(",", "")) if m_hrs else 0.0
        games.append({
            "appid": m_id.group(1),
            "name": html.unescape(m_name.group(1)) if m_name else m_id.group(1),
            "drops": int(m_drop.group(1)),
            "hours": hours,
        })
    return games


def count_pages(body: str) -> int:
    pages = [1]
    for tag in re.findall(r'<a[^>]*class="pagelink"[^>]*>', body):
        m = re.search(r"[?&]p=(\d+)", tag)
        if m:
            pages.append(int(m.group(1)))
    return max(pages)


def fetch_games_with_drops(cfg):
    first = fetch_badge_page(cfg, 1)
    games = parse_games(first)
    for page in range(2, count_pages(first) + 1):
        time.sleep(1)
        games += parse_games(fetch_badge_page(cfg, page))
    return games


# --------------------------------------------------------------------------
# Mode --list dan --auto
# --------------------------------------------------------------------------

def mode_list():
    cfg = load_config()
    games = fetch_games_with_drops(cfg)
    if not games:
        print("Tidak ada game dengan card drop tersisa.")
        return
    total = sum(g["drops"] for g in games)
    print(f"{len(games)} game, total {total} card drop tersisa:\n")
    for g in sorted(games, key=lambda x: -x["drops"]):
        print(f"  {g['appid']:>8}  {g['drops']:>2} drop  "
              f"{g['hours']:>6.1f} jam  {g['name']}")


def mode_auto():
    cfg = load_config()
    blacklist = {str(x) for x in cfg.get("blacklist", [])}
    interval = max(1, int(cfg.get("check_interval_minutes", 10))) * 60
    max_par = min(int(cfg.get("max_parallel", 30)), MAX_PARALLEL)
    low_par = max(1, int(cfg.get("low_hours_parallel", 1)))

    running = {}   # appid -> Popen
    failed = set()
    try:
        while True:
            games = [g for g in fetch_games_with_drops(cfg)
                     if g["appid"] not in blacklist and g["appid"] not in failed]
            if not games:
                print("Semua card drop sudah habis. Selesai.")
                break

            low = [g for g in games if g["hours"] < LOW_HOURS]
            high = [g for g in games if g["hours"] >= LOW_HOURS]
            if low:
                target = low[:low_par]
                phase = f"fase awal (game < {LOW_HOURS:g} jam)"
            else:
                target = high[:max_par]
                phase = "fase paralel"
            target_ids = {g["appid"] for g in target}

            # hentikan yang tidak lagi diperlukan
            for app_id in list(running):
                if app_id not in target_ids:
                    running.pop(app_id).terminate()

            # tandai proses yang mati sendiri (gagal init)
            for app_id, proc in list(running.items()):
                if proc.poll() is not None:
                    print(f"[{app_id}] proses berhenti, dilewati.")
                    failed.add(app_id)
                    running.pop(app_id)

            # mulai yang baru
            for g in target:
                if g["appid"] not in running and g["appid"] not in failed:
                    running[g["appid"]] = start_idle(g["appid"])
                    time.sleep(1)

            remaining = sum(g["drops"] for g in games)
            print(f"[{time.strftime('%H:%M:%S')}] {phase}: idle "
                  f"{len(running)} game, sisa {remaining} drop "
                  f"di {len(games)} game. Cek lagi {interval // 60} menit lagi.")
            for g in target:
                print(f"    - {g['name']} ({g['appid']}) "
                      f"{g['drops']} drop, {g['hours']:.1f} jam")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nMenghentikan...")
    finally:
        for proc in running.values():
            if proc.poll() is None:
                proc.terminate()


def mode_manual(app_ids):
    if len(app_ids) > MAX_PARALLEL:
        sys.exit(f"Maksimal {MAX_PARALLEL} game sekaligus.")
    procs = []
    try:
        for app_id in app_ids:
            procs.append(start_idle(app_id))
            time.sleep(1)
        print("Sedang idle. Tekan Ctrl+C untuk berhenti.")
        while True:
            time.sleep(5)
            if all(p.poll() is not None for p in procs):
                print("Semua proses berhenti.")
                break
    except KeyboardInterrupt:
        print("\nMenghentikan...")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--child":
        run_child(args[1])
    elif args == ["--list"]:
        mode_list()
    elif args == ["--auto"]:
        mode_auto()
    elif args and all(a.isdigit() for a in args):
        mode_manual(args)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
