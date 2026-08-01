# sutra.mk -- the family's shared RECIPE layer, vendored like code (see
# docs/BOOTSTRAP.md for the sibling code-vendoring mechanics). Include from
# a pill's own root Makefile:
#
#   PILL := coldspot
#   include src/share/coldspot/lib/sutra.mk
#
# PILL must be set before the include line. Everything else resolves
# relative to sutra.mk's OWN location -- $(lastword $(MAKEFILE_LIST)) at
# include-time is exactly that path, however the including Makefile spelled
# it -- never to sutra's own repo. That is the design constraint: get it
# wrong and five pills each debug their own copy of the same mistake, which
# is the exact failure mode this file exists to close.
#
# 441 lines of this recipe were maintained by hand across five pills before
# this existed; check-sutra alone ranged 30-57 lines for what was nominally
# one thing, and every divergence the family suffered was a recipe, never
# vendored CODE (which has never drifted once, because it has a hash
# anchor -- see docs/ARCHITECTURE.md). This is that same guard, extended to
# recipes: ship the recipe itself under the anchor, so a pill runs the
# current correct thing by construction instead of a snapshot someone
# copied by hand and never revisited.

_SUTRA_MK_DIR := $(dir $(lastword $(MAKEFILE_LIST)))
_SUTRA_CANON := $(HOME)/code/REPOS/sutra

# --- check-sutra: integrity (hard gate) + freshness (LAG/DRIFT) -----------
#
# Integrity: the vendored .py's sha256 against its own .version anchor --
# hand-edited or corrupted, always a hard fail, no nuance.
#
# Freshness: the .commit anchor compared against canonical sutra's history
# for THAT FILE SPECIFICALLY -- `git -C "$canon" log -1 --format=%H --
# "$mod.py"` -- never canonical's repo HEAD, which advances on every commit
# including ones that never touch the file at all. That was the 0.7.3 fix
# (decision 325b1969, correcting d51e090f's original HEAD-compare, which
# false-positived a LAG warning across the whole family the first time a
# docs-only commit landed). It reached one pill by hand-copying before this
# file existed; the other four still carry the stale form. Recorded at or
# after the file's own last commit -> fresh. A strict ancestor of it ->
# LAG, the copy has genuinely fallen behind, warn only. Not in canonical's
# history at all -> DRIFT, hard fail.
.PHONY: check-sutra
check-sutra:
	@[ -n "$(PILL)" ] || { echo "check-sutra: set PILL=<pill-name> before including sutra.mk"; exit 1; }
	@canon="$(_SUTRA_CANON)"; libdir="$(_SUTRA_MK_DIR)"; fail=0; \
	for mod in sutra sutra_update sutra_xen; do \
	    py="$$libdir$$mod.py"; ver="$$libdir$$mod.version"; cmt="$$libdir$$mod.commit"; \
	    [ -f "$$py" ] || { echo "check-sutra: $$py not vendored here, skipping $$mod"; continue; }; \
	    v=$$(cut -d' ' -f1 "$$ver"); \
	    sha=$$(awk '{print $$NF}' "$$ver"); \
	    actual=$$(sha256sum "$$py" | cut -d' ' -f1); \
	    if [ "$$sha" != "$$actual" ]; then \
	        echo "check-sutra FAIL: $$py doesn't match $$ver" \
	             "(hand-edited? re-vendor: bash $$canon/vendor.sh $$libdir --bootstrap=$(PILL))"; \
	        fail=1; continue; \
	    fi; \
	    echo "check-sutra: integrity ok ($$mod $$v, sha256 $$sha)"; \
	    if [ -d "$$canon/.git" ]; then \
	        if [ ! -f "$$cmt" ]; then \
	            echo "check-sutra: freshness unknown for $$mod (no $$cmt anchor, an older vendor)"; \
	        else \
	            recorded=$$(cat "$$cmt"); \
	            filehead=$$(git -C "$$canon" log -1 --format=%H -- "$$mod.py"); \
	            if git -C "$$canon" merge-base --is-ancestor "$$filehead" "$$recorded" 2>/dev/null; then \
	                echo "check-sutra: freshness ok ($$mod vendored from $$recorded, at or after its own head $$filehead)"; \
	            elif git -C "$$canon" merge-base --is-ancestor "$$recorded" "$$filehead" 2>/dev/null; then \
	                echo "check-sutra: LAG ($$mod vendored from $$recorded, canonical has since moved to $$filehead) -- warn, not a failure"; \
	            else \
	                echo "check-sutra FAIL: DRIFT ($$mod's vendored commit $$recorded is not in canonical's history at $$canon) -- re-vendor"; \
	                fail=1; \
	            fi; \
	        fi; \
	    fi; \
	done; \
	if [ ! -d "$$canon/.git" ]; then \
	    echo "check-sutra: canonical sutra checkout not present, freshness skipped"; \
	fi; \
	exit $$fail

