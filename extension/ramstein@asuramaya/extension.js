// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 asuramaya and RAMstein contributors
//
// RAMstein — memory as a deadline, not a percentage, in a GNOME Quick
// Settings pill. Read-only by design: one file (status.json), one
// GFileMonitor, no daemon-protocol client in GJS. M2/M3 (top process,
// zombie count, an advise headline) ride along as a small digest the
// daemon computes into status.json — calm/kill stay CLI-only, on purpose;
// a system-tray toggle is the wrong place for a kill confirmation.

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const STATUS_PATH = '/run/ramstein/status.json';

const ICON = 'utilities-system-monitor-symbolic';

// concept palette (family)
const ACCENT = '#b9acff';
const DIM = '#9aa0a6';
const GOOD = '#4caf50';
const WARN = '#ffbb33';
const BAD = '#ff5b5b';

const STATE_COLOR = {ok: GOOD, warn: WARN, hot: BAD};
const STATE_MARK = {ok: '', warn: '⚠ ', hot: '‼ '};

function isObj(v) {
    return v && typeof v === 'object' && !Array.isArray(v);
}
function num(v) {
    return (typeof v === 'number' && isFinite(v)) ? v : null;
}
function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
}
function fmtBytes(n) {
    if (n == null)
        return '?';
    const units = ['B', 'K', 'M', 'G', 'T'];
    let i = 0;
    while (Math.abs(n) >= 1024 && i < units.length - 1) {
        n /= 1024;
        i++;
    }
    return i === 0 ? `${Math.round(n)}B` : `${n.toFixed(1)}${units[i]}`;
}
function fmtBurn(bps) {
    // per-second is meaningful for memory — a leak eats MB/s, not GB/day
    if (bps == null || Math.abs(bps) < 1024 * 1024)
        return 'quiet';
    return `${fmtBytes(bps)}/s`;
}
function fmtEta(s) {
    // OOM horizons are minutes and hours, not days and weeks
    if (s == null)
        return '—';
    if (s >= 2 * 3600)
        return `~${Math.floor(s / 3600)}h`;
    if (s >= 120)
        return `~${Math.floor(s / 60)}m`;
    return `~${Math.max(1, Math.floor(s))}s`;
}
// severity order for the heat and the alert banner
const RANK = {ok: 0, warn: 1, hot: 2};

function readStatus() {
    try {
        const [ok, bytes] = GLib.file_get_contents(STATUS_PATH);
        if (!ok)
            return null;
        const o = JSON.parse(new TextDecoder().decode(bytes));
        return isObj(o) && isObj(o.memory) ? o : null;
    } catch (_e) {
        return null;
    }
}

