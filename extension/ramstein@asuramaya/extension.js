// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 asuramaya and RAMstein contributors
//
// RAMstein — memory as a deadline, not a percentage, in a GNOME Quick
// Settings pill. Read-only by design: one file (status.json), one
// GFileMonitor, no daemon-protocol client in GJS. M2/M3 (top process,
// zombie count, an advise headline) ride along as a small digest the
// daemon computes into status.json — calm/kill stay CLI-only, on purpose;
// a system-tray toggle is the wrong place for a kill confirmation.
//
// Wave B: adopts pill.js, the family's vendored extension commons (palette,
// formatters, row helpers, the status watcher, the update-surface UI, the
// Quick Settings indicator boilerplate) — everything here is RAMstein's own
// domain judgement (severity ranking, hero/alert/advise/autocalm content,
// swap-storm re-skinning, memory-specific ETA/burn formatting).

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import * as Pill from './pill.js';

const STATUS_PATH = '/run/ramstein/status.json';
const {PALETTE, NB} = Pill;
const {ACCENT, DIM, WARN} = PALETTE;

const ICON = 'utilities-system-monitor-symbolic';

const STATE_COLOR = {ok: PALETTE.GOOD, warn: PALETTE.WARN, hot: PALETTE.BAD};
const STATE_MARK = {ok: '', warn: '⚠ ', hot: '‼ '};
// the toggle/header icon changes shape, not just color, on real trouble —
// phanspeed's emergency-state icon swap, so a glance at the top bar alone
// (no color perception needed) tells warn from hot
const STATE_ICON = {ok: ICON, warn: 'dialog-warning-symbolic', hot: 'dialog-error-symbolic'};

function fmtBurn(bps) {
    // per-second is meaningful for memory — a leak eats MB/s, not GB/day
    if (bps == null || Math.abs(bps) < 1024 * 1024)
        return 'quiet';
    return `${Pill.fmtBytes(bps)}/s`;
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
    return Pill.readStatusFile(STATUS_PATH, o => Pill.isObj(o.memory));
}

// re-check cadence for the pill's own "update available" row — independent
// of ramstein-update.timer (which only notifies/logs, never paints the UI)
const UPDATE_CHECK_SECONDS = 6 * 3600;

