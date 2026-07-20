// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 asuramaya and RAMstein contributors
//
// RAMstein — memory as a deadline, not a percentage, in a GNOME Quick
// Settings pill. Read-only by design: one file (status.json), one
// GFileMonitor, no daemon-protocol client in GJS. M2/M3 (top process,
// zombie count, an advise headline) ride along as a small digest the
// daemon computes into status.json — calm/kill stay CLI-only, on purpose;
// a system-tray toggle is the wrong place for a kill confirmation.
// Row/icon conventions follow phanspeed and kast — the family's own
// golden examples for what a quick-settings pill should look like.

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

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
// the toggle/header icon changes shape, not just color, on real trouble —
// phanspeed's emergency-state icon swap, so a glance at the top bar alone
// (no color perception needed) tells warn from hot
const STATE_ICON = {ok: ICON, warn: 'dialog-warning-symbolic', hot: 'dialog-error-symbolic'};

// NBSP: glues a label to its figure ("OOM ~2h") so a wrap can only land on
// a real separator (' · '), never mid-phrase — see wrapRow/iconRow below
const NB = ' ';

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

function row(markup) {
    const it = new PopupMenu.PopupMenuItem('', {reactive: false});
    it.label.clutter_text.set_markup(markup);
    return it;
}

// content that can outgrow the popup's fixed width (the alert banner, the
// advise headline) wraps to a second line instead of clipping mid-word —
// PopupMenuItem labels don't wrap by default, which is the bug this fixes
function wrapRow(markup) {
    const it = row(markup);
    it.label.clutter_text.set_line_wrap(true);
    it.label.clutter_text.set_ellipsize(Pango.EllipsizeMode.NONE);
    return it;
}

// icon-led stat row — phanspeed/kast's usual shape, built the same way they
// build theirs (a reactive-false PopupBaseMenuItem wrapping an St.BoxLayout)
function iconRow(iconName, markup) {
    const it = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
    const box = new St.BoxLayout({x_expand: true});
    box.add_child(new St.Icon({icon_name: iconName, style_class: 'popup-menu-icon'}));
    const label = new St.Label({x_expand: true, style: 'margin-left: 8px;'});
    label.clutter_text.set_markup(markup);
    label.clutter_text.set_line_wrap(true);
    label.clutter_text.set_ellipsize(Pango.EllipsizeMode.NONE);
    box.add_child(label);
    it.add_child(box);
    return it;
}