const RAMsteinToggle = GObject.registerClass(
class RAMsteinToggle extends QuickMenuToggle {
    _init() {
        super._init({title: 'RAMstein', iconName: ICON, toggleMode: false});
        this.menu.setHeader(ICON, 'RAMstein', 'bytes alive');

        // alert banner — hidden until the memory state is warn/hot
        this._alertSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._alertSection);

        // memory / swap / top process / zombies / pressure / burn rows,
        // rebuilt on refresh
        this._rowSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._rowSection);

        // advise headline — hidden unless the daemon's advise rules have
        // something to say (M2/M3 digest, see bin/ramsteind's "pill" field)
        this._adviseSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._adviseSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._versionItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this.menu.addMenuItem(this._versionItem);

        // M1 has nothing to toggle; a click is a free instant refresh
        this.connect('clicked', () => this.refresh());
    }

    refresh() {
        const st = readStatus();
        const stale = st && (GLib.get_real_time() / 1e6 - st.ts) >
            3 * (num(st.daemon?.poll_interval) ?? 10) + 5;
        if (!st || stale) {
            this.subtitle = stale ? 'status stale' : 'daemon offline';
            this.checked = false;
            this._alertSection.removeAll();
            this._rowSection.removeAll();
            this._adviseSection.removeAll();
            const it = new PopupMenu.PopupMenuItem(
                stale ? 'ramsteind stopped updating' : 'ramsteind not running',
                {reactive: false});
            this._rowSection.addMenuItem(it);
            this._setVersion(null);
            return;
        }
        this._apply(st);
    }

    _apply(st) {
        const mem = st.memory;
        const pill = st.pill ?? null;
        const state = mem.state ?? 'ok';

        // tile: the hero readout — how much is left, how long until the
        // kernel starts shooting
        this.subtitle = `${STATE_MARK[state] ?? ''}` +
            `${fmtBytes(mem.available)} · OOM ${fmtEta(mem.eta_oom_seconds)}`;
        // the heat: pill lights accent whenever the state is warn or worse
        this.checked = (RANK[state] ?? 0) >= 1;

        // alert banner: warn/hot gets its own loud line — the why is the
        // thresholds the daemon classifies on: PSI full, available, ETA
        this._alertSection.removeAll();
        if ((RANK[state] ?? 0) >= 1) {
            const it = new PopupMenu.PopupMenuItem('', {reactive: false});
            const full10 = num(mem.psi?.full_avg10);
            const bits = [];
            if (mem.eta_oom_seconds != null)
                bits.push(`OOM ${fmtEta(mem.eta_oom_seconds)}`);
            if (full10 != null)
                bits.push(`psi full ${full10.toFixed(1)}%`);
            bits.push(`${fmtBytes(mem.available)} left`);
            it.label.clutter_text.set_markup(
                `<span foreground="${STATE_COLOR[state]}">` +
                `${STATE_MARK[state]}memory — ${esc(bits.join(' · '))}</span>`);
            this._alertSection.addMenuItem(it);
        }

        // rows: memory, swap, pressure, burn
        this._rowSection.removeAll();
        const color = STATE_COLOR[state] ?? DIM;

        const memIt = new PopupMenu.PopupMenuItem('', {reactive: false});
        memIt.label.clutter_text.set_markup(
            `<span foreground="${color}" font_weight="bold">●</span> ` +
            `memory  ` +
            `<span foreground="${ACCENT}">${fmtBytes(mem.available)}</span>` +
            `<span foreground="${DIM}"> of ${fmtBytes(mem.total)}</span>`);
        this._rowSection.addMenuItem(memIt);

        const swapIt = new PopupMenu.PopupMenuItem('', {reactive: false});
        // "X free of Y", matching the CLI — not "X of Y free", which reads
        // like X is USED (the classic "3 of 10" idiom) when X is what's
        // LEFT. Backwards at the worst possible moment: misread that way
        // right when swap is nearly full, it says the opposite of true.
        const swap = (num(mem.swap_total) ?? 0) > 0
            ? `<span foreground="${ACCENT}">${fmtBytes(mem.swap_free)}</span>` +
              `<span foreground="${DIM}"> free of ${fmtBytes(mem.swap_total)}</span>`
            : `<span foreground="${DIM}">none</span>`;
        swapIt.label.clutter_text.set_markup(
            `<span foreground="${DIM}" font_weight="bold">●</span> swap  ${swap}`);
        this._rowSection.addMenuItem(swapIt);

        // top process + zombie count come from the M2/M3 digest the daemon
        // computes on the sampler's own cadence — null until the first
        // sample lands (daemon just (re)started), so both are optional
        if (pill?.top_process) {
            const tp = pill.top_process;
            const topIt = new PopupMenu.PopupMenuItem('', {reactive: false});
            topIt.label.clutter_text.set_markup(
                `<span foreground="${DIM}" font_weight="bold">●</span> top   ` +
                `<span foreground="${ACCENT}">${fmtBytes(tp.rss)}</span>` +
                `<span foreground="${DIM}"> ${esc(tp.comm)} (pid ${tp.pid})</span>`);
            this._rowSection.addMenuItem(topIt);
        }

        if (pill?.zombie_count > 0) {
            const zIt = new PopupMenu.PopupMenuItem('', {reactive: false});
            zIt.label.clutter_text.set_markup(
                `<span foreground="${WARN}" font_weight="bold">●</span> zombies  ` +
                `<span foreground="${WARN}">${pill.zombie_count}</span>` +
                `<span foreground="${DIM}"> unreaped</span>`);
            this._rowSection.addMenuItem(zIt);
        }

        const psiIt = new PopupMenu.PopupMenuItem('', {reactive: false});
        const some10 = num(mem.psi?.some_avg10);
        const full10 = num(mem.psi?.full_avg10);
        psiIt.label.clutter_text.set_markup(
            `<span foreground="${DIM}">pressure  ` +
            `some ${some10 == null ? '?' : some10.toFixed(1)}% · ` +
            `full ${full10 == null ? '?' : full10.toFixed(1)}% (avg10)</span>`);
        this._rowSection.addMenuItem(psiIt);

        const burnIt = new PopupMenu.PopupMenuItem('', {reactive: false});
        burnIt.label.clutter_text.set_markup(
            `<span foreground="${DIM}">burn  ${esc(fmtBurn(mem.burn_bps))}</span>`);
        this._rowSection.addMenuItem(burnIt);

        // advise headline: the single most-urgent nudge, with a "+N more"
        // count when there's more than one — full detail stays a CLI-only
        // thing (`ramstein advise`), the pill just says something's worth a
        // look
        this._adviseSection.removeAll();
        if (pill?.advise_headline) {
            const extra = pill.advise_count > 1
                ? ` (+${pill.advise_count - 1} more)` : '';
            const advIt = new PopupMenu.PopupMenuItem('', {reactive: false});
            advIt.label.clutter_text.set_markup(
                `<span foreground="${WARN}">▸ ${esc(pill.advise_headline + extra)}</span>`);
            this._adviseSection.addMenuItem(advIt);
        }

        this.menu.setHeader(ICON, 'RAMstein', this.subtitle);
        this._setVersion(st.daemon?.version);
    }

    _setVersion(ver) {
        this._versionItem.label.clutter_text.set_markup(
            `<span foreground="${DIM}">ramstein ${ver ? `v${esc(ver)}` : '(daemon offline)'}</span>`);
    }
});