const RAMsteinToggle = GObject.registerClass(
class RAMsteinToggle extends QuickMenuToggle {
    _init(cancellable) {
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

        // V2.M2: auto-calm's last cycle — hidden until at least one cycle
        // has ever triggered (dry-run or real). Notifications are a
        // separate, one-shot thing (_maybeNotifyAutocalm) — this row is the
        // persistent "last calm line" the spec calls for.
        this._autocalmSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._autocalmSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._update = new Pill.UpdateSurface('ramstein', {cancellable});
        this.menu.addMenuItem(this._update.updateItem);
        this.menu.addMenuItem(this._update.versionItem);

        // tracks the last autocalm cycle we've already notified about, so a
        // GFileMonitor refresh (every poll) doesn't re-notify for the same
        // one — see _maybeNotifyAutocalm
        this._lastAutocalmTs = null;

        // M1 has nothing to toggle; a click is a free instant refresh
        this.connect('clicked', () => this.refresh());
    }

    refresh() {
        const st = readStatus();
        const stale = Pill.isStale(st, 10);
        if (!st || stale) {
            this.subtitle = stale ? 'status stale' : 'daemon offline';
            this.checked = false;
            this.iconName = ICON;
            this._alertSection.removeAll();
            this._rowSection.removeAll();
            this._adviseSection.removeAll();
            this._autocalmSection.removeAll();
            this._rowSection.addMenuItem(Pill.row(
                `<span foreground="${DIM}">` +
                `${stale ? 'ramsteind stopped updating' : 'ramsteind not running'}</span>`));
            this._update.setVersion(null);
            this.menu.setHeader(ICON, 'RAMstein', this.subtitle);
            return;
        }
        this._apply(st);
    }

    _apply(st) {
        const mem = st.memory;
        const pill = st.pill ?? null;
        const state = mem.state ?? 'ok';
        const some10 = Pill.num(mem.psi?.some_avg10);
        const full10 = Pill.num(mem.psi?.full_avg10);

        // V2.M1: swap-storm early warning — a distinct, additional signal
        // from the daemon's own classifier (avail%/PSI/eta can stay "ok"
        // for a while even as swap visibly drains). Presence alone bumps
        // the pill's effective severity to at least WARN, on top of
        // whatever `state` already says — never downgrades from hot.
        const swapStorm = Pill.isObj(st.warning) && st.warning.kind === 'swap_storm'
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
              `${Pill.fmtBytes(mem.available)} · OOM ${fmtEta(mem.eta_oom_seconds)}`;
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
            bits.push(`${Pill.fmtBytes(mem.available)}${NB}left`);
            this._alertSection.addMenuItem(Pill.wrapRow(
                `<span foreground="${STATE_COLOR[state] ?? DIM}">` +
                `${STATE_MARK[state]}memory — ${Pill.esc(bits.join(' · '))}</span>`));
        }

        // swap-storm banner: its own line, independent of the section
        // above — names the top-3 growers so the countdown comes with a
        // "who" attached, not just a number
        if (swapStorm) {
            const bits = [`OOM${NB}${fmtEta(swapStorm.eta_oom_seconds)}`];
            if (swapStorm.swap_burn_bps != null)
                bits.push(`swap${NB}burn${NB}${Pill.esc(fmtBurn(swapStorm.swap_burn_bps))}`);
            const growers = (swapStorm.top_growers || [])
                .map(g => `${Pill.esc(g.comm)}${NB}+${Pill.fmtBytes(g.swap_delta)}`)
                .join(', ');
            if (growers)
                bits.push(`top:${NB}${growers}`);
            this._alertSection.addMenuItem(Pill.wrapRow(
                `<span foreground="${WARN}">⚠ swap storm — ${bits.join(' · ')}</span>`));
        }

        // rows: memory (hero, bold+large) / swap / top process / zombies,
        // then a separator before the quieter pressure+burn technical line.
        // Six same-weight stacked rows read as noise; one clear headline
        // plus a couple of context rows and a dimmed technical footnote
        // reads as a pill.
        this._rowSection.removeAll();

        this._rowSection.addMenuItem(Pill.iconRow(ICON,
            `<span foreground="${color}" font_weight="bold" size="large">` +
            `${Pill.fmtBytes(mem.available)}</span>` +
            `<span foreground="${DIM}"> available of ${Pill.fmtBytes(mem.total)}</span>`));

        // "X free of Y", matching the CLI — not "X of Y free", which reads
        // like X is USED (the classic "3 of 10" idiom) when X is what's
        // LEFT. Backwards at the worst possible moment: misread that way
        // right when swap is nearly full, it says the opposite of true.
        const swap = (Pill.num(mem.swap_total) ?? 0) > 0
            ? `<span foreground="${ACCENT}">${Pill.fmtBytes(mem.swap_free)}</span>` +
              `<span foreground="${DIM}"> free of ${Pill.fmtBytes(mem.swap_total)}</span>`
            : `<span foreground="${DIM}">none</span>`;
        this._rowSection.addMenuItem(Pill.iconRow('drive-harddisk-symbolic', swap));

        // top process + zombie count come from the M2/M3 digest the daemon
        // computes on the sampler's own cadence — null until the first
        // sample lands (daemon just (re)started), so both are optional
        if (pill?.top_process) {
            const tp = pill.top_process;
            this._rowSection.addMenuItem(Pill.iconRow('system-run-symbolic',
                `<span foreground="${ACCENT}">${Pill.fmtBytes(tp.rss)}</span>` +
                `<span foreground="${DIM}"> ${Pill.esc(tp.comm)} (pid ${tp.pid})</span>`));
        }

        if (pill?.zombie_count > 0) {
            const n = pill.zombie_count;
            this._rowSection.addMenuItem(Pill.iconRow('process-stop-symbolic',
                `<span foreground="${WARN}">${n} unreaped zombie${n === 1 ? '' : 's'}</span>`));
        }

        this._rowSection.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._rowSection.addMenuItem(Pill.row(
            `<span foreground="${DIM}">pressure${NB}` +
            `${some10 == null ? '?' : some10.toFixed(1)}%${NB}/${NB}` +
            `${full10 == null ? '?' : full10.toFixed(1)}%${NB}(avg10)` +
            `${NB}·${NB}burn${NB}${Pill.esc(fmtBurn(mem.burn_bps))}</span>`));

        // advise headline: the single most-urgent nudge, with a "+N more"
        // count when there's more than one — full detail stays a CLI-only
        // thing (`ramstein advise`), the pill just says something's worth a
        // look
        this._adviseSection.removeAll();
        if (pill?.advise_headline) {
            const extra = pill.advise_count > 1
                ? ` (+${pill.advise_count - 1} more)` : '';
            this._adviseSection.addMenuItem(Pill.iconRow('emblem-important-symbolic',
                `<span foreground="${WARN}">${Pill.esc(pill.advise_headline + extra)}</span>`));
        }

        // V2.M2: auto-calm's last cycle — the "last calm line" the spec
        // calls for. armed+acted gets the accent (it really did something);
        // dry-run gets DIM (it only computed what it would have done).
        this._autocalmSection.removeAll();
        const ac = st.autocalm;
        const lastResult = ac?.last_result;
        if (lastResult?.trigger) {
            const t = lastResult.target ?? {};
            const steps = (lastResult.steps ?? [])
                .map(s => s.step).join('+') || 'no steps enabled';
            const verb = lastResult.acted ? 'acted' : 'dry-run';
            const vcolor = lastResult.acted ? ACCENT : DIM;
            const age = ac.last_action_ts != null
                ? fmtEta(Math.max(0, st.ts - ac.last_action_ts)) : '—';
            this._autocalmSection.addMenuItem(Pill.iconRow('preferences-system-symbolic',
                `<span foreground="${vcolor}">auto-calm ${verb}</span>` +
                `<span foreground="${DIM}">${NB}${Pill.esc(steps)} on` +
                `${NB}${Pill.esc(t.comm ?? '?')}${NB}(pid${NB}${t.pid ?? '?'})` +
                `${NB}·${NB}${age}${NB}ago</span>`));
        }
        this._maybeNotifyAutocalm(st);

        this.menu.setHeader(this.iconName, 'RAMstein', this.subtitle);
        this._update.setVersion(st.daemon?.version);
    }

    // A root systemd daemon has no clean path into the operator's own
    // desktop session, so the daemon only SURFACES the payload
    // (status.json's "autocalm" field) — the pill, already running in the
    // right session with real notification access, does the actual
    // Pill.notify() (Main.notify() underneath) call. Fires once per cycle
    // (tracked by last_action_ts), never re-fires for a result already
    // seen on an earlier poll.
    _maybeNotifyAutocalm(st) {
        const ac = st.autocalm;
        const r = ac?.last_result;
        if (!r?.trigger || ac.last_action_ts == null)
            return;
        if (this._lastAutocalmTs === ac.last_action_ts)
            return;
        this._lastAutocalmTs = ac.last_action_ts;
        const t = r.target ?? {};
        const verb = r.acted ? 'acted' : 'would act (dry-run)';
        const steps = (r.steps ?? []).map(s => s.step).join(', ') || 'nothing';
        const kill = r.notify?.suggested_kill;
        Pill.notify('RAMstein — auto-calm',
            `${verb} on ${t.comm ?? '?'} (pid ${t.pid ?? '?'}) — ${r.trigger}.` +
            ` Steps: ${steps}.` + (kill ? ` If needed: ${kill}` : ''));
    }

    checkForUpdate() {
        this._update.checkNow();
    }
});

export default class RAMsteinExtension extends Extension {
    enable() {
        this._cancellable = new Gio.Cancellable();
        this._toggle = new RAMsteinToggle(this._cancellable);
        this._indicator = Pill.addQuickSettingsToggle(this._toggle);
        this._toggle.refresh();
        this._toggle.checkForUpdate();

        // event-driven: the daemon writes status.json with an atomic rename,
        // which lands here as exactly one CREATED/CHANGES_DONE event per
        // poll, plus a slow fallback tick that catches daemon death (no
        // events, status goes stale) and monitor misses across /run
        // recreation on reboot
        this._watcher = new Pill.StatusWatcher(
            STATUS_PATH, () => this._toggle.refresh(), {fallbackSeconds: 60});
        this._updateTimeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, UPDATE_CHECK_SECONDS, () => {
                this._toggle.checkForUpdate();
                return GLib.SOURCE_CONTINUE;
            });
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
        if (this._updateTimeout) {
            GLib.source_remove(this._updateTimeout);
            this._updateTimeout = null;
        }
        this._watcher?.destroy();
        this._watcher = null;
        Pill.removeIndicator(this._indicator);
        this._indicator = null;
        this._toggle = null;
    }
}
