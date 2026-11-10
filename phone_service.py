# SPDX-License-Identifier: GPL-3.0-or-later
# /// script
# requires-python = ">=3.10"
# dependencies = ["pymobiledevice3>=11.50,<12"]
# ///
"""
Phone service: monthly iPhone maintenance over USB.

    uv run phone_service.py report           # read-only: health, crashes, big videos, apps
    uv run phone_service.py uninstall        # removes apps marked for deletion (asks first)
    uv run phone_service.py clear-crashes    # deletes crash logs on the phone (asks first)
    uv run phone_service.py organise         # status: is the phone in sync with the stored layout?
    uv run phone_service.py organise pull    # copy the phone's home screen into the stored layout
    uv run phone_service.py organise edit    # drag apps/folders/widgets, mark apps to delete (browser)
    uv run phone_service.py organise preview # picture of the stored layout with real icons
    uv run phone_service.py organise apply   # push the stored layout to the phone (asks first)
    uv run phone_service.py organise restore # put a saved phone layout back

Plug in, unlock, tap Trust the first time. Nothing deletes without a typed "yes".
"""

import asyncio
import csv
import html
import json
import plistlib
import posixpath
import re
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.crash_reports import CrashReportsManager
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.mobile_config import MobileConfigService
from pymobiledevice3.services.springboard import SpringBoardServicesService

HOME = Path.home() / "PhoneService"
APPS_CSV = HOME / "apps.csv"
VIDEO_EXT = {".mov", ".mp4", ".m4v"}


def gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


async def section(title: str, lines: list[str], coro):
    """Run one check; if Apple has changed that service, note it and carry on."""
    lines.append(f"\n## {title}\n")
    try:
        await coro(lines)
    except Exception as e:  # noqa: BLE001
        lines.append(f"_Couldn't read this on this iOS version: {type(e).__name__}: {e}_")


CRASH_EXT = (".ips", ".crash", ".ips.synced", ".crash.synced")


async def crash_files(ld) -> list[str]:
    """Crash/diagnostic report paths. iOS 27 names them *.ips.synced.

    flush() asks the separate crash-mover service to file very recent reports first. On
    iOS 27 that service often resets the connection, so it's best-effort: listing works
    without it, you just might not see crashes from the last few minutes."""
    async with CrashReportsManager(ld) as cm:
        try:
            await cm.flush()
        except Exception:  # noqa: BLE001
            pass
    async with CrashReportsManager(ld) as cm:
        paths = await cm.ls("/", depth=-1)
    return sorted(p for p in paths if p.endswith(CRASH_EXT))


def crash_source(path: str) -> str:
    # "WhatsApp-2026-10-16-175458.ips.synced" -> "WhatsApp"
    # "cloudd.diskwrites_resource-2026-...": kept, it says what kind of report it is
    return re.split(r"-\d{4}-\d{2}-\d{2}", posixpath.basename(path))[0]


async def report():
    HOME.mkdir(exist_ok=True)
    ld = await create_using_usbmux()
    name = await ld.get_value(key="DeviceName")
    lines = [f"# Phone service: {name}", f"{datetime.now():%d %B %Y %H:%M}, iOS {ld.product_version}"]

    async def storage(out):
        d = await ld.get_value(domain="com.apple.disk_usage")
        total, free = d["TotalDiskCapacity"], d["AmountDataAvailable"]
        out.append(f"- {gb(free)} free of {gb(total)} ({free / total:.0%})")

    async def icloud_backup(out):
        b = await ld.get_value(domain="com.apple.mobile.backup") or {}
        if not b.get("CloudBackupEnabled"):
            out.append("- **iCloud Backup is OFF.** Turn it on before deleting anything.")
            return
        last = b.get("LastCloudBackupDate")  # seconds since 1 Jan 2001 (Apple's epoch)
        if last is None:
            out.append("- On, but the phone doesn't report a last backup date.")
            return
        when = datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=last)
        age = datetime.now(timezone.utc) - when
        out.append(f"- On. Last backup {when.astimezone():%d %b %Y %H:%M} ({age.days} days ago)")
        if age > timedelta(days=7):
            out.append("- **Over a week old.** Run Settings > [your name] > iCloud > iCloud Backup > Back Up Now first.")

    async def battery(out):
        async with DiagnosticsService(lockdown=ld) as diag:
            b = await diag.get_battery() or {}
        # iOS 27 nests capacities in BatteryData; AppleRawMaxCapacity is gone and
        # MaxCapacity is a plain 100 (percent), so NominalChargeCapacity is the real figure.
        data = b.get("BatteryData") or b
        cycles = b.get("CycleCount")
        design = data.get("DesignCapacity")
        current = data.get("NominalChargeCapacity")
        out.append(f"- Cycle count: {cycles if cycles is not None else 'unknown'}")
        if design and current:
            out.append(f"- Capacity: {current / design:.0%} of original ({current} / {design} mAh)")
            if current / design < 0.8:
                out.append("- **Under 80%. Worth a battery replacement.**")

    async def profiles(out):
        async with MobileConfigService(lockdown=ld) as mc:
            meta = (await mc.get_profile_list()).get("ProfileMetadata", {})
        if not meta:
            out.append("- None installed")
        for pid, m in meta.items():
            out.append(f"- {m.get('PayloadDisplayName', pid)} ({m.get('PayloadOrganization', 'no org')})")
        if meta:
            out.append("\nRemove stale ones in Settings > General > VPN & Device Management.")

    async def crashes(out):
        reports = await crash_files(ld)
        apps = Counter(crash_source(p) for p in reports)
        out.append(f"- {len(reports)} reports on the phone")
        for app, n in apps.most_common(8):
            out.append(f"- {app}: {n}")
        if reports:
            out.append("\nRun `clear-crashes` once you've read this.")

    async def videos(out):
        found = []
        async with AfcService(lockdown=ld) as afc:
            async for p in afc.dirlist("DCIM", -1):
                if Path(p).suffix.lower() in VIDEO_EXT:
                    st = await afc.stat(p)
                    found.append((st["st_size"], st["st_mtime"], p))
        found.sort(reverse=True)
        out.append(f"- {len(found)} videos stored on the phone, {gb(sum(f[0] for f in found))} in total")
        out.append("- Biggest. Dates are when the file landed on the phone, not when it was shot,")
        out.append("  so search Photos by size/length rather than date. Export, then delete in Photos:\n")
        for size, mtime, p in found[:15]:
            when = mtime.strftime("%d %b %Y") if hasattr(mtime, "strftime") else mtime
            out.append(f"  - {gb(size)}, {when}, {posixpath.basename(p)}")

    async def apps(out):
        async with InstallationProxyService(lockdown=ld) as ip:
            found = await ip.get_apps(application_type="User", calculate_sizes=True)
        rows = []
        for bid, a in found.items():
            size = (a.get("StaticDiskUsage") or 0) + (a.get("DynamicDiskUsage") or 0)
            rows.append([a.get("CFBundleDisplayName") or a.get("CFBundleName") or bid, bid, round(size / 1e6), ""])
        rows.sort(key=lambda r: -r[2])
        with APPS_CSV.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "bundle_id", "size_mb", "delete"])
            w.writerows(rows)
        out.append(f"- {len(rows)} apps, {gb(sum(r[2] for r in rows) * 1e6)} in total")
        for r in rows[:15]:
            out.append(f"- {r[0]}: {gb(r[2] * 1e6)}")
        out.append(f"\nFull list in `{APPS_CSV}`. Put an x in the delete column, then run `uninstall`.")

    await section("iCloud Backup", lines, icloud_backup)
    await section("Storage", lines, storage)
    await section("Battery", lines, battery)
    await section("Configuration profiles", lines, profiles)
    await section("Crashes", lines, crashes)
    await section("Big videos", lines, videos)
    await section("Apps by size", lines, apps)

    path = HOME / f"report-{datetime.now():%Y-%m-%d}.md"
    path.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nSaved to {path}")