const RAMsteinToggle = GObject.registerClass(
class RAMsteinToggle extends QuickMenuToggle {
    _init() {
        super._init({title: 'RAMstein', iconName: ICON, toggleMode: false});
        this.menu.setHeader(ICON, 'RAMstein', 'bytes alive');

        // alert banner — hidden until the memory state is warn/hot
        this._alertSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._alertSection);

        // memory (hero) / swap / top process / zombies / pressure / burn
        // rows, rebuilt on refresh
        this._rowSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._rowSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // advise headline — hidden unless the daemon's advise rules have
        // something to say (M2/M3 digest, see bin/ramsteind's "pill" field)
        this._adviseSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._adviseSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._versionItem = row('');
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
            this.iconName = ICON;
            this._alertSection.removeAll();
            this._rowSection.removeAll();
            this._adviseSection.removeAll();
            this._rowSection.addMenuItem(row(
                `<span foreground="${DIM}">` +
                `${stale ? 'ramsteind stopped updating' : 'ramsteind not running'}</span>`));
            this._setVersion(null);
            this.menu.setHeader(ICON, 'RAMstein', this.subtitle);
            return;
        }
        this._apply(st);
    }

    _apply(st) {
        const mem = st.memory;
        const pill = st.pill ?? null;
        const state = mem.state ?? 'ok';
        const some10 = num(mem.psi?.some_avg10);
        const full10 = num(mem.psi?.full_avg10);

        // V2.M1: swap-storm early warning — a distinct, additional signal
        // from the daemon's own classifier (avail%/PSI/eta can stay "ok"
        // for a while even as swap visibly drains). Presence alone bumps
        // the pill's effective severity to at least WARN, on top of
        // whatever `state` already says — never downgrades from hot.
        const swapStorm = isObj(st.warning) && st.warning.kind === 'swap_storm'
            ? st.warning : null;
        const baseRank = RANK[state] ?? 0;
        const rank = Math.max(baseRank, swapStorm ? 1 : 0);
        const effState = rank >= 2 ? 'hot' : rank >= 1 ? 'warn' : 'ok';
        const color = STATE_COLOR[effState] ?? DIM;

        // tile: the hero readout — how much is left, how long until the
        // kernel starts shooting. Swap storm pre-empts the usual subtitle
        // with its own countdown — a distinct, more urgent story.
        this.subtitle = swapStorm
            ? `⚠ swap storm · OOM ${fmtEta(swapStorm.eta_oom_seconds)}`
            : `${STATE_MARK[effState] ?? ''}` +
              `${fmtBytes(mem.available)} · OOM ${fmtEta(mem.eta_oom_seconds)}`;
        // the heat: pill lights accent whenever the effective state is warn+
        this.checked = rank >= 1;
        this.iconName = STATE_ICON[effState] ?? ICON;

        // alert banner: warn/hot gets its own loud line — the why is the
        // thresholds the daemon classifies on: PSI full, available, ETA.
        // NBSP-glued so a wrap (the popup is a fixed ~280-300px) can only
        // land on a ' · ' join, never split a figure like "OOM ~2h" in two.
        // Gated on the daemon's own `state` (not the swap-storm-bumped
        // rank) since its content is specifically about that classifier.
        this._alertSection.removeAll();
        if (baseRank >= 1) {
            const bits = [];
            if (mem.eta_oom_seconds != null)
                bits.push(`OOM${NB}${fmtEta(mem.eta_oom_seconds)}`);
            if (full10 != null)
                bits.push(`psi${NB}full${NB}${full10.toFixed(1)}%`);
            bits.push(`${fmtBytes(mem.available)}${NB}left`);
            this._alertSection.addMenuItem(wrapRow(
                `<span foreground="${STATE_COLOR[state] ?? DIM}">` +
                `${STATE_MARK[state]}memory — ${esc(bits.join(' · '))}</span>`));
        }

        // swap-storm banner: its own line, independent of the section
        // above — names the top-3 growers so the countdown comes with a
        // "who" attached, not just a number
        if (swapStorm) {
            const bits = [`OOM${NB}${fmtEta(swapStorm.eta_oom_seconds)}`];
            if (swapStorm.swap_burn_bps != null)
                bits.push(`swap${NB}burn${NB}${esc(fmtBurn(swapStorm.swap_burn_bps))}`);
            const growers = (swapStorm.top_growers || [])
                .map(g => `${esc(g.comm)}${NB}+${fmtBytes(g.swap_delta)}`)
                .join(', ');
            if (growers)
                bits.push(`top:${NB}${growers}`);
            this._alertSection.addMenuItem(wrapRow(
                `<span foreground="${WARN}">⚠ swap storm — ${bits.join(' · ')}</span>`));
        }

        // rows: memory (hero, bold+large) / swap / top process / zombies,
        // then a separator before the quieter pressure+burn technical line.
        // Six same-weight stacked rows read as noise; one clear headline
        // plus a couple of context rows and a dimmed technical footnote
        // reads as a pill.
        this._rowSection.removeAll();

        this._rowSection.addMenuItem(iconRow(ICON,
            `<span foreground="${color}" font_weight="bold" size="large">` +
            `${fmtBytes(mem.available)}</span>` +
            `<span foreground="${DIM}"> available of ${fmtBytes(mem.total)}</span>`));

        // "X free of Y", matching the CLI — not "X of Y free", which reads
        // like X is USED (the classic "3 of 10" idiom) when X is what's
        // LEFT. Backwards at the worst possible moment: misread that way
        // right when swap is nearly full, it says the opposite of true.
        const swap = (num(mem.swap_total) ?? 0) > 0
            ? `<span foreground="${ACCENT}">${fmtBytes(mem.swap_free)}</span>` +
              `<span foreground="${DIM}"> free of ${fmtBytes(mem.swap_total)}</span>`
            : `<span foreground="${DIM}">none</span>`;
        this._rowSection.addMenuItem(iconRow('drive-harddisk-symbolic', swap));

        // top process + zombie count come from the M2/M3 digest the daemon
        // computes on the sampler's own cadence — null until the first
        // sample lands (daemon just (re)started), so both are optional
        if (pill?.top_process) {
            const tp = pill.top_process;
            this._rowSection.addMenuItem(iconRow('system-run-symbolic',
                `<span foreground="${ACCENT}">${fmtBytes(tp.rss)}</span>` +
                `<span foreground="${DIM}"> ${esc(tp.comm)} (pid ${tp.pid})</span>`));
        }

        if (pill?.zombie_count > 0) {
            const n = pill.zombie_count;
            this._rowSection.addMenuItem(iconRow('process-stop-symbolic',
                `<span foreground="${WARN}">${n} unreaped zombie${n === 1 ? '' : 's'}</span>`));
        }

        this._rowSection.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._rowSection.addMenuItem(row(
            `<span foreground="${DIM}">pressure${NB}` +
            `${some10 == null ? '?' : some10.toFixed(1)}%${NB}/${NB}` +
            `${full10 == null ? '?' : full10.toFixed(1)}%${NB}(avg10)` +
            `${NB}·${NB}burn${NB}${esc(fmtBurn(mem.burn_bps))}</span>`));

        // advise headline: the single most-urgent nudge, with a "+N more"
        // count when there's more than one — full detail stays a CLI-only
        // thing (`ramstein advise`), the pill just says something's worth a
        // look
        this._adviseSection.removeAll();
        if (pill?.advise_headline) {
            const extra = pill.advise_count > 1
                ? ` (+${pill.advise_count - 1} more)` : '';
            this._adviseSection.addMenuItem(iconRow('emblem-important-symbolic',
                `<span foreground="${WARN}">${esc(pill.advise_headline + extra)}</span>`));
        }

        this.menu.setHeader(this.iconName, 'RAMstein', this.subtitle);
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
