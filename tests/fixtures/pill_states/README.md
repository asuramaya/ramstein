# Pill state fixtures

Ready-made `status.json` files for every state/evidence combination the pill's
collapsed tile can render, built from `classify()`'s own real thresholds
(`generate.py`) rather than hand-typed numbers — regenerate after any threshold
change, don't hand-edit these files.

| fixture | state | evidence | what it demonstrates |
|---|---|---|---|
| `ok.json` | ok | — | the baseline: no mark, no evidence tag |
| `warn_psi.json` | warn | `psi` | pressure-driven warn — subtitle should read `⚠ pressure · ...` |
| `warn_avail.json` | warn | `avail` | low-memory-driven warn — `⚠ low mem · ...` |
| `warn_eta.json` | warn | `eta` | ETA-driven warn with calm PSI/burn — the exact case FAMILY.md named ("OOM ~Nh" over "pressure 0.0%") |
| `warn_multi.json` | warn | `avail`, `eta` | two conditions firing at once — both named, not collapsed |
| `hot_psi.json` | hot | `psi` | pressure-driven hot |
| `hot_avail.json` | hot | `avail` | critical-memory hot, with a populated advise headline |
| `hot_multi.json` | hot | `psi`, `avail`, `eta` | everything firing — the worst-case header text |
| `swap_storm.json` | (ok underneath) | — | swap storm's own countdown pre-empts the usual subtitle entirely, even though the base memory state alone is calm |

## Using these

The pill hardcodes `/run/ramstein/status.json` — there's no env override for the
extension side, so forcing a state means substituting the real file while the
daemon isn't overwriting it:

```
sudo systemctl stop ramsteind
sudo cp tests/fixtures/pill_states/hot_multi.json /run/ramstein/status.json
# click the tile / open the menu, look at what rendered
sudo systemctl start ramsteind   # restores real polling when done
```

The pill's `GFileMonitor` fires on the copy, no logout/reload needed — this is
exactly what a headless-compositor verification pass (still pending the
operator's word, see thread 4095149a) would drive through automatically once
that route opens.