async def uninstall():
    marked = {}  # bundle id -> row; marks come from apps.csv and from the layout editor
    if APPS_CSV.exists():
        with APPS_CSV.open(newline="") as f:
            for r in csv.DictReader(f):
                if r["delete"].strip().lower() == "x":
                    marked[r["bundle_id"]] = {"name": r["name"], "bundle_id": r["bundle_id"], "size_mb": r["size_mb"]}
    store = load_store(required=False)
    for i in store.get("delete", []):
        a = store["apps"].get(i, {})
        marked[i] = {"name": a.get("name", i), "bundle_id": i, "size_mb": a.get("size_mb", "?")}
    marked = list(marked.values())
    if not marked:
        sys.exit("Nothing marked. Use `organise edit`, or put an x in the delete column of apps.csv.")
    print("This deletes these apps AND their data:\n")
    for r in marked:
        print(f"  {r['name']} ({r['size_mb']} MB)")
    if input("\nType yes to go ahead: ").strip().lower() != "yes":
        sys.exit("Cancelled.")
    ld = await create_using_usbmux()
    removed = set()
    async with InstallationProxyService(lockdown=ld) as ip:
        for r in marked:
            try:
                await ip.uninstall(r["bundle_id"])
                removed.add(r["bundle_id"])
                print(f"  removed {r['name']}")
            except Exception as e:  # noqa: BLE001
                print(f"  failed {r['name']}: {e}")
    if store and removed:
        prune(store, removed)
        save_store(store)


async def clear_crashes():
    ld = await create_using_usbmux()
    files = await crash_files(ld)
    if not files:
        sys.exit("No crash logs on the phone.")
    print("This deletes these crash/diagnostic logs from the phone:\n")
    for p in files:
        print(f"  {p}")
    print(f"\n{len(files)} files.")
    if input("Type yes to go ahead: ").strip().lower() != "yes":
        sys.exit("Cancelled.")
    async with CrashReportsManager(ld) as cm:
        for p in files:
            try:
                await cm.clear(posixpath.dirname(p) or "/", match=f"^{re.escape(posixpath.basename(p))}$")
            except Exception as e:  # noqa: BLE001
                print(f"  couldn't delete {p}: {e}")
    left = await crash_files(ld)
    print(f"Done. {len(files) - len(left)} removed, {len(left)} left.")


# ---------------------------------------------------------------- organise
# Home screen layout over USB (SpringBoard services).
# Writing a layout makes iOS put EVERY installed app on the home screen: there is no
# "App Library only" flag. So every layout written here places every app explicitly.

CATS_JSON = HOME / "categories.json"
CONFIG = HOME / "config.json"
ICONS = HOME / "icons"
PAGE_SLOTS = 24  # 6 rows x 4
FOLDER_PAGE = 9  # what you see when you open a folder
WIDGET_SLOTS = {"small": 4, "medium": 8, "large": 16, "extraLarge": 16}

CATEGORY_FOLDER = {  # App Store primary genre -> folder
    "Finance": "Money", "Travel": "Travel", "Navigation": "Travel", "Food & Drink": "Food",
    "Shopping": "Shopping", "Entertainment": "Watch & Listen", "Music": "Watch & Listen",
    "Photo & Video": "Photo & Video", "Lifestyle": "Home", "Business": "Work",
    "Developer Tools": "Work", "Productivity": "Work", "Graphics & Design": "Work",
    "Education": "Learn", "Reference": "Learn", "Book": "Learn", "Health & Fitness": "Health",
    "Medical": "Health", "Sports": "Health", "Games": "Games", "Utilities": "Tools",
    "Weather": "Tools", "News": "Social", "Social Networking": "Social",
}

APPLE_FOLDER = {
    "com.apple.Translate": "Learn", "com.apple.iBooks": "Learn",
    "com.apple.freeform": "Work", "com.apple.mobilecal": "Work", "com.apple.mobilenotes": "Work",
    "com.apple.reminders": "Work", "com.apple.DocumentsApp": "Work", "com.apple.MobileAddressBook": "Work",
    "com.apple.Health": "Health", "com.apple.Fitness": "Health",
    "com.apple.Music": "Watch & Listen", "com.apple.tv": "Watch & Listen", "com.apple.news": "Watch & Listen",
    "com.apple.games": "Games", "com.apple.Home": "Home", "com.apple.Maps": "Travel",
    "com.apple.Passbook": "Money", "com.apple.facetime": "Social",
}  # any other Apple app -> Tools

OVERRIDES = {  # well-known apps whose App Store category is a poor fit
    "com.google.ios.youtube": "Watch & Listen", "com.google.Docs": "Work",
    "com.google.Sheets": "Work", "com.google.Slides": "Work", "com.google.Drive": "Work",
    "com.google.calendar": "Work", "com.apple.Pages": "Work", "com.apple.Numbers": "Work",
    "com.apple.Keynote": "Work", "notion.id": "Work", "com.figma.FigmaMirror": "Work",
    "com.openai.chat": "AI", "com.anthropic.claude": "AI", "com.google.gemini": "AI",
    "com.microsoft.azureauthenticator": "Tools", "com.google.Authenticator": "Tools",
    "com.8bit.bitwarden": "Tools", "com.philips.lighting.hue2": "Home",
    "com.google.Chromecast": "Home", "com.sonos.SonosController2": "Home",
    "com.tplink.tapo": "Home", "com.eufylife.EufyHome": "Home", "com.tuya.smartlife": "Home",
    "com.google.Translate": "Learn",
}


def config() -> dict:
    """Optional ~/PhoneService/config.json:
    {"country": "gb",                        App Store region for category lookups
     "folders": {"bundle.id": "Folder"},     your own folder rules (win over the defaults)
     "webclip_folders": {"Name": "Folder"}}  web clips have no bundle id; matched by name"""
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def item_id(x) -> str | None:
    return x.get("bundleIdentifier") or x.get("displayIdentifier") if isinstance(x, dict) else None


def is_widget(x) -> bool:
    return isinstance(x, dict) and ("gridSize" in x or "elements" in x or x.get("elementType") == "widget")


def is_folder(x) -> bool:
    return isinstance(x, dict) and x.get("listType") == "folder"


def items(x):
    """Every app / web clip in a layout, including inside folders (not widgets)."""
    if isinstance(x, list):
        for y in x:
            yield from items(y)
    elif is_folder(x):
        yield from items(x["iconLists"])
    elif isinstance(x, dict) and not is_widget(x) and item_id(x):
        yield x


