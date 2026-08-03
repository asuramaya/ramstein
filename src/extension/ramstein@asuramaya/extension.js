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
import St from 'gi://St';

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

// FAMILY.md doctrine #12 ("subtitle re-skins to state") — audited
// 2026-08-02 as a gap NO pill in the family closes: all four render the
// collapsed tile's title/subtitle text in the theme's default color
// regardless of state. St.Widget's inline `style` property is stable,
// public GJS extension API (not an internal-class-name reach-around) and
// its `color` cascades down to the toggle's own title/subtitle labels, so
// setting it on the toggle itself recolors the whole tile's text on
// warn/hot without depending on GNOME Shell's private widget structure.
const STATE_TEXT_STYLE = {ok: '', warn: `color: ${PALETTE.WARN};`, hot: `color: ${PALETTE.BAD};`};

// FAMILY.md: "where a state is derived from OR'd conditions, the header
// must carry WHICH evidence fired" — classify()'s state_evidence names
// which of psi/avail/eta actually crossed the threshold; this is the
// header-facing label for each code. "warn" from stalling tasks (psi) and
// "warn" from a three-hour ETA are different situations, and previously
// the header showed the same bare mark for both (measured: "OOM ~11h"
// rendered directly above "pressure 0.0% · burn quiet", predicting death
// and reporting calm in the same card with nothing tying them together).
const EVIDENCE_LABEL = {psi: 'pressure', avail: 'low mem', eta: 'ETA'};

// swap-size's own preset strip, matching Werner's RESERVE_PRESETS shape
// (fixed chip values, not derived from device capacity) -- byte sizes
// rather than percentages, since that's what the verb actually takes.
const SWAP_SIZE_PRESETS = ['2G', '4G', '8G', '16G'];

function evidenceTag(mem) {
    const ev = mem.state_evidence;
    if (!Array.isArray(ev) || !ev.length)
        return '';
    return ev.map(e => EVIDENCE_LABEL[e] ?? e).join('+');
}

// ---- layer-3 controls: RAMstein's first consumption of the shared
// affordance vocabulary (alfred's dispatch, thread f10c0cd3). Werner's
// ByeByte shapes (reserve's SEGMENT chip-strip, declare's Reclaim
// pick-list) both complete in ONE round trip; none of the three verbs
// here do — swap-size/zram genuinely take real wall-clock time
// (fallocate+mkswap / the generator's own setup run). This is RAMstein's
// own answer to a shape Werner's code never needed, local until a
// second pill needs it (Tantra extracts once more than one local copy
// exists).
//
// THE MISFIT THAT MATTERS MOST, found building this rather than reading
// Werner's code: every PERSISTENT verb here (swappiness set, swap-size
// set, zram enable — and RAMstein's own pre-existing autocalm arm) gates
// on an INTERACTIVE TYPED CONFIRMATION at the CLI layer
// (`sys.stdin.isatty()` + `input("type '...' to confirm: ")`). A pill
// click spawns a Gio.Subprocess with no TTY at all — `isatty()` reads
// False every time, so the CLI refuses outright before the daemon ever
// sees the request, exactly like a script piping `--kill` is refused by
// design. ByeByte's reserve/declare never hit this because Werner's own
// runByebyteJson calls already pass `--yes` — a bypass flag those verbs
// were built with from the start. RAMstein's PERSISTENT verbs have no
// such flag; nothing below can actually fire through the CLI as written.
//
// Deliberately NOT worked around by talking to the control socket
// directly (Pill.sendCmd could reach do_swap_size_set/do_zram_enable
// today — neither has its own TTY check, only the CLI wrapper does) —
// that would make the pill the only caller with no consent gate at all,
// the opposite of RAMstein's own standing discipline that the pill and
// the CLI answer the SAME authority, not different ones. The click
// handlers below call `--yes`, a flag that does not exist yet — this is
// the shape a fix should take (matching ByeByte's own precedent), not a
// bug in this file. See the fit report (DM to alfred) for the actual
// recommendation: a real GNOME-native confirm step belongs on the PILL
// side of that flag, not a bare bypass — the CLI's typed-word gate stays
// the answer for terminal use, the pill needs its own.

let _ramsteinCliPath = null;
function ramsteinCli() {
    if (_ramsteinCliPath)
        return _ramsteinCliPath;
    for (const p of ['/usr/bin/ramstein', '/usr/local/bin/ramstein']) {
        if (GLib.file_test(p, GLib.FileTest.IS_EXECUTABLE)) {
            _ramsteinCliPath = p;
            return _ramsteinCliPath;
        }
    }
    _ramsteinCliPath = 'ramstein';
    return _ramsteinCliPath;
}

