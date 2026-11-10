# phone-service

Monthly iPhone housekeeping from your Mac, over a USB cable. It gives you a health report,
deletes apps in bulk, clears crash logs, and lets you rearrange your home screen by dragging
things around in a browser.

It's one Python file. You run it with [uv](https://docs.astral.sh/uv/), which installs its one
dependency, [pymobiledevice3](https://github.com/doronz88/pymobiledevice3), automatically. No
jailbreak, no app on the phone, no Apple developer account.

**Nothing on the phone is deleted or rearranged until you've seen exactly what will change and
typed `yes`.**

## Requirements

- A Mac. pymobiledevice3 also runs on Linux and Windows, but this tool has only been tested on macOS.
- An iPhone and a USB cable. The phone must be unlocked, and you tap **Trust** the first time.
- uv: `brew install uv`, or see the [uv install guide](https://docs.astral.sh/uv/getting-started/installation/).

Tested with iOS 27.0.1, macOS 26.6 and pymobiledevice3 11.50.3. Apple changes these services
between iOS versions, so other versions may behave differently. Each report section fails on
its own without stopping the rest.

## Quick start

```sh
git clone https://github.com/viewingstudio/iphone-housekeeping.git
cd iphone-housekeeping
uv run phone_service.py report
```

## Commands

| Command | What it does |
|---|---|
| `report` | Read-only. iCloud Backup status and age, storage, battery health (cycle count and capacity), configuration profiles, crash reports, the biggest videos, and apps by size. Saves a Markdown report and `apps.csv`. |
| `uninstall` | Deletes the apps you've marked, along with their data. Mark them in the editor, or put an `x` in the `delete` column of `apps.csv`. |
| `clear-crashes` | Lists every crash and diagnostic log on the phone, then deletes them. |
| `organise` | Status: is the phone in sync with your stored layout? |
| `organise pull` | Copies the phone's home screen (pages, folders, widgets, order) to your Mac. |
| `organise edit` | Opens the layout editor in your browser (see below). |
| `organise preview` | A picture of the stored layout with real icons. |
| `organise apply` | Pushes the stored layout to the phone. Saves the phone's current layout first. |
| `organise restore [file]` | Puts a saved phone layout back. Without a file, it lists them. |

Run them all as `uv run phone_service.py <command>`.

## Organising the home screen

`organise pull` copies the phone's home screen to your Mac. `organise edit` opens a page that only
this Mac can reach, where you can:

- drag apps, folders and widgets between the dock and pages, and apps in and out of folders;
- add, rename or remove pages and folders;
- click an app to mark it for deletion (`uninstall` acts on these marks);
- see each page's slot count (a page holds 24: an app or folder is 1, widgets are 4, 8 or 16)
  and which 9 apps show when a folder is opened.

`organise apply` then writes the layout to the phone.

You can change things on either side. Running `organise` tells you which side has changed:
pull to bring phone changes to the Mac, apply to push your edits to the phone. Apps installed
since your last pull are filed automatically into a suggested folder, based on Apple's App
Store category.

### Your own folder rules

Create `~/PhoneService/config.json` to set where specific apps go and which App Store region
to use for category lookups:

```json
{
  "country": "gb",
  "folders": { "com.tesco.grocery": "Food", "com.dji.go": "Photo & Video" },
  "webclip_folders": { "My Notes": "Work" }
}
```

To find an app's bundle ID, look in `~/PhoneService/layout.json` or `apps.csv`.

## A monthly routine

1. `report`. Check the **iCloud Backup** line first. If it's over a week old, back up the
   phone (Settings > [your name] > iCloud > iCloud Backup > Back Up Now) before deleting anything.
2. `organise pull` (if you've rearranged anything on the phone), then `organise edit`.
3. `uninstall`, then `clear-crashes`.
4. `organise apply`.

This tool doesn't back up the phone. Use iCloud Backup, or Finder.

## Known limitations

- **Writing a layout puts every app on the home screen.** iOS has no "App Library only" flag
  in this service, so `apply` gives every app a place, usually in a folder. `restore` puts any
  apps missing from a saved layout on extra pages at the end. To hide those, long-press the
  home screen, tap the page dots and untick them.
- **iOS can drop a widget when a layout is written.** `apply` checks afterwards and names any
  widget that disappeared. Add it back by hand.
- **No usage data.** iOS doesn't share last-opened dates or Screen Time data over USB.
- **Crash reports.** On iOS 27 the step that files the very newest reports often drops the
  connection, so it's best-effort. Listing and deleting reports both work.
- **Battery.** On iOS 27 the capacity figures are nested in `BatteryData`, and
  `AppleRawMaxCapacity` no longer exists. The report compares `NominalChargeCapacity` with
  `DesignCapacity`.
- **Video dates** in the report are when the file reached the phone, not when it was shot.
- **Web clips** (home-screen bookmarks) and Apple's built-in apps can't be deleted with this tool.

## Privacy

- Everything stays on your Mac in `~/PhoneService/`.
- To look up App Store categories, the bundle IDs of your installed apps are sent to Apple's
  public lookup service at `itunes.apple.com`. No account or personal details are sent. The
  results are cached in `categories.json`.
- The editor only accepts connections from this Mac, and each run uses a random token. The
  page loads [SortableJS](https://sortablejs.github.io/Sortable/) from cdnjs.

## Files in `~/PhoneService`

| File | What it is |
|---|---|
| `report-*.md`, `apps.csv` | Output of `report` |
| `layout.json`, `layout.prev.json` | Your stored layout, and the version before your last save |
| `layout-backup-*.plist` | The phone's layout, saved before every `apply`/`restore`. The oldest is from before this tool first changed anything. |
| `config.json` | Your folder rules (optional) |
| `categories.json`, `icons/` | Caches |

## Disclaimer

Not affiliated with or endorsed by Apple. Deleting an app deletes its data, and apps that are
no longer on the App Store can't be downloaded again. Use it at your own risk, and keep a
backup.

## License

See [LICENSE](LICENSE). Built on [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)
(GPL-3.0-or-later).