def label(x) -> str:
    return (x.get("displayName") or item_id(x) or "?").strip().lstrip("‎")


def slots(x) -> int:
    return WIDGET_SLOTS.get(x.get("gridSize"), 8) if is_widget(x) else 1


async def get_layout(ld):
    async with SpringBoardServicesService(lockdown=ld) as sb:
        return await sb.get_icon_state()


async def put_layout(ld, state):
    async with SpringBoardServicesService(lockdown=ld) as sb:
        await sb.set_icon_state(state)


def save_layout_backup(state) -> Path:
    path = HOME / f"layout-backup-{datetime.now():%Y%m%d-%H%M%S}.plist"
    path.write_bytes(plistlib.dumps(state, fmt=plistlib.FMT_BINARY))
    return path


def layout_backups() -> list[Path]:
    return sorted(HOME.glob("layout-backup-*.plist"))


def lookup_categories(bundle_ids) -> dict:
    """App Store primary genre per bundle id, from Apple's public lookup API. Cached."""
    cache = json.loads(CATS_JSON.read_text()) if CATS_JSON.exists() else {}
    todo = [b for b in bundle_ids if b not in cache]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        country = config().get("country", "us")
        url = f"https://itunes.apple.com/lookup?country={country}&entity=software&bundleId=" + ",".join(chunk)
        with urllib.request.urlopen(url, timeout=30) as r:
            found = {res["bundleId"]: res.get("primaryGenreName") for res in json.load(r)["results"]}
        for b in chunk:
            cache[b] = {"category": found.get(b)}
    CATS_JSON.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return {b: (cache.get(b) or {}).get("category") for b in bundle_ids}


# --- The stored layout ---------------------------------------------------------------
# ~/PhoneService/layout.json is your layout, kept on the Mac:
#   dock: [app ids]   pages: [[{"app": id} | {"folder": name} | {"widget": id}, ...], ...]
#   folders: {name: [app ids]}   apps: {id: name/kind/size/category}   delete: [app ids]
# `pull` copies the phone into it, `edit` changes it, `apply` writes it to the phone.

STORE = HOME / "layout.json"


def widget_id(x) -> str:
    return x.get("displayIdentifier") or x.get("uniqueIdentifier") or ""


def widget_label(x, names: dict) -> str:
    if "elements" in x:
        parts = [names.get(c, c.split(".")[-1]) for e in x["elements"] if (c := e.get("containerBundleIdentifier"))]
        return "Stack: " + ", ".join(parts) if parts else "Widget stack"
    c = x.get("containerBundleIdentifier") or ""
    return names.get(c) or c.split(".")[-1] or "Widget"


def load_store(required: bool = True) -> dict:
    if not STORE.exists():
        if required:
            sys.exit("No stored layout yet. Run `organise pull` to copy it from the phone.")
        return {}
    return json.loads(STORE.read_text())


def save_store(store: dict):
    if STORE.exists():
        STORE.with_suffix(".prev.json").write_text(STORE.read_text())  # one step of undo
    STORE.write_text(json.dumps(store, indent=1, ensure_ascii=False))


def stored_app_ids(store: dict) -> list[str]:
    out = list(store["dock"])
    for page in store["pages"]:
        out += [t["app"] for t in page if "app" in t]
    for ids in store["folders"].values():
        out += ids
    return out


def token_slots(store: dict, t: dict) -> int:
    if "widget" in t:
        return WIDGET_SLOTS.get(store["widgets"].get(t["widget"], {}).get("size"), 8)
    return 1


def page_slots(store: dict, page: list) -> int:
    return sum(token_slots(store, t) for t in page)


def place_token(store: dict, tok: dict, prefer: int | None = None):
    """Put a new folder or widget on a page with room: its own page if given, else the page
    with the most folders (for a folder), else the first page after page 1 with room."""
    need = token_slots(store, tok)
    pages = store["pages"]
    order = list(range(len(pages)))
    if prefer is not None and prefer < len(pages):
        order.insert(0, prefer)
    elif "folder" in tok:
        order.sort(key=lambda n: -sum("folder" in t for t in pages[n]))
    for n in order:
        if (n > 0 or prefer == 0) and page_slots(store, pages[n]) + need <= PAGE_SLOTS:
            pages[n].append(tok)
            return n
    pages.append([tok])
    return len(pages) - 1


def suggest_folder(i: str, app: dict) -> str:
    cfg = config()
    f = (cfg.get("folders", {}).get(i) or cfg.get("webclip_folders", {}).get(app.get("name", ""))
         or OVERRIDES.get(i))
    if not f and app.get("kind") == "apple":
        f = APPLE_FOLDER.get(i)
    return f or CATEGORY_FOLDER.get(app.get("category", ""), "Tools")


async def app_meta(ld, current) -> dict:
    """Name, kind, size and App Store category for everything on the home screen."""
    async with InstallationProxyService(lockdown=ld) as ip:
        info = await ip.get_apps(application_type="Any", calculate_sizes=True)
    user_apps = {b for b, a in info.items() if a.get("ApplicationType") == "User"}
    cats = lookup_categories(sorted(user_apps))
    meta = {}
    for x in items(current):
        i, a = item_id(x), info.get(item_id(x), {})
        meta[i] = {"name": label(x),
                   "kind": "webclip" if "webClipURL" in x else "apple" if i.startswith("com.apple.") and i not in user_apps else "app",
                   "size_mb": round(((a.get("StaticDiskUsage") or 0) + (a.get("DynamicDiskUsage") or 0)) / 1e6),
                   "category": cats.get(i) or ""}
    return meta


def arrangement(store: dict) -> dict:
    """The part of the store that `apply` writes; compared to tell whether there are edits."""
    return {"dock": store["dock"], "pages": store["pages"], "folders": store["folders"]}


def store_from_phone(current, meta: dict, old: dict) -> dict:
    names = {i: m["name"] for i, m in meta.items()}
    folders, widgets, pages = {}, {}, []
    for page in current[1:]:
        toks = []
        for x in page:
            if is_widget(x):
                wid = widget_id(x)
                widgets[wid] = {"label": widget_label(x, names), "size": x.get("gridSize", "medium")}
                toks.append({"widget": wid})
            elif is_folder(x):
                base = (x.get("displayName") or "").strip() or "Folder"
                name, n = base, 2
                while name in folders:
                    name, n = f"{base} {n}", n + 1
                folders[name] = [item_id(y) for y in items(x["iconLists"])]
                toks.append({"folder": name})
            elif item_id(x):
                toks.append({"app": item_id(x)})
        if toks:
            pages.append(toks)
    store = {"version": 1, "dock": [item_id(x) for x in items(current[0])], "pages": pages,
             "folders": folders, "apps": meta, "widgets": widgets,
             "delete": [i for i in old.get("delete", []) if meta.get(i, {}).get("kind") == "app"],
             "edited": False, "synced": describe(current), "synced_at": f"{datetime.now():%Y-%m-%d %H:%M}"}
    store["base"] = json.loads(json.dumps(arrangement(store)))
    return store