const RAMsteinIndicator = GObject.registerClass(
class RAMsteinIndicator extends SystemIndicator {
    _init() {
        super._init();
        this.toggle = new RAMsteinToggle();
        this.quickSettingsItems.push(this.toggle);
    }
});

export default class RAMsteinExtension extends Extension {
    enable() {
        this._indicator = new RAMsteinIndicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        this._indicator.toggle.refresh();

        // event-driven: the daemon writes status.json with an atomic rename,
        // which lands here as exactly one CREATED/CHANGES_DONE event per poll
        this._file = Gio.File.new_for_path(STATUS_PATH);
        this._monitor = this._file.monitor_file(Gio.FileMonitorFlags.NONE, null);
        this._monitorId = this._monitor.connect('changed', (_m, _f, _of, ev) => {
            if (ev === Gio.FileMonitorEvent.CHANGES_DONE_HINT ||
                ev === Gio.FileMonitorEvent.CREATED ||
                ev === Gio.FileMonitorEvent.RENAMED)
                this._indicator.toggle.refresh();
        });
        // slow fallback tick: catches daemon death (no events, status goes
        // stale) and monitor misses across /run recreation on reboot
        this._timeout = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 60, () => {
            this._indicator.toggle.refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._timeout) {
            GLib.source_remove(this._timeout);
            this._timeout = null;
        }
        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
            this._monitorId = null;
        }
        this._file = null;
        this._indicator?.quickSettingsItems.forEach(i => i.destroy());
        this._indicator?.destroy();
        this._indicator = null;
    }
}