# --- the row-count primitive ----------------------------------------------
# The exact measurement, not a find+skip-list: three of five pills each
# wrote their own skip-list independently and all three came out wrong
# differently. This is what git itself tracks -- build output and local
# scratch never enter it, by construction (REPO-STANDARD.md's "honest
# measurement"). A pill's own check-repo should reference this variable
# instead of re-deriving the command by hand.
SUTRA_ROOT_ROWS = $(shell git ls-files | cut -d/ -f1 | sort -u | wc -l)

.PHONY: check-sutra-rows
check-sutra-rows:
	@echo "root row count: $(SUTRA_ROOT_ROWS)"

# --- the checkout-run guard ------------------------------------------------
# Till's form (b211651): a binary that silently imported a DIFFERENT
# sutra.py off sys.path would still exit 0 -- rc=0 alone proves nothing
# about WHICH copy got imported. Prove the path itself.
#
# tjmax's refinement (msg 1749): a binary that can't cleanly exit 0 in a
# hardware-free runner (root/CAP_* requirements, no real device to talk to)
# shouldn't fail this guard on that account -- that failure has nothing to
# do with whether the vendored copy resolved correctly.
#
# CORRECTION (msg 2673, Alfred, caught by reproduction): the first cut of
# this target computed the EXPECTED path in shell from the bootstrap
# preamble's own formula and checked that a file exists there. That is a
# LAYOUT check, not a RESOLUTION check -- it never asks Python what the
# binary actually imported. A binary that forgot the bootstrap preamble
# entirely, sitting beside a stale co-located sutra.py (the exact
# pre-migration shape the whole ruling exists to clean up), would still
# import successfully (Python's own sys.path includes the script's own
# directory) and the shell arithmetic would still find a real file at the
# computed path -- green on the precise regression this guard exists to
# catch. Fixed below: load the binary as a module for real and read back
# <module>.<SUTRA_CHECK_MODULE>.__file__, the path Python actually
# resolved, never a second shell computation of what it SHOULD be.
#
# SUTRA_CHECK_BIN is the pill-specific part: which binary to run. Defaults
# to src/bin/$(PILL). SUTRA_CHECK_MODULE is the attribute name the import
# binds (almost always "sutra"; a binary that only imports sutra_update,
# e.g. an update-spine-only tool, sets this to "sutra_update"). Both
# override per pill.
#
# The binary is loaded under a non-"__main__" module name specifically so
# an `if __name__ == "__main__":` guard does NOT fire during the check --
# standard practice for anything meant to be imported cleanly, and the
# same reason SUTRA_CHECK_ARGS/--help exists as a belt-and-suspenders
# smoke path. A binary whose import-time code does real work unconditionally
# (no main-guard) cannot be safely loaded this way; tjmax's
# ModuleNotFoundError/ImportError output-grep remains the correct fallback
# for exactly that case -- it still runs unconditionally below, first.
SUTRA_CHECK_BIN ?= src/bin/$(PILL)
SUTRA_CHECK_ARGS ?= --help
SUTRA_CHECK_MODULE ?= sutra