def prune(store: dict, gone: set):
    store["dock"] = [i for i in store["dock"] if i not in gone]
    store["pages"] = [[t for t in p if t.get("app") not in gone] for p in store["pages"]]
    store["folders"] = {f: [i for i in ids if i not in gone] for f, ids in store["folders"].items()}
    store["delete"] = [i for i in store["delete"] if i not in gone]
    for i in gone:
        store["apps"].pop(i, None)


def absorb(store: dict, current) -> list[str]:
    """Bring the store up to date with apps/widgets added or removed on the phone since.
    Returns lines describing what changed."""
    notes = []
    on_phone = {item_id(x): x for x in items(current)}
    known = set(stored_app_ids(store))
    if gone := known - set(on_phone):
        prune(store, gone)
        notes.append(f"{len(gone)} apps no longer on the phone, removed from the stored layout.")
    new = [i for i in on_phone if i not in known]
    if new:
        maybe_apps = [i for i in new if not i.startswith("com.apple.") and "webClipURL" not in on_phone[i]]
        cats = lookup_categories(maybe_apps) if maybe_apps else {}
        for i in new:
            x = on_phone[i]
            store["apps"][i] = {"name": label(x), "kind": "webclip" if "webClipURL" in x else "apple" if i.startswith("com.apple.") else "app",
                                "size_mb": 0, "category": cats.get(i) or ""}
            f = suggest_folder(i, store["apps"][i])
            if f not in store["folders"]:
                store["folders"][f] = []
                place_token(store, {"folder": f})
            store["folders"][f].append(i)
            notes.append(f"New app {label(x)} -> {f}")
    names = {i: m["name"] for i, m in store["apps"].items()}
    stored_w = {t["widget"] for p in store["pages"] for t in p if "widget" in t}
    for n, page in enumerate(current[1:]):
        for x in page:
            if is_widget(x) and widget_id(x) not in stored_w:
                store["widgets"][widget_id(x)] = {"label": widget_label(x, names), "size": x.get("gridSize", "medium")}
                where = place_token(store, {"widget": widget_id(x)}, prefer=n)
                notes.append(f"New widget {widget_label(x, names)} -> page {where + 1}")
    return notes


def build_from_store(current, store: dict) -> tuple[list, list[str]]:
    """The layout to write. Every app on the phone must be in the store (run absorb first)."""
    by_id = {item_id(x): x for x in items(current)}
    widgets_now = {widget_id(x): x for page in current for x in page if is_widget(x)}
    placed, lost = set(), []

    def app(i):
        if i in by_id and i not in placed:
            placed.add(i)
            return by_id[i]

    dock = [x for i in store["dock"] if (x := app(i))]
    if len(dock) > 4:
        sys.exit("More than 4 apps in the dock.")
    pages = []
    for page in store["pages"]:
        out = []
        for t in page:
            if "app" in t and (x := app(t["app"])):
                out.append(x)
            elif "widget" in t:
                if t["widget"] in widgets_now:
                    out.append(widgets_now[t["widget"]])
                else:
                    lost.append(store["widgets"].get(t["widget"], {}).get("label", "a widget"))
            elif "folder" in t:
                ids = [i for i in store["folders"].get(t["folder"], []) if app(i)]
                if ids:
                    lists = [[by_id[i] for i in ids[n:n + FOLDER_PAGE]] for n in range(0, len(ids), FOLDER_PAGE)]
                    out.append({"displayName": t["folder"], "iconLists": lists, "listType": "folder"})
        if out:
            if sum(slots(x) for x in out) > PAGE_SLOTS:
                sys.exit(f"Page {len(pages) + 1} has more than {PAGE_SLOTS} slots of apps, folders and widgets.")
            pages.append(out)
    layout = [dock, *pages]
    missing = set(by_id) - placed
    assert not missing, f"not placed: {missing}"
    return layout, lost


def describe(layout) -> list[str]:
    out = [f"Dock: {', '.join(label(x) for x in layout[0])}"]
    for n, page in enumerate(layout[1:], start=1):
        parts = []
        for x in page:
            if is_folder(x):
                parts.append(f"[{x['displayName']}: {sum(len(lst) for lst in x["iconLists"])}]")
            elif is_widget(x):
                parts.append(f"<{x.get('gridSize', '')} widget>")
            else:
                parts.append(label(x))
        out.append(f"Page {n}: {', '.join(parts)}")
    return out


async def phone_now():
    ld = await create_using_usbmux()
    return ld, await get_layout(ld)


# --- organise commands -------------------------------------------------------------------

async def organise_pull():
    """Copy the phone's layout into the stored one. The phone wins."""
    HOME.mkdir(exist_ok=True)
    old = load_store(required=False)
    if old.get("edited"):
        print("You have edits here that were never applied to the phone. Pulling replaces them")
        print(f"(the old version is kept as {STORE.with_suffix('.prev.json').name}).")
        if input("Type yes to pull anyway: ").strip().lower() != "yes":
            sys.exit("Cancelled. Run `organise apply` to push your edits instead.")
    ld, current = await phone_now()
    print("Reading the phone (sizes take a few seconds)...")
    store = store_from_phone(current, await app_meta(ld, current), old)
    save_store(store)
    print("\n".join(describe(current)))
    print(f"\nStored. {len(stored_app_ids(store))} apps, {len(store['folders'])} folders, {len(store['pages'])} pages.")
    print("Next: `organise edit`.")


async def organise_status():
    store = load_store(required=False)
    if not store:
        print("No stored layout. Run `organise pull` first.")
    else:
        print(f"Stored layout: last synced with the phone {store.get('synced_at', '?')}.")
        print("You have edits not yet applied to the phone." if store.get("edited") else "No unapplied edits.")
        if store.get("delete"):
            print(f"{len(store['delete'])} apps marked for deletion (run `uninstall`).")
        try:
            _, current = await phone_now()
            changed = describe(current) != store.get("synced")
            print("The phone's layout has changed since then. `organise pull` copies those changes here."
                  if changed else "The phone matches what was last pulled/applied.")
        except Exception:  # noqa: BLE001
            print("(Phone not connected, so I can't compare.)")
    print("\norganise pull | edit | preview | apply | restore [file]")


async def organise_edit():
    store = load_store()
    banner = ""
    try:
        ld, current = await phone_now()
        if describe(current) != store.get("synced") and not store.get("edited"):
            banner = "Your phone has changed since you last pulled. To keep those changes, close this, run organise pull, then edit."
        elif describe(current) != store.get("synced"):
            banner = "Both the phone and this layout have changed. Applying will replace the phone's changes."
        notes = absorb(store, current)
        if notes:
            print("\n".join(notes))
            save_store(store)
        await fetch_icons(ld, [i for i in store["apps"] if not (ICONS / f"{i}.png").exists()])
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        print("Phone not connected: editing the stored layout offline.")
    serve_editor(banner)


async def organise_preview():
    store = load_store()
    ld, current = await phone_now()
    for note in absorb(store, current):
        print(note)
    layout, lost = build_from_store(current, store)
    await fetch_icons(ld, [item_id(x) for x in items(layout)])
    path = HOME / "preview.html"
    path.write_text(render_preview(layout))
    print("\n".join(describe(layout)))
    print(f"\nPreview: {path}")
    subprocess.run(["open", str(path)], check=False)


