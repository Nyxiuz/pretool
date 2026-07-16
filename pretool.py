#!/usr/bin/env python3
"""
pretool - SITE PRE tool for cbftp HTTPS/JSON API

Features:
  * Manage sites (cbftp name, groupdir, active/inactive)
  * Manage sections with regexp for automatic release detection
  * List releases in groupdirs on all active sites
  * Check completeness via zipscript tag dirs before pre
  * Send SITE PRE to one or more sites simultaneously
  * Online check with latency measurement per site
  * API log for debugging

Dependencies: pip install textual requests tomli-w
Config:       config.toml next to the script (created automatically on first run)
Requires:     Python 3.11+ (tomllib)
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests
import urllib3

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

import tomli_w

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Log,
    Static,
    TextArea,
)

urllib3.disable_warnings()

CONFIG_PATH = Path(__file__).with_name("config.toml")

DEFAULT_CONFIG: dict = {
    "api": {
        "url": "https://127.0.0.1:10302",
        "password": "CHANGE_ME",
        "verify_ssl": False,
        "timeout": 15,
    },
    "defaults": {
        # {release} and {section} are substituted. Empty section => trimmed away.
        "pre_command": "site pre {release} {section}",
        # Tag dirs that mean COMPLETE (regexp, tested against dir names in the release)
        "complete_patterns": [
            "(?i)\\(\\s*100\\s*%[^)]*\\)",
            "(?i)\\[\\s*100\\s*%[^]]*\\]",
            "(?i)-\\s*100%\\s*complete",
            "(?i)\\d+F\\s*-\\s*COMPLETE",
        ],
        # How percent is extracted from a tag dir
        "percent_pattern": "(\\d{1,3})\\s*%",
        # Coarse filter: dirs that look like zipscript tags at all
        "tag_hint_pattern": "(?i)%|complete|incomplete",
    },
    "sites": [],
    "sections": [],
}


# ---------------------------------------------------------------- config ----

def _deepcopy(obj):
    return json.loads(json.dumps(obj))


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        cfg = _deepcopy(DEFAULT_CONFIG)
        save_config(cfg)
        return cfg
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    for key, val in DEFAULT_CONFIG.items():
        if key not in cfg:
            cfg[key] = _deepcopy(val)
        elif isinstance(val, dict):
            for k2, v2 in val.items():
                cfg[key].setdefault(k2, _deepcopy(v2))
    # Migrate old single group_dir to affils list
    for site in cfg.get("sites", []):
        if "group_dir" in site and "affils" not in site:
            gd = site.pop("group_dir")
            affil_name = gd.rstrip("/").rsplit("/", 1)[-1]
            site["affils"] = [{"name": affil_name, "path": gd}]
        site.setdefault("affils", [])
        site.setdefault("section_map", {})
    return cfg


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("wb") as f:
        tomli_w.dump(cfg, f)


# ------------------------------------------------------------- cbftp-API ----

class CbApi:
    """Thin wrapper around cbftp /raw endpoint. Defensive parsing since
    response format can differ between revisions."""

    def __init__(self, cfg: dict, logger=None):
        a = cfg["api"]
        self.url = a["url"].rstrip("/")
        self.auth = ("", a["password"])
        self.verify = bool(a.get("verify_ssl", False))
        self.timeout = int(a.get("timeout", 15))
        self.logger = logger

    def _log(self, msg: str) -> None:
        if self.logger:
            self.logger(msg)

    def raw(self, command: str, sites: list[str], path: str | None = None) -> dict[str, str]:
        """Sends a raw command to one or more sites.
        Returns {sitename: response_text}."""
        payload: dict = {"command": command, "sites": sites}
        if path:
            payload["path"] = path
        self._log(f">>> POST /raw {json.dumps(payload)}")
        r = requests.post(
            f"{self.url}/raw",
            json=payload,
            auth=self.auth,
            verify=self.verify,
            timeout=self.timeout,
        )
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            self._log(f"<<< (not JSON) {r.text[:400]}")
            return {s: r.text for s in sites}
        self._log(f"<<< {json.dumps(data)[:1500]}")
        results = self._extract(data)
        if not results:
            results = {s: json.dumps(data, indent=2) for s in sites}
        return results

    @staticmethod
    def _extract(data) -> dict[str, str]:
        out: dict[str, str] = {}

        def as_text(v) -> str:
            if isinstance(v, list):
                return "\n".join(str(x) for x in v)
            if isinstance(v, dict):
                return json.dumps(v, indent=2)
            return str(v)

        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("site") or "?"
                for f in ("result", "raw_result", "rawResult", "data", "response", "text"):
                    if f in item:
                        out[str(name)] = as_text(item[f])
                        break
                else:
                    out[str(name)] = json.dumps(item, indent=2)
        elif isinstance(data, dict):
            for f in ("successes", "results", "sites", "data"):
                if isinstance(data.get(f), list):
                    return CbApi._extract(data[f])
            for k, v in data.items():
                out[str(k)] = as_text(v)
        return out


# --------------------------------------------------------------- parsing ----

CODE_PREFIX_RE = re.compile(r"^\d{3}[- ]\s?")


def parse_listing(text: str) -> list[tuple[str, bool]]:
    """Parses unix-style listing from STAT -l / LIST response.
    Returns [(name, is_dir), ...]."""
    entries: list[tuple[str, bool]] = []
    for line in text.splitlines():
        line = CODE_PREFIX_RE.sub("", line.strip())
        if not line or line[0] not in "d-l":
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        name = parts[8]
        if name in (".", ".."):
            continue
        entries.append((name, line[0] == "d"))
    return entries


def guess_section(cfg: dict, release: str) -> str:
    for sec in cfg.get("sections", []):
        try:
            if re.search(sec["pattern"], release):
                return sec["name"]
        except re.error:
            continue
    return ""


def completeness(cfg: dict, entries: list[tuple[str, bool]]) -> tuple[str, int | None]:
    """Checks zipscript tag dirs in a release.
    Returns (status_text, percent|None)."""
    d = cfg["defaults"]
    hint = re.compile(d["tag_hint_pattern"])
    completes = [re.compile(p) for p in d["complete_patterns"]]
    pct_re = re.compile(d["percent_pattern"])
    best: tuple[int, str] | None = None
    for name, _is_dir in entries:
        if not hint.search(name):
            continue
        if any(c.search(name) for c in completes):
            return ("COMPLETE", 100)
        m = pct_re.search(name)
        if m:
            pct = int(m.group(1))
            if pct >= 100:
                return ("COMPLETE", 100)
            if best is None or pct > best[0]:
                best = (pct, name)
    if best:
        return (f"{best[0]}%", best[0])
    return ("no tag", None)


def enabled_sites(cfg: dict) -> list[dict]:
    return [s for s in cfg.get("sites", []) if s.get("enabled", True)]


# ------------------------------------------------------------------ modals ----

class SiteModal(ModalScreen):
    """Add / edit site."""

    def __init__(self, site: dict | None):
        super().__init__()
        self.site = site

    def compose(self) -> ComposeResult:
        s = self.site or {}
        affils_text = "\n".join(
            f"{a['name']} {a['path']}" for a in s.get("affils", [])
        )
        secmap_text = "\n".join(
            f"{k}={v}" for k, v in s.get("section_map", {}).items()
        )
        with Vertical(id="modalbox"):
            yield Label("Site (name as known by cbftp)")
            yield Input(value=s.get("name", ""), placeholder="SITENAME", id="in_name")
            yield Label("Affils (one per line: NAME /path)")
            yield TextArea(affils_text, id="ta_affils")
            yield Label("Section map (one per line: GLOBAL=SITELOCAL)")
            yield TextArea(secmap_text, id="ta_secmap")
            yield Checkbox("Active", value=s.get("enabled", True), id="cb_enabled")
            with Horizontal(id="buttons"):
                yield Button("Save", id="btn_save")
                yield Button("Cancel", id="btn_cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_save":
            name = self.query_one("#in_name", Input).value.strip()
            if not name:
                self.app.notify("Site name is required", severity="error")
                return
            # Parse affils
            affils: list[dict] = []
            for line in self.query_one("#ta_affils", TextArea).text.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    self.app.notify(
                        f"Bad affil line (expected NAME /path): {line}",
                        severity="error",
                    )
                    return
                affils.append({"name": parts[0], "path": parts[1].rstrip("/")})
            if not affils:
                self.app.notify("At least one affil is required", severity="error")
                return
            # Parse section map
            section_map: dict[str, str] = {}
            for line in self.query_one("#ta_secmap", TextArea).text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "=" not in line:
                    self.app.notify(
                        f"Bad section map line (expected GLOBAL=SITE): {line}",
                        severity="error",
                    )
                    return
                k, v = line.split("=", 1)
                section_map[k.strip()] = v.strip()
            self.dismiss({
                "name": name,
                "affils": affils,
                "section_map": section_map,
                "enabled": self.query_one("#cb_enabled", Checkbox).value,
            })
        else:
            self.dismiss(None)


class SectionModal(ModalScreen):
    """Add / edit section with regexp."""

    def __init__(self, section: dict | None):
        super().__init__()
        self.section = section

    def compose(self) -> ComposeResult:
        s = self.section or {}
        with Vertical(id="modalbox"):
            yield Label("Section name (as the pre-script expects it)")
            yield Input(value=s.get("name", ""), placeholder="TV-1080", id="in_name")
            yield Label("Regexp matching release names")
            yield Input(
                value=s.get("pattern", ""),
                placeholder="(?i)S\\d{2}E\\d{2}.*1080p",
                id="in_pattern",
            )
            with Horizontal(id="buttons"):
                yield Button("Save", id="btn_save")
                yield Button("Cancel", id="btn_cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_save":
            name = self.query_one("#in_name", Input).value.strip()
            pattern = self.query_one("#in_pattern", Input).value.strip()
            if not name or not pattern:
                self.app.notify("Name and regexp are required", severity="error")
                return
            try:
                re.compile(pattern)
            except re.error as e:
                self.app.notify(f"Invalid regexp: {e}", severity="error")
                return
            self.dismiss({"name": name, "pattern": pattern})
        else:
            self.dismiss(None)


class ConfirmModal(ModalScreen):
    BINDINGS = [
        ("y", "yes", "Yes"),
        ("n", "no", "No"),
        ("escape", "no", "No"),
    ]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmbox"):
            yield Label(self.question)
            yield Label("[b]y[/b]:Yes  [b]n[/b]:No")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ResultModal(ModalScreen):
    """Shows responses from sites after a pre."""

    def __init__(self, title: str, text: str):
        super().__init__()
        self.title_text = title
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="resultbox"):
            yield Label(self.title_text)
            log = Log(id="resultlog")
            yield log
            with Horizontal(id="buttons"):
                yield Button("Ok", id="btn_ok")

    def on_mount(self) -> None:
        self.query_one("#resultlog", Log).write(self.text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class ConfirmPreModal(ModalScreen):
    """Shows command preview and asks for confirmation."""

    def __init__(self, preview: str):
        super().__init__()
        self.preview = preview

    def compose(self) -> ComposeResult:
        with Vertical(id="resultbox"):
            yield Label("[b]Commands to send:[/b]")
            log = Log(id="previewlog")
            yield log
            with Horizontal(id="buttons"):
                yield Button("Confirm", id="btn_confirm")
                yield Button("Cancel", id="btn_cancel")

    def on_mount(self) -> None:
        self.query_one("#previewlog", Log).write(self.preview)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn_confirm")


class PreModal(ModalScreen):
    """Select sites + section and send SITE PRE for a release."""

    def __init__(self, release: str, entries: list[dict]):
        super().__init__()
        self.release = release
        self.entries = entries  # each: {"site": dict, "affil_path": str, "affil_name": str}

    def _entry_label(self, entry: dict) -> str:
        label = entry["site"]["name"]
        if len(entry["site"].get("affils", [])) > 1:
            label += f" ({entry['affil_name']})"
        return label

    def compose(self) -> ComposeResult:
        with Vertical(id="modalbox"):
            yield Label(f"[b]{self.release}[/b]")
            yield Label("Section")
            yield Input(
                value=guess_section(self.app.cfg, self.release),
                placeholder="leave empty if pre-script guesses on its own",
                id="in_section",
            )
            yield Label("Sites (complete ones are auto-checked)")
            with VerticalScroll(id="sitelist"):
                for i, entry in enumerate(self.entries):
                    with Horizontal(classes="siterow"):
                        yield Checkbox(self._entry_label(entry), value=False, id=f"cb_{i}")
                        yield Static("checking completeness...", id=f"st_{i}", classes="sitestatus")
            with Horizontal(id="buttons"):
                yield Button("Send SITE PRE", id="btn_pre", disabled=True)
                yield Button("Cancel", id="btn_cancel")

    def on_mount(self) -> None:
        self.check_completeness()

    @work(thread=True)
    def check_completeness(self) -> None:
        api = self.app.make_api()
        for i, entry in enumerate(self.entries):
            site_name = entry["site"]["name"]
            path = f"{entry['affil_path']}/{self.release}"
            try:
                res = api.raw(f"stat -l {path}", [site_name])
                text = next(iter(res.values()), "")
                entries = parse_listing(text)
                tag_status, pct = completeness(self.app.cfg, entries)

                # Check for required files/dirs
                names_lower = [(n.lower(), is_dir) for n, is_dir in entries]
                has_nfo = any(n.endswith(".nfo") and not d for n, d in names_lower)
                has_sfv = any(n.endswith(".sfv") and not d for n, d in names_lower)
                has_sample = any(n == "sample" and d for n, d in names_lower)
                has_proof = any(n == "proof" and d for n, d in names_lower)

                # Verify Sample dir contains files
                if has_sample:
                    try:
                        sres = api.raw(f"stat -l {path}/Sample", [site_name])
                        sample_entries = parse_listing(next(iter(sres.values()), ""))
                        has_sample = any(not is_dir for _, is_dir in sample_entries)
                    except Exception:
                        has_sample = False

                # Verify Proof dir contains files
                if has_proof:
                    try:
                        pres = api.raw(f"stat -l {path}/Proof", [site_name])
                        proof_entries = parse_listing(next(iter(pres.values()), ""))
                        has_proof = any(not is_dir for _, is_dir in proof_entries)
                    except Exception:
                        has_proof = False

                # Build missing list
                missing = []
                if not has_nfo:
                    missing.append("nfo")
                if not has_sfv:
                    missing.append("sfv")
                if not has_sample:
                    missing.append("Sample")
                if not has_proof:
                    missing.append("Proof")

                # Build combined status string
                if missing:
                    status = f"{tag_status} | missing: {', '.join(missing)}"
                else:
                    status = tag_status

                ok = pct == 100 and not missing
            except Exception as e:
                status, ok = f"ERROR: {e}", False
            self.app.call_from_thread(self._set_status, i, status, ok)
        self.app.call_from_thread(self._enable_send)

    def _set_status(self, i: int, status: str, ok: bool) -> None:
        st = self.query_one(f"#st_{i}", Static)
        st.update(f"[b]{status}[/b]" if ok else f"{status}")
        cb = self.query_one(f"#cb_{i}", Checkbox)
        if ok:
            cb.value = True
        else:
            cb.disabled = True

    def _enable_send(self) -> None:
        self.query_one("#btn_pre", Button).disabled = False

    def _build_commands(self, chosen: list[dict], section: str) -> list[tuple[dict, str]]:
        """Build (entry, command) pairs for preview."""
        cmd_tpl = self.app.cfg["defaults"]["pre_command"]
        result = []
        for entry in chosen:
            site = entry["site"]
            section_map = site.get("section_map", {})
            mapped_section = section_map.get(section, section) if section else ""
            cmd = cmd_tpl.format(release=self.release, section=mapped_section).strip()
            cmd = re.sub(r"\s+", " ", cmd)
            result.append((entry, cmd))
        return result

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_pre":
            chosen = [
                entry for i, entry in enumerate(self.entries)
                if self.query_one(f"#cb_{i}", Checkbox).value
            ]
            if not chosen:
                self.app.notify("No sites selected", severity="error")
                return
            section = self.query_one("#in_section", Input).value.strip()
            pairs = self._build_commands(chosen, section)
            preview = "\n".join(
                f"{entry['site']['name']:>10}  >>  {cmd}" for entry, cmd in pairs
            )

            def on_confirm(confirmed: bool | None) -> None:
                if confirmed:
                    self.dismiss({"release": self.release, "section": section, "sites": chosen})

            self.app.push_screen(ConfirmPreModal(preview), on_confirm)
        else:
            self.dismiss(None)


# ----------------------------------------------------------------- screens ----

class SitesScreen(Screen):
    BINDINGS = [
        ("a", "add", "Add"),
        ("e", "edit", "Edit"),
        ("d", "delete", "Delete"),
        ("r", "recheck", "Recheck"),
        ("escape", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("pretool | Sites", id="topbar")
        yield DataTable(cursor_type="row", id="table")
        yield Static("a:Add  e:Edit  d:Delete  r:Recheck  esc:Back", id="bottombar")
        yield Static(" ", id="marquee")

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        self._cols = t.add_columns("Site", "Affils", "Active", "Status", "Response time")
        self._rows: dict[str, object] = {}
        self.refresh_table()
        self.run_checks()

    def refresh_table(self) -> None:
        t = self.query_one("#table", DataTable)
        t.clear()
        self._rows = {}
        for s in self.app.cfg["sites"]:
            affil_names = ", ".join(a["name"] for a in s.get("affils", []))
            active = "yes" if s.get("enabled", True) else "no"
            if s.get("enabled", True):
                key = t.add_row(s["name"], affil_names or "-", active, "checking...", "-")
                self._rows[s["name"]] = key
            else:
                t.add_row(s["name"], affil_names or "-", active, "-", "-")

    def action_recheck(self) -> None:
        self.refresh_table()
        self.run_checks()

    @work(thread=True, exclusive=True)
    def run_checks(self) -> None:
        api = self.app.make_api()
        for s in enabled_sites(self.app.cfg):
            name = s["name"]
            t0 = time.monotonic()
            try:
                res = api.raw("noop", [name])
                ms = (time.monotonic() - t0) * 1000
                text = next(iter(res.values()), "").lower()
                if "fail" in text or "error" in text or "unable" in text:
                    status, rtime = "[red]ERROR[/red]", f"{ms:.0f} ms"
                else:
                    status, rtime = "[green]ONLINE[/green]", f"{ms:.0f} ms"
            except Exception as e:
                status, rtime = f"[red]OFFLINE ({type(e).__name__})[/red]", "-"
            self.app.call_from_thread(self._update_row, name, status, rtime)

    def _update_row(self, name: str, status: str, rtime: str) -> None:
        t = self.query_one("#table", DataTable)
        key = self._rows.get(name)
        if key is not None:
            t.update_cell(key, self._cols[3], status)
            t.update_cell(key, self._cols[4], rtime)

    def _idx(self) -> int | None:
        t = self.query_one("#table", DataTable)
        if not self.app.cfg["sites"] or t.cursor_row is None:
            return None
        return t.cursor_row

    def action_add(self) -> None:
        def done(result: dict | None) -> None:
            if result:
                self.app.cfg["sites"].append(result)
                save_config(self.app.cfg)
                self.refresh_table()
        self.app.push_screen(SiteModal(None), done)

    def action_edit(self) -> None:
        i = self._idx()
        if i is None:
            return
        def done(result: dict | None) -> None:
            if result:
                self.app.cfg["sites"][i] = result
                save_config(self.app.cfg)
                self.refresh_table()
        self.app.push_screen(SiteModal(self.app.cfg["sites"][i]), done)

    def action_delete(self) -> None:
        i = self._idx()
        if i is None:
            return
        name = self.app.cfg["sites"][i]["name"]
        def done(yes: bool | None) -> None:
            if yes:
                del self.app.cfg["sites"][i]
                save_config(self.app.cfg)
                self.refresh_table()
        self.app.push_screen(ConfirmModal(f"Delete {name}?"), done)


class SectionsScreen(Screen):
    BINDINGS = [
        ("a", "add", "Add"),
        ("e", "edit", "Edit"),
        ("d", "delete", "Delete"),
        ("escape", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("pretool | Sections", id="topbar")
        yield DataTable(cursor_type="row", id="table")
        yield Static("a:Add  e:Edit  d:Delete  esc:Back", id="bottombar")
        yield Static(" ", id="marquee")

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("Section", "Regexp")
        self.refresh_table()

    def refresh_table(self) -> None:
        t = self.query_one("#table", DataTable)
        t.clear()
        for s in self.app.cfg["sections"]:
            t.add_row(s["name"], s["pattern"])

    def _idx(self) -> int | None:
        t = self.query_one("#table", DataTable)
        if not self.app.cfg["sections"] or t.cursor_row is None:
            return None
        return t.cursor_row

    def action_add(self) -> None:
        def done(result: dict | None) -> None:
            if result:
                self.app.cfg["sections"].append(result)
                save_config(self.app.cfg)
                self.refresh_table()
        self.app.push_screen(SectionModal(None), done)

    def action_edit(self) -> None:
        i = self._idx()
        if i is None:
            return
        def done(result: dict | None) -> None:
            if result:
                self.app.cfg["sections"][i] = result
                save_config(self.app.cfg)
                self.refresh_table()
        self.app.push_screen(SectionModal(self.app.cfg["sections"][i]), done)

    def action_delete(self) -> None:
        i = self._idx()
        if i is None:
            return
        name = self.app.cfg["sections"][i]["name"]
        def done(yes: bool | None) -> None:
            if yes:
                del self.app.cfg["sections"][i]
                save_config(self.app.cfg)
                self.refresh_table()
        self.app.push_screen(ConfirmModal(f"Delete {name}?"), done)


class PreScreen(Screen):
    """Lists releases in groupdirs on all active sites."""

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("p", "pre", "Pre selected"),
        ("escape", "app.pop_screen", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("pretool | Pre Release", id="topbar")
        yield Static("Fetching releases from groupdirs...", id="status")
        yield DataTable(cursor_type="row", id="table")
        yield Static("enter:Select  esc:Back", id="bottombar")
        yield Static(" ", id="marquee")

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("Release", "Section (guessed)", "Found on")
        self.releases: dict[str, list[dict]] = {}
        self._order: list[str] = []
        self.action_refresh()

    def action_refresh(self) -> None:
        self.query_one("#status", Static).update("Fetching releases from groupdirs...")
        self.fetch()

    @work(thread=True, exclusive=True)
    def fetch(self) -> None:
        api = self.app.make_api()
        releases: dict[str, list[dict]] = {}
        errors: list[str] = []
        for s in enabled_sites(self.app.cfg):
            for affil in s.get("affils", []):
                try:
                    res = api.raw(f"stat -l {affil['path']}", [s["name"]])
                    text = next(iter(res.values()), "")
                    for name, is_dir in parse_listing(text):
                        if not is_dir:
                            continue
                        # Match release group tag to affil name
                        tag = name.rsplit("-", 1)[-1] if "-" in name else ""
                        if tag.upper() != affil["name"].upper():
                            continue
                        releases.setdefault(name, []).append({
                            "site": s,
                            "affil_path": affil["path"],
                            "affil_name": affil["name"],
                        })
                except Exception as e:
                    errors.append(f"{s['name']}/{affil['name']}: {e}")
        self.app.call_from_thread(self._populate, releases, errors)

    def _populate(self, releases: dict[str, list[dict]], errors: list[str]) -> None:
        self.releases = releases
        t = self.query_one("#table", DataTable)
        t.clear()
        self._order = sorted(releases.keys())
        for name in self._order:
            # Deduplicate site names for display
            seen: list[str] = []
            for entry in releases[name]:
                sn = entry["site"]["name"]
                if sn not in seen:
                    seen.append(sn)
            t.add_row(name, guess_section(self.app.cfg, name) or "-", ", ".join(seen))
        status = f"{len(releases)} releases found."
        if errors:
            status += "  Errors: " + "; ".join(errors)
        if not enabled_sites(self.app.cfg):
            status = "No active sites - add them under Sites in the main menu."
        self.query_one("#status", Static).update(status)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_pre()

    def action_pre(self) -> None:
        t = self.query_one("#table", DataTable)
        if not self._order or t.cursor_row is None:
            return
        release = self._order[t.cursor_row]
        sites = self.releases.get(release, [])
        if not sites:
            return

        def done(result: dict | None) -> None:
            if result:
                self.send_pre(result)

        self.app.push_screen(PreModal(release, sites), done)

    @work(thread=True)
    def send_pre(self, job: dict) -> None:
        api = self.app.make_api()
        cmd_tpl = self.app.cfg["defaults"]["pre_command"]
        release, section = job["release"], job["section"]
        lines: list[str] = []
        for entry in job["sites"]:
            site = entry["site"]
            # Translate global section name via site's section_map
            section_map = site.get("section_map", {})
            mapped_section = section_map.get(section, section) if section else ""
            cmd = cmd_tpl.format(release=release, section=mapped_section).strip()
            cmd = re.sub(r"\s+", " ", cmd)  # trim if section was empty
            try:
                res = api.raw(cmd, [site["name"]], path=entry["affil_path"])
                text = next(iter(res.values()), "(empty response)")
                lines.append(f"=== {site['name']} ===\n{text}\n")
            except Exception as e:
                lines.append(f"=== {site['name']} ===\nERROR: {e}\n")
        self.app.call_from_thread(
            self.app.push_screen, ResultModal(f"PRE: {release}", "\n".join(lines))
        )


class LogScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("s", "save_log", "Save log"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("pretool | API Log", id="topbar")
        yield Log(id="apilog")
        yield Static("s:Save  esc:Back", id="bottombar")
        yield Static(" ", id="marquee")

    def on_mount(self) -> None:
        log = self.query_one("#apilog", Log)
        for line in self.app.log_lines:
            log.write_line(line)

    def action_save_log(self) -> None:
        path = os.path.join(os.path.dirname(__file__), "pretool_debug.log")
        with open(path, "w") as f:
            f.write("\n".join(self.app.log_lines))
        self.notify(f"Log saved to {path}")


class SettingsScreen(Screen):
    """Edit API connection (url, password, port, timeout, ssl)."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        a = self.app.cfg["api"]
        yield Static("pretool | Settings", id="topbar")
        with VerticalScroll(id="settingsbox"):
            yield Label("cbftp API URL (https://host:port)")
            yield Input(value=a.get("url", ""), placeholder="https://127.0.0.1:10302", id="in_url")
            yield Label("API password")
            yield Input(value=a.get("password", ""), password=True, id="in_pass")
            yield Checkbox("Show password", value=False, id="cb_showpass")
            yield Checkbox("Verify SSL cert", value=a.get("verify_ssl", False), id="cb_verify")
            yield Label("Timeout (seconds)")
            yield Input(value=str(a.get("timeout", 15)), id="in_timeout")
            yield Static("", id="settings_status")
            with Horizontal(id="buttons"):
                yield Button("Test connection", id="btn_test")
                yield Button("Save", id="btn_save")
        yield Static("esc:Back", id="bottombar")
        yield Static(" ", id="marquee")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "cb_showpass":
            self.query_one("#in_pass", Input).password = not event.value

    def _collect(self) -> dict | None:
        url = self.query_one("#in_url", Input).value.strip().rstrip("/")
        password = self.query_one("#in_pass", Input).value
        timeout_raw = self.query_one("#in_timeout", Input).value.strip()
        if not url or not url.startswith(("http://", "https://")):
            self.app.notify("URL must start with http:// or https://", severity="error")
            return None
        if not password:
            self.app.notify("Password is required", severity="error")
            return None
        try:
            timeout = int(timeout_raw)
        except ValueError:
            self.app.notify("Timeout must be an integer", severity="error")
            return None
        return {
            "url": url,
            "password": password,
            "verify_ssl": self.query_one("#cb_verify", Checkbox).value,
            "timeout": timeout,
        }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_save":
            data = self._collect()
            if data is None:
                return
            self.app.cfg["api"].update(data)
            save_config(self.app.cfg)
            self.app.notify("Saved")
        elif event.button.id == "btn_test":
            data = self._collect()
            if data is None:
                return
            self.query_one("#settings_status", Static).update("Testing...")
            self.run_test(data)

    @work(thread=True)
    def run_test(self, data: dict) -> None:
        tmp_cfg = _deepcopy(self.app.cfg)
        tmp_cfg["api"].update(data)
        api = CbApi(tmp_cfg, logger=self.app.add_log)
        t0 = time.monotonic()
        try:
            api.raw("noop", [s["name"] for s in enabled_sites(self.app.cfg)] or ["_"])
            ms = (time.monotonic() - t0) * 1000
            msg = f"OK - response in {ms:.0f} ms"
        except requests.exceptions.SSLError as e:
            msg = f"SSL error: {e}. Try with Verify SSL off."
        except requests.exceptions.ConnectionError as e:
            msg = f"Cannot reach API: {e}"
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            if code == 401:
                msg = "401 Unauthorized - wrong password"
            else:
                msg = f"HTTP error {code}"
        except Exception as e:
            msg = f"Error: {type(e).__name__}: {e}"
        self.app.call_from_thread(self.query_one("#settings_status", Static).update, msg)