# A `define`/`endef` block, not a heredoc inlined into the recipe: GNU Make
# requires every physical recipe line to either start with a TAB or be a
# backslash-continuation of one, and a quoted heredoc's body is neither --
# measured, not assumed (the heredoc form hit "missing separator" the first
# time this was tried). `define` is Make's own mechanism for a multi-line
# value; `export` turns it into a real environment variable a subshell can
# read back with `$$VARNAME`, sidestepping Make's own recipe-line rules
# entirely for the payload.
define _SUTRA_CHECK_VENDORED_PATH_PY
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

bin_path, mod_attr, expected = sys.argv[1], sys.argv[2], sys.argv[3]
expected = os.path.realpath(expected)

# Two mistakes measured and corrected here, in order:
#
# 1. SourceFileLoader/exec_module does not add the loaded file's own
#    directory to sys.path, and neither does runpy.run_path on its own --
#    but a real `python3 <bin>` invocation always does, and that is
#    precisely the mechanism a stale sibling import exploits. Replicated
#    deliberately below rather than inherited for free.
# 2. runpy.run_path was tried next and gets (1) right with a manual
#    sys.path insert, but loses everything on an exception -- no partial
#    result. A binary whose import-time code does something unrelated
#    AFTER a successful `import sutra` (tjmax's case, generalized beyond
#    just a subprocess exit code) would then read as "never bound a name",
#    indistinguishable from actually missing the import. exec_module
#    instead updates the module object's namespace incrementally as each
#    top-level statement runs, so whatever was bound BEFORE a later
#    exception survives it -- inspect that, rather than treating any
#    exception as this guard's business.
bin_dir = os.path.dirname(os.path.abspath(bin_path))
sys.path.insert(0, bin_dir)
try:
    loader = SourceFileLoader("_sutra_check_vendored_path_probe", bin_path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    try:
        loader.exec_module(m)
    except BaseException:
        pass
finally:
    sys.path.remove(bin_dir)

actual_mod = getattr(m, mod_attr, None)
if actual_mod is None or not hasattr(actual_mod, "__file__"):
    print(f"check-vendored-path FAIL: {bin_path!r} never bound a name {mod_attr!r} with a "
          f"__file__ (missing the bootstrap preamble and the import entirely, or a different "
          f"attribute name -- set SUTRA_CHECK_MODULE=)")
    sys.exit(1)

actual = os.path.realpath(actual_mod.__file__)
if actual != expected:
    print(f"check-vendored-path FAIL: {bin_path} resolved {mod_attr} to {actual}, expected "
          f"{expected} -- it imported a DIFFERENT copy (stale sibling? missing bootstrap "
          f"preamble?)")
    sys.exit(1)

print(f"check-vendored-path: ok -- {bin_path} resolved {mod_attr} to {actual}")
endef
export _SUTRA_CHECK_VENDORED_PATH_PY

.PHONY: check-vendored-path
check-vendored-path:
	@[ -n "$(PILL)" ] || { echo "check-vendored-path: set PILL=<pill-name> before including sutra.mk"; exit 1; }
	@[ -e "$(SUTRA_CHECK_BIN)" ] || { echo "check-vendored-path: no $(SUTRA_CHECK_BIN) -- set SUTRA_CHECK_BIN="; exit 1; }
	@out=$$(python3 "$(SUTRA_CHECK_BIN)" $(SUTRA_CHECK_ARGS) 2>&1); rc=$$?; \
	if [ $$rc -ne 0 ] && echo "$$out" | grep -qE 'ModuleNotFoundError|ImportError'; then \
	    echo "check-vendored-path FAIL: $(SUTRA_CHECK_BIN) could not import $(SUTRA_CHECK_MODULE) from the checkout:"; \
	    echo "$$out"; exit 1; \
	fi; \
	expected="$$(cd "$(_SUTRA_MK_DIR)" && pwd)/$(SUTRA_CHECK_MODULE).py"; \
	echo "$$_SUTRA_CHECK_VENDORED_PATH_PY" | python3 - "$(SUTRA_CHECK_BIN)" "$(SUTRA_CHECK_MODULE)" "$$expected"