async def organise_apply():
    store = load_store()
    ld, current = await phone_now()
    if describe(current) != store.get("synced"):
        print("Heads up: the phone's layout has changed since you last pulled or applied.")
        print("Applying replaces those changes. To keep them, cancel and run `organise pull` first.\n")
    notes = absorb(store, current)
    layout, lost = build_from_store(current, store)
    if notes:
        print("\n".join(notes) + "\n")
    print("This rearranges your home screen to:\n")
    print("\n".join(describe(layout)))
    if lost:
        print(f"\nNot on the phone any more, so skipped: {', '.join(lost)} widget(s).")
    print("\nNo apps are deleted. Your current layout is saved first so `organise restore` can undo it.")
    if input("\nType yes to go ahead: ").strip().lower() != "yes":
        sys.exit("Cancelled.")
    print(f"Saved current layout to {save_layout_backup(current)}")
    await put_layout(ld, layout)
    await asyncio.sleep(2)
    after = await get_layout(await create_using_usbmux())
    print("Done, and the phone reports the new layout." if describe(after) == describe(layout)
          else "Written, but the phone reports a different layout. Check it, or run `organise restore`.")
    widgets = lambda st: Counter(widget_id(x) for page in st for x in page if is_widget(x))  # noqa: E731
    if dropped := widgets(layout) - widgets(after):
        names = [store["widgets"].get(w, {}).get("label", w) for w in dropped]
        print(f"iOS dropped these widgets: {', '.join(names)}. Re-add them by hand (long-press > +), then `organise pull`.")
    store.update(edited=False, synced=describe(after), synced_at=f"{datetime.now():%Y-%m-%d %H:%M}")
    store["base"] = json.loads(json.dumps(arrangement(store)))
    save_store(store)


# --- the editor (a small local web page) -------------------------------------------------

def save_edits(data: dict) -> str:
    """Validate what the editor sent and store it. Returns an error message, or ''."""
    store = load_store()
    dock, pages, folders = data.get("dock") or [], data.get("pages") or [], data.get("folders") or {}
    if len(dock) > 4:
        return "The dock holds 4 apps."
    names = [str(n).strip() for n in folders]
    if any(not n for n in names) or len(set(names)) != len(names):
        return "Folder names must be unique and not empty."
    tiles = [t["folder"] for p in pages for t in p if "folder" in t]
    if sorted(tiles) != sorted(folders):
        return "Every folder needs exactly one place on a page."
    sent_apps = list(dock) + [t["app"] for p in pages for t in p if "app" in t] + [i for ids in folders.values() for i in ids]
    if sorted(sent_apps) != sorted(stored_app_ids(store)):
        return "Some apps went missing or appear twice. Reload the page and try again."
    sent_w = sorted(t["widget"] for p in pages for t in p if "widget" in t)
    if sent_w != sorted(t["widget"] for p in store["pages"] for t in p if "widget" in t):
        return "Widgets went missing. Reload the page and try again."
    pages = [p for p in pages if p]
    store.update(dock=dock, pages=pages, folders={k.strip(): v for k, v in folders.items()})
    store["edited"] = arrangement(store) != store.get("base")
    for n, p in enumerate(pages, start=1):
        if page_slots(store, p) > PAGE_SLOTS:
            return f"Page {n} is over {PAGE_SLOTS} slots."
    store["delete"] = [i for i in data.get("delete") or [] if store["apps"].get(i, {}).get("kind") == "app"]
    save_store(store)
    return ""


def editor_html(token: str, banner: str) -> str:
    store = load_store()
    apps = {i: {**m, "icon": (ICONS / f"{i}.png").exists()} for i, m in store["apps"].items()}
    data = {k: store[k] for k in ("dock", "pages", "folders", "widgets", "delete")}
    data.update(apps=apps, banner=banner, slots=PAGE_SLOTS, wslots=WIDGET_SLOTS)
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return EDITOR_PAGE.replace("__DATA__", blob).replace("__TOKEN__", token)