# --------------------------------------------------------------------- app ----

class PretoolApp(App):
    TITLE = "pretool"
    SUB_TITLE = "SITE PRE via cbftp"

    CSS = """
    * { background: black; color: white; }
    Screen { background: black; color: white; }
    #topbar { height: 1; background: white; color: black; padding: 0 1; }
    #bottombar { height: 1; background: white; color: black; padding: 0 1; dock: bottom; }
    #marquee { height: 1; background: black; color: white; padding: 0 1; dock: bottom; overflow: hidden; }
    #menu { height: 1fr; background: black; }
    #modalbox, #resultbox {
        width: 90; max-height: 90%;
        border: solid white; background: black; padding: 0 1;
    }
    #resultbox { width: 100; height: 80%; }
    #resultlog { height: 1fr; background: black; color: white; }
    ModalScreen { align: center middle; background: black; }
    #confirmbox {
        width: auto; max-width: 50; height: auto;
        border: solid white; background: black; padding: 0 1;
    }
    #modalbox Input { margin-bottom: 1; background: black; color: white; }
    #modalbox TextArea { height: 6; margin-bottom: 1; background: black; color: white; }
    #buttons { height: auto; align-horizontal: right; }
    Button { margin-left: 1; background: black; color: grey; border: solid grey; text-style: none; }
    Button.-active { background: white; color: black; }
    Button:hover { background: white; color: black; }
    Button:focus { background: white !important; color: black !important; border: solid white !important; text-style: bold reverse; }
    #sitelist { max-height: 15; background: black; }
    .siterow { height: auto; }
    .sitestatus { padding-left: 2; }
    DataTable { height: 1fr; border: none; background: black; color: white; }
    DataTable > .datatable--cursor { background: white; color: black; }
    DataTable:focus > .datatable--cursor { background: white; color: black; }
    DataTable > .datatable--header { background: black; color: white; text-style: bold; }
    #status { padding: 0 1; height: auto; background: black; color: white; }
    #settingsbox { padding: 0 1; background: black; color: white; }
    #settingsbox Input { margin-bottom: 1; background: black; color: white; }
    #settingsbox Checkbox { margin-bottom: 1; }
    #settings_status { height: auto; }
    *:focus { border: solid white; }
    Input { border: solid grey; background: black; color: white; }
    Input:focus { border: solid white; background: black; color: white; }
    TextArea { border: solid grey; background: black; color: white; }
    TextArea:focus { border: solid white; background: black; color: white; }
    Checkbox { border: none; background: black; color: white; }
    Checkbox:focus { border: none; color: white; text-style: bold; }
    Vertical { background: black; }
    Horizontal { background: black; }
    VerticalScroll { background: black; }
    Static { background: black; color: white; }
    Label { background: black; color: white; }
    Log { background: black; color: white; }
    """

    BINDINGS = [("q", "request_quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.log_lines: list[str] = []
        self._site_status: dict[str, bool | None] = {}
        self._site_rtime: dict[str, str] = {}

    def make_api(self) -> CbApi:
        return CbApi(self.cfg, logger=self.add_log)

    def add_log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {msg}")
        del self.log_lines[:-500]

    MENU_ITEMS = [
        ("Pre release", "pre"),
        ("Sites", "sites"),
        ("Sections", "sections"),
        ("Settings (API)", "settings"),
        ("API log", "log"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("pretool | Main Menu", id="topbar")
        with Vertical(id="menu"):
            yield DataTable(cursor_type="row", id="menutable")
        yield Static("q:Quit", id="bottombar")
        yield Static(" ", id="marquee")

    def action_request_quit(self) -> None:
        def done(yes: bool | None) -> None:
            if yes:
                self.exit()
        self.push_screen(ConfirmModal("Are you sure you want to quit?"), done)

    def on_mount(self) -> None:
        t = self.query_one("#menutable", DataTable)
        t.add_column("Option")
        for label, _key in self.MENU_ITEMS:
            t.add_row(label)
        for s in enabled_sites(self.cfg):
            self._site_status[s["name"]] = None
        self._update_status_bar()
        self._check_sites()
        self.set_interval(60, self._check_sites)

    @work(thread=True, exclusive=True)
    def _check_sites(self) -> None:
        api = self.make_api()
        for s in enabled_sites(self.cfg):
            name = s["name"]
            t0 = time.monotonic()
            try:
                res = api.raw("noop", [name])
                ms = (time.monotonic() - t0) * 1000
                text = next(iter(res.values()), "").lower()
                online = "fail" not in text and "error" not in text and "unable" not in text
            except Exception:
                online = False
                ms = 0
            self._site_status[name] = online
            if online:
                self._site_rtime[name] = f"{ms:.0f}ms"
            else:
                self._site_rtime.pop(name, None)
            self.call_from_thread(self._update_status_bar)

    def _update_status_bar(self) -> None:
        parts = []
        for name, status in self._site_status.items():
            if status is None:
                parts.append(name)
            elif status:
                rtime = self._site_rtime.get(name, "")
                if rtime:
                    parts.append(f"[green]{name} ({rtime})[/green]")
                else:
                    parts.append(f"[green]{name}[/green]")
            else:
                parts.append(f"[red]{name}[/red]")
        text = "  ".join(parts) if parts else " "
        try:
            self.screen.query_one("#marquee", Static).update(text)
        except Exception:
            pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "menutable":
            return
        row = event.cursor_row
        if row < 0 or row >= len(self.MENU_ITEMS):
            return
        key = self.MENU_ITEMS[row][1]
        screens = {
            "pre": PreScreen,
            "sites": SitesScreen,
            "sections": SectionsScreen,
            "settings": SettingsScreen,
            "log": LogScreen,
        }
        if key in screens:
            self.push_screen(screens[key]())


if __name__ == "__main__":
    import os
    PretoolApp().run(mouse=False)
    os.system("clear")