// Runs `ramstein <args>` and parses stdout as JSON -- ByeByte's
// runByebyteJson shape exactly (kast's readKastJson lineage before it):
// always calls onDone exactly once, with the parsed doc or null on any
// spawn/communicate/parse failure.
function runRamsteinJson(args, cancellable, onDone) {
    try {
        const proc = Gio.Subprocess.new(
            [ramsteinCli(), ...args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        proc.communicate_utf8_async(null, cancellable, (p, res) => {
            let parsed = null;
            try {
                const [, stdout] = p.communicate_utf8_finish(res);
                parsed = JSON.parse(stdout);
            } catch (e) {
                logError(e, 'ramstein: JSON parse failed');
            }
            onDone(parsed);
        });
    } catch (e) {
        logError(e, 'ramstein: subprocess failed');
        onDone(null);
    }
}

// BOUNDED-WAIT polling: swap-size/zram report `pending` immediately and
// need `status` polled for the real outcome (the CLI's own
// _swap_size_poll_until_done does the identical thing from the terminal
// side). Stops on isDone(), on a spawn/parse failure (never spins on
// nothing), or after timeoutSeconds — hands back the last-seen doc
// either way, never silently claims to have finished.
function pollUntilDone(args, cancellable, {isDone, onTick, onDone,
                                            intervalSeconds = 1, timeoutSeconds = 120}) {
    const tick = elapsed => {
        runRamsteinJson(args, cancellable, doc => {
            if (!doc) {
                onDone(null);
                return;
            }
            if (isDone(doc)) {
                onDone(doc);
                return;
            }
            onTick?.(doc, elapsed);
            const next = elapsed + intervalSeconds;
            if (next >= timeoutSeconds) {
                onDone(doc);
                return;
            }
            GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, intervalSeconds, () => {
                tick(next);
                return GLib.SOURCE_REMOVE;
            });
        });
    };
    tick(0);
}

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
        // layer-3: swap-size's BOUNDED-WAIT preset chips, oomd enroll's
        // and zram's TOGGLEs — persistent rows, always visible (a control
        // needs to be reachable whether or not the machine is currently
        // healthy, unlike advise/autocalm's conditional sections above).
        this._controlsSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._controlsSection);
        this._swapSizeRow = null;
        this._oomdToggleRow = null;
        this._zramToggleRow = null;

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
            this.style = '';
            this._alertSection.removeAll();
            this._rowSection.removeAll();
            this._adviseSection.removeAll();
            this._autocalmSection.removeAll();
            this._controlsSection.removeAll();
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
        // with its own countdown — a distinct, more urgent story; it
        // already names itself, so it carries no state_evidence tag.
        // Otherwise the tag says WHICH of classify()'s OR'd conditions
        // fired (empty at ok) — this branch is only reached when swapStorm
        // is null, so effState here always equals mem.state exactly.
        const tag = swapStorm ? '' : evidenceTag(mem);
        this.subtitle = swapStorm
            ? `⚠ swap storm · OOM ${fmtEta(swapStorm.eta_oom_seconds)}`
            : `${STATE_MARK[effState] ?? ''}${tag ? `${tag} · ` : ''}` +
              `${Pill.fmtBytes(mem.available)} · OOM ${fmtEta(mem.eta_oom_seconds)}`;
        // the heat: pill lights accent whenever the effective state is warn+
        this.checked = rank >= 1;
        this.iconName = STATE_ICON[effState] ?? ICON;
        // doctrine #12: the tile's own text re-skins to state, not just
        // the icon/accent — see STATE_TEXT_STYLE above.
        this.style = STATE_TEXT_STYLE[effState] ?? '';

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
        this._renderControls(pill);

        this.menu.setHeader(this.iconName, 'RAMstein', this.subtitle);
        this._update.setVersion(st.daemon?.version);
    }

    // ---- layer 3: swap-size (BOUNDED-WAIT button), oomd enroll and zram
    // (both TOGGLEs) — see the file-header note on the CLI's TTY gate:
    // the click handlers below are the shape a working control takes,
    // not yet a working control. Rebuilt each refresh, like every other
    // section here except Reclaim's own persistent picks (nothing here
    // holds in-progress user state the way that does).

    _renderControls(pill) {
        this._controlsSection.removeAll();
        if (!pill)
            return;
        this._controlsSection.addMenuItem(this._buildSwapSizeRow(pill.swap_size));
        this._controlsSection.addMenuItem(this._buildToggleRow({
            label: 'systemd-oomd swap protection',
            enabled: !!pill.oomd?.enrolled,
            pending: false,
            onToggle: () => this._onOomdToggle(pill.oomd),
        }));
        this._controlsSection.addMenuItem(this._buildToggleRow({
            label: 'zram (compressed swap)',
            enabled: !!pill.zram?.config_enabled,
            pending: !!pill.zram?.in_progress,
            disabled: !pill.zram?.generator_present,
            onToggle: () => this._onZramToggle(pill.zram),
        }));
    }

    // BOUNDED-WAIT BUTTON: a SEGMENT-shaped preset strip (Werner's own
    // reserve chip shape — St.Button row, CHIP/CHIP_ON), except a click
    // here can't render success synchronously the way his does: fallocate
    // +mkswap genuinely takes real wall-clock time, so `pending` replaces
    // the strip with a busy row instead of highlighting a chip. Neither
    // ByeByte verb needed this — reserve/declare both settle in one call.
    _buildSwapSizeRow(swapSize) {
        const box = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const layout = new St.BoxLayout({x_expand: true});
        const lab = new St.Label({text: 'swap file', style: `color:${DIM}; padding-right:8px;`});
        lab.y_align = 2;
        layout.add_child(lab);

        if (!swapSize || swapSize.in_progress) {
            layout.add_child(new St.Label({
                text: 'resizing…', x_expand: true, style: `color:${ACCENT};`,
            }));
        } else {
            const activeText = swapSize.active_bytes ? Pill.fmtBytes(swapSize.active_bytes) : null;
            for (const preset of SWAP_SIZE_PRESETS) {
                // string compare against fmtBytes' own rounding -- exact
                // only for the presets that round cleanly (4G, 8G, 16G);
                // 2G vs "2.0G" already needs unit-aware parsing Werner's
                // reserve strip never had to do (percentages compare as
                // plain integers). Real misfit, not a placeholder bug —
                // see the fit report.
                const isCurrent = activeText === preset;
                const btn = new St.Button({
                    label: preset, x_expand: true, can_focus: true,
                    style: isCurrent ? Pill.CHIP_ON : Pill.CHIP,
                });
                btn.connect('clicked', () => this._onSwapSizeClick(preset));
                layout.add_child(btn);
            }
        }
        box.add_child(layout);
        return box;
    }

    _onSwapSizeClick(size) {
        // --yes does not exist on this verb yet (file-header note) --
        // this call is the intended shape, not a working one.
        pollUntilDone(['swap-size', 'set', size, '--yes', '--json'], this._cancellable, {
            isDone: doc => !doc.pending,
            onDone: doc => {
                if (!doc) {
                    Pill.notify('RAMstein', 'swap-size set — daemon unreachable');
                    return;
                }
                if (doc.error) {
                    Pill.notify('RAMstein', `swap-size: ${doc.error}`);
                    return;
                }
                const lr = doc.last_result;
                Pill.notify('RAMstein', lr?.ok
                    ? `swap file → ${Pill.fmtBytes(lr.measured)} (confirmed live)`
                    : `swap-size set to ${size} did not take effect: ${lr?.error ?? 'unknown'}`);
            },
        });
    }

    // TOGGLE: PopupMenu.PopupSwitchMenuItem, GNOME's own stock widget --
    // no shared vocabulary primitive needed here, unlike submenuRow/
    // accordionRow. `pending` disables interaction and shows a busy
    // label instead of the switch, same reasoning as swap-size's row.
    // `disabled` (zram's generator-absent case) shows the actionable
    // refusal text in place of the switch entirely -- the pill surfacing
    // the same "apt install systemd-zram-generator" the CLI already
    // gives, not a dead-ended toggle.
    _buildToggleRow({label, enabled, pending, disabled, onToggle}) {
        if (disabled) {
            const it = new PopupMenu.PopupMenuItem('', {reactive: false});
            it.label.clutter_text.set_markup(
                `<span foreground="${DIM}">${Pill.esc(label)}: systemd-zram-generator` +
                ` not installed (apt install systemd-zram-generator)</span>`);
            return it;
        }
        const it = new PopupMenu.PopupSwitchMenuItem(label, enabled, {reactive: !pending});
        if (pending) {
            it.label.text = `${label} (working…)`;
        } else {
            it.connect('toggled', (_item, state) => onToggle(state));
        }
        return it;
    }

    _onOomdToggle(oomdStatus) {
        const action = oomdStatus?.enrolled ? 'disenroll' : 'enroll';
        // enroll's own pre-flight refusal MUST read as a redirect, not a
        // wall (FAMILY.md) -- the daemon already composes that message
        // ({"error": "refusing: ... see `ramstein oom` ..."}); the toggle
        // reverting to its prior state and surfacing that same text
        // unchanged IS the redirect, not a separate thing to build.
        runRamsteinJson(['oomd', action, '--yes', '--json'], this._cancellable, doc => {
            if (!doc || doc.error) {
                Pill.notify('RAMstein', doc?.error || `oomd ${action} — daemon unreachable`);
                this.refresh();   // toggle reverts: re-render from real state
                return;
            }
            Pill.notify('RAMstein',
                `systemd-oomd swap protection ${action === 'enroll' ? 'enrolled' : 'removed'}` +
                ' (confirmed live).');
        });
    }

    _onZramToggle(zramStatus) {
        const action = zramStatus?.config_enabled ? 'disable' : 'enable';
        pollUntilDone(['zram', action, '--yes', '--json'], this._cancellable, {
            isDone: doc => !doc.pending,
            onDone: doc => {
                if (!doc) {
                    Pill.notify('RAMstein', `zram ${action} — daemon unreachable`);
                    return;
                }
                if (doc.error) {
                    Pill.notify('RAMstein', `zram: ${doc.error}`);
                    return;
                }
                const lr = doc.last_result;
                Pill.notify('RAMstein', lr?.ok
                    ? `zram ${action}d (confirmed live).`
                    : `zram ${action} did not take effect: ${lr?.error ?? 'unknown'}`);
            },
        });
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