def serve_editor(banner: str):
    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def local(self) -> bool:  # refuse requests that didn't come to our own address
            return self.headers.get("Host") == f"127.0.0.1:{server.server_port}"

        def do_GET(self):
            if not self.local():
                return self.send(403, b"", "text/plain")
            if self.path == "/":
                return self.send(200, editor_html(token, banner).encode(), "text/html; charset=utf-8")
            if self.path.startswith("/icons/"):
                p = ICONS / urllib.parse.unquote(self.path[len("/icons/"):])
                if p.parent == ICONS and p.suffix == ".png" and p.exists():
                    return self.send(200, p.read_bytes(), "image/png")
            self.send(404, b"", "text/plain")

        def do_POST(self):
            if not self.local() or self.path != f"/save/{token}":
                return self.send(403, b"", "text/plain")
            try:
                err = save_edits(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            self.send(200, json.dumps({"ok": not err, "error": err}).encode(), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Editor open at {url}\nSave in the page, then press Ctrl-C here to stop.", flush=True)
    subprocess.run(["open", url], check=False)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Next: `organise apply` to push it to the phone, `uninstall` for marked apps.")


EDITOR_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Home Screen Editor</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.2/Sortable.min.js"></script>
<style>
:root{--bg:#f2f2f5;--card:#fff;--ink:#1c1c1e;--muted:#6e6e73;--line:#dcdce1;--tile:#e5e5ea;--accent:#0a84ff;--first:#e8f1ff;--danger:#ff3b30;--warn:#fff4d6;--warnink:#7a5600}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#101012;--card:#1c1c1e;--ink:#f2f2f7;--muted:#98989f;--line:#333336;--tile:#2c2c2e;--first:#14243a;--warn:#3a2e10;--warnink:#ffd66b}}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.35 -apple-system,system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(12px);
 border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center}
h1{font-size:17px;margin:0;flex:1 1 auto} .stat{color:var(--muted)} .stat b{color:var(--ink)}
input.search,.newf input{padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit}
input.search{flex:0 1 220px}
button{font:inherit;border:0;border-radius:9px;padding:8px 14px;cursor:pointer;background:var(--tile);color:var(--ink)}
button.primary{background:var(--accent);color:#fff;font-weight:600} button:disabled{opacity:.5;cursor:default}
button.small{padding:3px 8px;font-size:12px}
#status{color:var(--muted);min-width:90px}
main{padding:16px;max-width:1600px;margin:0 auto} .hint{color:var(--muted);margin:0 0 16px;max-width:900px}
.banner{background:var(--warn);color:var(--warnink);padding:10px 14px;border-radius:12px;margin:0 0 16px}
h2.sec{font-size:15px;margin:28px 0 12px}
.pages{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:12px}
.card h3{font-size:13px;font-weight:600;margin:0 0 8px;display:flex;align-items:center;gap:8px}
.card h3 .count{color:var(--muted);font-weight:400;margin-left:auto} .card h3 .count.over{color:var(--danger);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(4,64px);grid-auto-rows:78px;gap:6px 10px;grid-auto-flow:row dense;min-height:84px;padding:4px;border-radius:12px}
.dock .grid{grid-auto-rows:78px;min-height:84px}
.app,.ftile{position:relative;width:64px;border-radius:10px;display:flex;flex-direction:column;align-items:center;gap:3px;padding-top:4px;cursor:grab;user-select:none}
.app img,.app .ph{width:48px;height:48px;border-radius:11px;background:var(--tile);display:grid;place-items:center;font-weight:600;pointer-events:none}
.name{font-size:10.5px;max-width:62px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.app.del img,.app.del .ph{opacity:.35;filter:grayscale(1)} .app.del .name{color:var(--danger);text-decoration:line-through}
.app.del::after{content:"✕";position:absolute;top:0;right:6px;width:18px;height:18px;border-radius:50%;background:var(--danger);color:#fff;font-size:11px;display:grid;place-items:center}
.app.new::before{content:"new";position:absolute;top:0;left:2px;background:var(--accent);color:#fff;font-size:9px;padding:0 4px;border-radius:6px}
.mini{width:48px;height:48px;border-radius:11px;background:var(--tile);display:grid;grid-template-columns:repeat(3,1fr);gap:2px;padding:5px;pointer-events:none}
.mini img{width:100%;border-radius:3px}
.widget{grid-column:span 4;grid-row:span 2;background:var(--tile);border-radius:18px;display:grid;place-items:center;text-align:center;
 color:var(--muted);font-size:12px;padding:8px;cursor:grab;user-select:none}
.widget.small{grid-column:span 2;grid-row:span 2} .widget.large,.widget.extraLarge{grid-row:span 4}
.widget b{display:block;color:var(--ink);font-weight:600}
.dim{opacity:.18} .hit{outline:2px solid var(--accent)} .flash{outline:3px solid var(--accent)}
.sortable-ghost{opacity:.3} .sortable-chosen{background:var(--tile)}
.addpage{width:120px;min-height:160px;border:2px dashed var(--line);border-radius:18px;display:grid;place-items:center;color:var(--muted);cursor:pointer;background:none}
#folders{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px}
.folder h3 input{font:inherit;font-weight:600;border:0;background:transparent;color:var(--ink);padding:2px 4px;border-radius:6px;min-width:0;flex:1}
.folder h3 input:focus{outline:2px solid var(--accent)}
.folder .list{display:grid;grid-template-columns:repeat(auto-fill,64px);gap:6px 8px;min-height:84px;padding:6px;border-radius:12px}
.folder .list > .app:nth-child(-n+9){background:var(--first)}
.newf{display:flex;gap:8px;margin:0 0 16px}
#trash ul{margin:0;padding-left:18px;columns:2;column-gap:24px} #trash li{cursor:pointer} #trash li:hover{text-decoration:line-through}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 14px;border-radius:9px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw}
@media (max-width:640px){#trash ul{columns:1} .grid{grid-template-columns:repeat(4,58px);gap:6px}}
</style></head><body>
<header>
  <h1>Home screen editor</h1>
  <input class="search" id="q" placeholder="Find an app" autocomplete="off">
  <span class="stat"><b id="ndel">0</b> to delete · <b id="gbdel">0 MB</b></span>
  <span id="status">Saved</span>
  <button class="primary" id="save" disabled>Save</button>
</header>
<main>
  <div class="banner" id="banner" hidden></div>
  <p class="hint">Drag apps, folders and widgets between the dock and pages, and apps in and out of folders.
  Click an app to mark it for deletion, click a folder on a page to jump to it.
  The shaded first 9 in each folder are what you see when you open it.
  Nothing changes on the phone until you run <code>organise apply</code> / <code>uninstall</code>.</p>
  <h2 class="sec">Home screen</h2>
  <div class="pages" id="pages"></div>
  <h2 class="sec">Folders</h2>
  <div class="newf"><input id="newname" placeholder="New folder name"><button id="addf">Add folder</button></div>
  <div id="folders"></div>
  <div class="card" id="trash" style="margin-top:24px"><h3>Marked for deletion <span class="count" id="trashcount"></span></h3><ul></ul></div>
</main>
<div id="toast"></div>
<script>
const D = __DATA__, TOKEN = "__TOKEN__";
const del = new Set(D.delete);
let dirty = false, fid = 0;
const $ = s => document.querySelector(s);
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const toast = m => { const t = $("#toast"); t.textContent = m; t.style.opacity = 1; clearTimeout(t._h); t._h = setTimeout(() => t.style.opacity = 0, 2200); };
const mb = n => n >= 1000 ? (n / 1000).toFixed(1) + " GB" : Math.round(n) + " MB";
const iconSrc = id => "/icons/" + encodeURIComponent(id) + ".png";
const slotsOf = e => e.classList.contains("widget") ? (D.wslots[e.dataset.size] || 8) : 1;
function touch() { dirty = true; $("#save").disabled = false; $("#status").textContent = "Unsaved changes"; refresh(); }

function appTile(id) {
  const a = D.apps[id] || { name: id, kind: "app" };
  const d = el("div", "app" + (del.has(id) ? " del" : "") + (a.size_mb === 0 && a.kind === "app" && !a.category ? "" : ""));
  d.dataset.id = id;
  d.title = `${a.name}${a.size_mb ? "\n" + mb(a.size_mb) : ""}${a.category ? " · " + a.category : ""}` +
            (a.kind !== "app" ? "\n(" + (a.kind === "webclip" ? "web clip" : "built-in Apple app") + ": can't delete here)" : "");
  if (a.icon) { const i = el("img"); i.src = iconSrc(id); i.alt = ""; i.loading = "lazy"; d.append(i); }
  else d.append(el("span", "ph", a.name.slice(0, 1).toUpperCase()));
  d.append(el("span", "name", a.name));
  d.addEventListener("click", () => {
    if (a.kind !== "app") { toast(a.kind === "webclip" ? "Web clips are removed on the phone" : "Built-in Apple apps can't be deleted from here"); return; }
    del.has(id) ? del.delete(id) : del.add(id); d.classList.toggle("del"); touch();
  });
  return d;
}

function folderTile(key) {
  const d = el("div", "ftile"); d.dataset.fkey = key;
  d.append(el("div", "mini"), el("span", "name"));
  d.addEventListener("click", () => { const c = document.querySelector(`.folder[data-fkey="${key}"]`); c.scrollIntoView({ block: "center", behavior: "smooth" }); c.classList.add("flash"); setTimeout(() => c.classList.remove("flash"), 1200); });
  return d;
}

function widgetTile(wid) {
  const w = D.widgets[wid] || { label: "Widget", size: "medium" };
  const d = el("div", "widget " + w.size); d.dataset.widget = wid; d.dataset.size = w.size;
  d.append(el("b", "", w.label), el("span", "", w.size + " widget"));
  return d;
}

const homeGroup = (kind) => ({ name: "home", put: (to, from, drag) => {
  const g = to.el;
  if (g.dataset.kind === "folder") return drag.classList.contains("app");
  if (g.dataset.kind === "dock") return drag.classList.contains("app") && (from.el === g || g.children.length < 4);
  const used = [...g.children].filter(c => c !== drag).reduce((s, c) => s + slotsOf(c), 0);
  return from.el === g || used + slotsOf(drag) <= D.slots;
}});
function sortable(box) { Sortable.create(box, { group: homeGroup(box.dataset.kind), animation: 120, onSort: touch, onAdd: touch, onRemove: touch }); }

function pageCard(tokens, title) {
  const c = el("section", "card page"); const h = el("h3");
  h.append(el("span", "ptitle", title)); const cnt = el("span", "count"); h.append(cnt);
  const rm = el("button", "small", "Remove"); rm.onclick = () => { c.remove(); touch(); }; h.append(rm);
  c.append(h);
  const g = el("div", "grid"); g.dataset.kind = "page";
  tokens.forEach(t => g.append(t.app ? appTile(t.app) : t.widget ? widgetTile(t.widget) : folderTile(keyOf[t.folder])));
  c.append(g); sortable(g); return c;
}

function folderCard(name, ids, key) {
  const c = el("section", "card folder"); c.dataset.fkey = key;
  const h = el("h3"); const n = el("input"); n.value = name; n.addEventListener("input", touch); h.append(n);
  const cnt = el("span", "count"); h.append(cnt);
  const rm = el("button", "small", "Remove"); rm.onclick = () => {
    if (c.querySelector(".list").children.length) return toast("Move its apps out first");
    c.remove(); document.querySelector(`.ftile[data-fkey="${key}"]`)?.remove(); touch(); };
  h.append(rm); c.append(h);
  const l = el("div", "list"); l.dataset.kind = "folder"; ids.forEach(i => l.append(appTile(i)));
  c.append(l); sortable(l); return c;
}

function refresh() {
  document.querySelectorAll(".page").forEach((c, n) => {
    const g = c.querySelector(".grid"), used = [...g.children].reduce((s, e) => s + slotsOf(e), 0);
    c.querySelector(".ptitle").textContent = "Page " + (n + 1);
    const cnt = c.querySelector(".count"); cnt.textContent = used + "/" + D.slots; cnt.classList.toggle("over", used > D.slots);
    c.querySelector("button").hidden = g.children.length > 0;
  });
  document.querySelectorAll(".folder").forEach(c => {
    const key = c.dataset.fkey, kids = [...c.querySelector(".list").children], name = c.querySelector("input").value;
    c.querySelector(".count").textContent = kids.length + (kids.length === 1 ? " app" : " apps");
    c.querySelector("button").hidden = kids.length > 0;
    const t = document.querySelector(`.ftile[data-fkey="${key}"]`); if (!t) return;
    t.querySelector(".name").textContent = name; t.title = name;
    const mini = t.querySelector(".mini"); mini.replaceChildren();
    kids.slice(0, 9).forEach(k => { const i = el("img"); i.src = iconSrc(k.dataset.id); i.alt = ""; mini.append(i); });
  });
  $("#ndel").textContent = del.size;
  $("#gbdel").textContent = mb([...del].reduce((s, i) => s + (D.apps[i]?.size_mb || 0), 0));
  const ul = $("#trash ul"); ul.replaceChildren();
  [...del].sort((a, b) => (D.apps[b]?.size_mb || 0) - (D.apps[a]?.size_mb || 0)).forEach(i => {
    const li = el("li", "", `${D.apps[i]?.name || i} — ${mb(D.apps[i]?.size_mb || 0)}`); li.title = "Click to unmark";
    li.onclick = () => { del.delete(i); document.querySelector(`.app[data-id="${CSS.escape(i)}"]`)?.classList.remove("del"); touch(); };
    ul.append(li);
  });
  $("#trashcount").textContent = del.size ? "click one to unmark" : "none yet";
}

// build
const keyOf = {}; Object.keys(D.folders).forEach(n => keyOf[n] = "f" + (fid++));
if (D.banner) { $("#banner").textContent = D.banner; $("#banner").hidden = false; }
const dock = el("section", "card dock"); dock.append(Object.assign(el("h3"), { textContent: "Dock" }));
const dg = el("div", "grid"); dg.dataset.kind = "dock"; D.dock.forEach(i => dg.append(appTile(i))); dock.append(dg); sortable(dg);
$("#pages").append(dock);
D.pages.forEach((p, n) => $("#pages").append(pageCard(p, "Page " + (n + 1))));
const add = el("button", "addpage", "+ Add page"); add.onclick = () => { $("#pages").insertBefore(pageCard([], ""), add); touch(); };
$("#pages").append(add);
Object.entries(D.folders).forEach(([n, ids]) => $("#folders").append(folderCard(n, ids, keyOf[n])));
$("#addf").onclick = () => {
  const name = $("#newname").value.trim(); if (!name) return;
  const key = "f" + (fid++);
  const pages = [...document.querySelectorAll(".page .grid")];
  const target = pages.sort((a, b) => b.querySelectorAll(".ftile").length - a.querySelectorAll(".ftile").length)
                      .find(g => [...g.children].reduce((s, e) => s + slotsOf(e), 0) < D.slots);
  if (!target) return toast("No page has room. Add a page first.");
  target.append(folderTile(key)); $("#folders").prepend(folderCard(name, [], key)); $("#newname").value = ""; touch();
};
$("#q").addEventListener("input", e => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll(".app").forEach(d => {
    const hit = q && (D.apps[d.dataset.id]?.name || "").toLowerCase().includes(q);
    d.classList.toggle("dim", !!q && !hit); d.classList.toggle("hit", !!hit);
  });
  document.querySelector(".app.hit")?.scrollIntoView({ block: "center", behavior: "smooth" });
});

async function save() {
  const nameOf = k => document.querySelector(`.folder[data-fkey="${k}"] input`).value.trim();
  const tok = e => e.classList.contains("app") ? { app: e.dataset.id } : e.classList.contains("widget") ? { widget: e.dataset.widget } : { folder: nameOf(e.dataset.fkey) };
  const body = {
    dock: [...dg.children].map(e => e.dataset.id),
    pages: [...document.querySelectorAll(".page .grid")].map(g => [...g.children].map(tok)),
    folders: Object.fromEntries([...document.querySelectorAll(".folder")].map(c => [c.querySelector("input").value.trim(), [...c.querySelectorAll(".app")].map(e => e.dataset.id)])),
    delete: [...del] };
  $("#status").textContent = "Saving…";
  try {
    const r = await fetch("/save/" + TOKEN, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json(); if (!j.ok) throw new Error(j.error);
    dirty = false; $("#save").disabled = true; $("#status").textContent = "Saved"; toast("Saved");
  } catch (e) { $("#status").textContent = "Not saved"; toast(e.message || "Save failed. Is the script still running?"); }
}
$("#save").onclick = save;
addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); if (dirty) save(); } });
addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
refresh();
</script></body></html>"""


async def organise():
    sub = sys.argv[2] if len(sys.argv) > 2 else ""
    steps = {"pull": organise_pull, "edit": organise_edit, "preview": organise_preview,
             "apply": organise_apply, "status": organise_status}
    if sub == "restore":
        await organise_restore(sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        await steps.get(sub, organise_status)()


async def fetch_icons(ld, ids):
    ICONS.mkdir(parents=True, exist_ok=True)
    async with SpringBoardServicesService(lockdown=ld) as sb:
        for i in ids:
            path = ICONS / f"{i}.png"
            if path.exists():
                continue
            try:
                png = await sb.get_icon_pngdata(i)
                if png:
                    path.write_bytes(png)
            except Exception:  # noqa: BLE001  web clips may have no icon over USB
                pass


def render_preview(layout) -> str:
    esc = html.escape

    def icon(x, cls="app"):
        name, i = label(x), item_id(x)
        img = (f'<img src="icons/{esc(i)}.png" alt="">' if (ICONS / f"{i}.png").exists()
               else f'<span class="ph">{esc(name[:1].upper())}</span>')
        return f'<div class="{cls}">{img}<span class="name">{esc(name)}</span></div>'

    def widget(x):
        size = x.get("gridSize", "medium")
        what = x.get("containerBundleIdentifier") or "Widget stack"
        return f'<div class="widget {esc(size)}"><span>{esc(what.split(".")[-1])} ({esc(size)})</span></div>'

    def folder_tile(x):
        minis = "".join(f'<img src="icons/{esc(item_id(y))}.png" alt="">' for y in x["iconLists"][0])
        return f'<div class="app"><div class="ftile">{minis}</div><span class="name">{esc(x["displayName"])}</span></div>'

    def page(title, cells, cls="page"):
        return f'<section><h2>{esc(title)}</h2><div class="{cls}">{"".join(cells)}</div></section>'

    parts = [page("Dock", [icon(x) for x in layout[0]], "page dock")]
    folders = []
    for n, pg in enumerate(layout[1:], start=1):
        cells = []
        for x in pg:
            if is_folder(x):
                cells.append(folder_tile(x))
                folders.append(x)
            elif is_widget(x):
                cells.append(widget(x))
            else:
                cells.append(icon(x))
        parts.append(page(f"Page {n}", cells))
    detail = []
    for f in folders:
        first, rest = f["iconLists"][0], [y for lst in f["iconLists"][1:] for y in lst]
        detail.append(
            f'<section class="folder"><h3>{esc(f["displayName"])} <small>{len(first) + len(rest)} apps</small></h3>'
            f'<div class="first">{"".join(icon(y) for y in first)}</div>'
            + (f'<div class="rest">{"".join(icon(y, "app sm") for y in rest)}</div>' if rest else "")
            + "</section>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Home Screen Preview</title>
<style>
:root{{--bg:#f4f4f6;--card:#fff;--ink:#1c1c1e;--muted:#6e6e73;--line:#d9d9de;--accent:#0a84ff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111113;--card:#1c1c1e;--ink:#f2f2f7;--muted:#98989f;--line:#38383c}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);font:14px -apple-system,system-ui,sans-serif}}
h1{{font-size:22px;margin:0 0 4px}} p.lede{{color:var(--muted);margin:0 0 24px}}
.pages{{display:flex;flex-wrap:wrap;gap:24px}} section h2{{font-size:13px;color:var(--muted);font-weight:600;margin:0 0 8px}}
.page{{display:grid;grid-template-columns:repeat(4,64px);grid-auto-rows:84px;gap:8px 14px;background:var(--card);
 border:1px solid var(--line);border-radius:28px;padding:18px;width:max-content;grid-auto-flow:row dense}}
.page.dock{{grid-auto-rows:84px}}
.app{{display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0}}
.app img,.ph{{width:56px;height:56px;border-radius:13px;display:grid;place-items:center;background:var(--line);font-weight:600}}
.name{{font-size:11px;max-width:64px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:center}}
.widget{{background:var(--line);border-radius:20px;display:grid;place-items:center;color:var(--muted);font-size:12px;text-align:center;padding:6px}}
.widget.small{{grid-column:span 2;grid-row:span 2}} .widget.medium{{grid-column:span 4;grid-row:span 2}}
.widget.large,.widget.extraLarge{{grid-column:span 4;grid-row:span 4}}
.ftile{{width:56px;height:56px;border-radius:13px;background:var(--line);display:grid;grid-template-columns:repeat(3,1fr);gap:2px;padding:5px;box-sizing:border-box}}
.ftile img{{width:100%;height:auto;border-radius:3px}}
.folders{{margin-top:40px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
.folder{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px}}
.folder h3{{margin:0 0 10px;font-size:15px}} .folder small{{color:var(--muted);font-weight:400}}
.first{{display:grid;grid-template-columns:repeat(3,64px);gap:8px 14px;padding:10px;border-radius:12px;outline:2px solid var(--accent);width:max-content}}
.rest{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}} .app.sm img,.app.sm .ph{{width:36px;height:36px;border-radius:9px}}
.app.sm .name{{max-width:48px;font-size:10px}}
@media (max-width:420px){{.page{{grid-template-columns:repeat(4,56px);gap:8px;padding:12px}}}}
</style></head><body>
<h1>Home screen preview</h1>
<p class="lede">Nothing has been written to the phone. The blue box in each folder is what you see when you open it.</p>
<div class="pages">{"".join(parts)}</div>
<h2 style="margin-top:40px">Inside each folder</h2>
<div class="folders">{"".join(detail)}</div>
</body></html>"""


async def organise_restore(arg: str | None):
    backups = layout_backups()
    if not backups:
        sys.exit("No saved layouts.")
    if not arg:
        print("Saved layouts (oldest first):")
        for b in backups:
            print(f"  {b}")
        print("\nThe oldest one is your layout from before this tool rearranged anything.")
        sys.exit("Run `organise restore <file>` with the one you want.")
    path = Path(arg).expanduser()
    saved = plistlib.loads(path.read_bytes())
    ld = await create_using_usbmux()
    current = await get_layout(ld)
    now = {item_id(x): x for x in items(current)}

    def keep(x):  # drop anything deleted since the backup
        if is_folder(x):
            x = {**x, "iconLists": [[y for y in lst if keep(y)] for lst in x["iconLists"]]}
            return x if any(x["iconLists"]) else None
        return x if is_widget(x) or item_id(x) in now else None

    pages = [[k for x in page if (k := keep(x))] for page in saved]
    inside = {item_id(x) for x in items(pages)}
    extra = [x for i, x in now.items() if i not in inside]  # apps that were App Library only
    first_extra = len(pages)
    pages += [extra[n:n + PAGE_SLOTS] for n in range(0, len(extra), PAGE_SLOTS)]
    print(f"This restores {path.name}: {first_extra - 1} pages plus the dock.")
    if extra:
        print(f"{len(extra)} apps that weren't on those pages go on pages {first_extra} to {len(pages) - 1}.")
        print("Hide those afterwards: long-press the home screen, tap the page dots, untick them.")
    if input("\nType yes to go ahead: ").strip().lower() != "yes":
        sys.exit("Cancelled.")
    print(f"Saved current layout to {save_layout_backup(current)}")
    await put_layout(ld, pages)
    print("Restored.")
    print("Then run `organise pull` so the stored layout matches.")


COMMANDS = {"report": report, "uninstall": uninstall, "clear-crashes": clear_crashes, "organise": organise}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in COMMANDS:
        asyncio.run(COMMANDS[cmd]())
    else:
        print(__doc__)
