"""ledgerbench CLI (typer). Subcommands: doctor smoke run-all gate ladder confirm
soak analyze verify status.

doctor + smoke run live. run-all is the live, ledger-backed, resumable
runner that orchestrates the phases (phases.py). gate/ladder/confirm/soak are
per-phase re-run entry
points wired to the same phase functions, each requiring --run-id. analyze reads
a finished ledger and emits the knee fits / stats / report. verify rechecks the
run's published artifacts against its checksum map. status renders the
ledger. (Seeding is compose/Make only; there is no `seed` subcommand here.)

A measured stage REFUSES TO RUN unless the capacity gate passed in that run;
that includes the per-stage commands, which re-assert the recorded verdict
before their first probe. The two operator escape hatches are environment
variables (see ENV_HELP below), echoed in `--help` so they are discoverable
without reading the source.

Everything is `uv run`-able directly so PowerShell users skip make.
"""

from __future__ import annotations

import platform
import sys

import typer
from rich.console import Console
from rich.table import Table

from . import doctor, phases, smoke
from .ledger import PHASE_ORDER, Ledger, State
from .util.clock import rfc3339_micros

# Operator environment variables. Repeated in the epilog of every command that
# can hit the capacity gate or the ladder, so `--help` alone tells the operator
# what the escape hatches are and what they cost.
ENV_HELP = (
    "Environment:\n"
    f"  {phases.ALLOW_DB_IMPOSED_KNEE_ENV}=1  proceed even though the capacity "
    "gate did not pass (default: ABORT before any measured probe). The run is "
    "stamped degraded in the ledger and the manifest, and every stage command "
    "re-checks the recorded verdict, so the caveat cannot be lost on a resume.\n"
    f"  {phases.ALLOW_SUB_KNEE_LADDER_ENV}=1  proceed even though a slot "
    "reached the ladder's rung ceiling WITHOUT crossing the SLO (default: "
    "ABORT). The headline metric is cost AT the crossing rate, so such a slot "
    "has no operating point and the cell is not a comparison at a fixed latency "
    "promise. The cell's ladder stage is stamped degraded with "
    "`ceiling_reached_without_crossing` naming the slot; raising "
    "`ladder_max_rungs` or `ladder_start_rps` is the real fix.\n"
    f"  {phases.CACHE_TTL_OVERRIDE_ENV}=<seconds>  supply the cache TTL that "
    "realizes the gate's cache_hit_rate lever (spec/slo.yaml pre-registers no "
    "value; without it the lever is recorded NOT APPLIED and a PASS that "
    "depends on it fails the gate).\n"
    f"  {phases.BODY_POOL_ENV}=<n>  requests the shared request-BODY pool is "
    "sized for on every MEASURED tier (full, rehearsal): the working-set dial "
    "(default 1000000; the create/quote split comes from the mix). It is a "
    "FLOOR, not the size of any draw -- each attack draws rate x duration x 1.02 "
    "targets from its own PCG64 stream. Pinned into the ledger at run start, so "
    "set it BEFORE starting a run; on resume the pin wins over the environment. "
    f"The predecessor {phases._LEGACY_TARGET_COUNT_ENV} is REFUSED, not ignored: "
    "it sized the one targets file a run replayed, and nothing replays now.\n"
    f"  {phases.RUN_VARIANTS_ENV}=<ids>  comma-separated variant cell ids to "
    "EXECUTE (e.g. variant.npgsql-default-prepare); unknown ids abort. Default: "
    "variants are declared but not run. Each executed variant is a FULL cell "
    "(ladder/fit/confirm/soak, roughly 15 h at full scale). Also pinned into the "
    "ledger at run start."
)

app = typer.Typer(
    name="ledgerbench",
    help="Java vs .NET Ledgerline benchmark runner.",
    epilog=ENV_HELP,
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


# Built from the builder table for the same reason the error message is: a help
# string that lists the tiers by hand is one more place to forget a new one.
_SCALE_HELP = (
    f"{'|'.join(phases.SCALES)}; omit on an existing run to adopt its pinned scale"
)


def _validated_scale(scale: str) -> str:
    """Reject an unknown `--scale` as a CLI error, not a traceback.

    `phases.ScaleProfile.resolve` raises ValueError, and `make_context` is called
    OUTSIDE every `try/except PhaseError` in this module -- so before this guard
    a mistyped tier surfaced as an uncaught ValueError. The choices come from
    `phases.SCALES`, so this message cannot drift from the set that builds.
    """
    if scale not in phases.SCALES:
        raise typer.BadParameter(
            f"unknown scale {scale!r}; expected {'|'.join(phases.SCALES)}",
            param_hint="--scale",
        )
    return scale


def _scale_for(run_id: str, scale: str | None, default: str) -> str:
    """`--scale` if given, else the run's pinned scale, else `default`.

    The ledger pins the scale and `Ledger.load_or_create` REFUSES a mismatch, so
    a fixed default made every scale-omitted resume of a non-default tier fail
    with a RuntimeError naming a scale the operator never typed.
    """
    if scale is not None:
        return _validated_scale(scale)
    led = Ledger.load(run_id)
    pinned = getattr(led, "scale", None)
    return pinned if pinned else default


def _new_run_id(scale: str) -> str:
    """Scale + UTC timestamp + host (the run_id).

    The scale leads so `.gitignore`'s `results/smoke-*/` rule can match. Round 1
    ran timestamp-first, so that rule matched nothing and every functional
    rehearsal's artifacts were staged for commit alongside measured ones.
    """
    stamp = rfc3339_micros().replace(":", "").replace(".", "").replace("-", "")[:15]
    return f"{scale}-{stamp}-{platform.node()}"


@app.command(name="doctor")
def doctor_cmd(
    require_docker: bool = typer.Option(False, "--require-docker", help="Fail if docker is absent (Linux runs)."),
    out: str = typer.Option("", help="Optional path to write doctor.json."),
) -> None:
    """Preflight checks (per-OS; Windows runs a subset)."""
    report = doctor.run_doctor(require_docker=require_docker)
    table = Table(title="ledgerbench doctor")
    table.add_column("check")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for c in report.checks:
        status = "[yellow]skip[/]" if c.skipped else ("[green]ok[/]" if c.ok else "[red]FAIL[/]")
        table.add_row(c.name, status, c.detail)
    console.print(table)
    if out:
        from pathlib import Path

        report.save(Path(out))
    raise typer.Exit(code=0 if report.all_ok else 1)


@app.command(name="smoke")
def smoke_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command plan, run nothing."),
    seed: int = typer.Option(-1, help="Override the target-generator seed (default: workload.yaml)."),
) -> None:
    """Windows smoke flow (build/seed-smoke/up/probe/teardown) -- functional only."""
    report = smoke.run_smoke(dry_run=dry_run, seed=None if seed < 0 else seed)
    table = Table(title=f"ledgerbench smoke [{report.label}] (resources: {report.resource_accounting})")
    table.add_column("step")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for s in report.steps:
        status = "[blue]deferred[/]" if s.deferred else ("[green]ok[/]" if s.ok else "[red]FAIL[/]")
        table.add_row(s.name, status, s.detail)
    console.print(table)
    raise typer.Exit(code=0 if report.ok else 1)


# --------------------------------------------------------------------------- #
# run-all (live, resumable)
# --------------------------------------------------------------------------- #
def _no_such_run_message(run_id: str) -> str:
    unfinished = [led.run_id for _, led in Ledger._scan() if not led.is_complete()]
    hint = (
        f" Unfinished runs: {', '.join(unfinished)}."
        if unfinished
        else " There are no unfinished runs."
    )
    return f"run {run_id!r} has no ledger (results/{run_id}/ledger.json).{hint}"


def _require_existing_run(run_id: str) -> None:
    """Per-stage commands operate on an EXISTING run.

    Without this check, `make_context` on a mistyped --run-id materializes a
    brand-new ledger and manifest for the typo as a side effect of a command
    whose help text says "re-run" -- and the operator's real run sits untouched
    while a phantom sibling accumulates state beside it.
    """
    if Ledger.load(run_id) is None:
        raise typer.BadParameter(_no_such_run_message(run_id))


def _resolve_resume_run_id(
    run_id: str | None, resume: bool, scale: str | None
) -> tuple[str, bool, str]:
    """Resolve (run_id, is_resume, effective_scale) from the flags.

    no flags                  -> NEW run (fresh run_id)
    --resume (no --run-id)    -> most recent unfinished ledger (error if none)
    --resume --run-id X       -> resume exactly X; REFUSED if X has no ledger
    --run-id X (no --resume)  -> resume X if it exists, else a NEW run named X

    The --resume --run-id refusal is load-bearing: a mistyped id used to fall
    through to the NEW-run branch, whose fresh-seed path runs
    `docker compose down -v` -- destroying the very volumes the crashed run's
    continuation needed. A typo after a power cut must be an error, never a
    silently different run.

    An EXISTING run takes its scale from its own ledger when `--scale` is
    omitted; only a new run falls back to the `full` default.
    """
    if resume and run_id is None:
        led = Ledger.latest_unfinished()
        if led is None:
            raise typer.BadParameter(
                "no unfinished run to resume (no results/*/ledger.json with pending "
                "work). Start a fresh run with `run-all` (no --resume)."
            )
        return led.run_id, True, _scale_for(led.run_id, scale, default="full")
    if run_id is not None:
        existing = Ledger.load(run_id)
        if resume and existing is None:
            raise typer.BadParameter(_no_such_run_message(run_id))
        return run_id, existing is not None, _scale_for(run_id, scale, default="full")
    resolved_scale = _validated_scale(scale) if scale is not None else "full"
    return _new_run_id(resolved_scale), False, resolved_scale


def _resume_banner(led: Ledger) -> None:
    done = total = 0
    for c in led.cells:
        for st in ("ladder", "fit", "confirm", "soak"):
            d, t = led.stage_counts(c.cell_id, st)
            done += d
            total += t
    console.print(
        f"[cyan]resuming[/] {led.run_id}: done {done}/{total} probes, "
        f"next: [bold]{led.next_pending()}[/]"
    )


@app.command(name="run-all", epilog=ENV_HELP)
def run_all(
    scale: str = typer.Option(None, help=_SCALE_HELP),
    run_id: str = typer.Option(None, "--run-id", help="Resume/target a specific run id."),
    resume: bool = typer.Option(False, "--resume", help="Resume an existing ledger."),
) -> None:
    """Full phase pipeline, live + ledger-backed + resumable.

    ABORTS at the capacity gate unless it passes: a run whose DB headroom was
    never established is not a language comparison at a fixed latency promise.
    """
    resolved_id, is_resume, resolved_scale = _resolve_resume_run_id(run_id, resume, scale)
    ctx = phases.make_context(run_id=resolved_id, scale=resolved_scale)
    if is_resume:
        _resume_banner(ctx.led)
    else:
        console.print(f"[green]new run[/] {resolved_id} (scale={resolved_scale})")
    try:
        phases.run_all(ctx)
    except phases.PhaseError as exc:
        console.print(f"[red]run-all failed[/]: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]run-all complete[/] {resolved_id} "
        f"(complete={ctx.led.is_complete()})"
    )


# --------------------------------------------------------------------------- #
# per-phase re-run entry points (each requires --run-id)
# --------------------------------------------------------------------------- #
def _cell_or_fail(ctx, cell_id: str) -> phases.Cell:
    for c in phases.CELLS:
        if c.cell_id == cell_id:
            return c
    raise typer.BadParameter(f"unknown cell {cell_id!r}; known: {[c.cell_id for c in phases.CELLS]}")


def _guarded(label: str, fn, *args) -> None:
    """Run a phase function, turning a PhaseError into a clean exit-1 message.

    These commands re-assert the capacity gate before their first probe, so a
    non-passing (or never-run) gate surfaces here as an operator-readable
    refusal instead of a traceback.
    """
    try:
        fn(*args)
    except phases.PhaseError as exc:
        console.print(f"[red]{label} refused[/]: {exc}")
        raise typer.Exit(code=1) from exc


@app.command(epilog=ENV_HELP)
def gate(
    run_id: str = typer.Option(..., "--run-id", help="Run id to (re)run the gate in."),
    scale: str = typer.Option(None, help=_SCALE_HELP),
) -> None:
    """DB capacity gate (create-only ladder + levers), wired to phases.

    A non-passing verdict FAILS this command (exit 1) and blocks every measured
    stage in the run until the gate passes or the opt-out is set.
    """
    _require_existing_run(run_id)
    ctx = phases.make_context(run_id=run_id, scale=_scale_for(run_id, scale, default="smoke"))
    _guarded("gate", phases.run_capacity_gate_phase, ctx)
    console.print(f"[green]gate done[/] {run_id}: {ctx.led.phase('capacity_gate').data}")


@app.command(epilog=ENV_HELP)
def ladder(
    run_id: str = typer.Option(..., "--run-id"),
    cell: str = typer.Option("headline.mixed.gc-default", help="cell id"),
    scale: str = typer.Option(None, help=_SCALE_HELP),
) -> None:
    """Geometric mixed ladder -> bracket EACH SLOT's SLO crossing (per-cell).

    Re-asserts the run's capacity gate first: it adopts the gate's EFFECTIVE
    workload (so the levers the gate applied are actually in force) and refuses
    to probe when the recorded verdict is not `pass`, or when no gate ever ran
    in this run.
    """
    _require_existing_run(run_id)
    ctx = phases.make_context(run_id=run_id, scale=_scale_for(run_id, scale, default="smoke"))
    _guarded("ladder", phases.run_ladder, ctx, _cell_or_fail(ctx, cell))
    console.print(f"[green]ladder done[/] {cell}: {ctx.led.stage(cell, 'ladder').data}")


@app.command(epilog=ENV_HELP)
def confirm(
    run_id: str = typer.Option(..., "--run-id"),
    cell: str = typer.Option("headline.mixed.gc-default", help="cell id"),
    scale: str = typer.Option(None, help=_SCALE_HELP),
) -> None:
    """Confirm reps at each slot's own fitted knee (RCB interleave, per-cell).

    Gate-guarded exactly like `ladder` (both the fit and the confirm probes
    re-assert the recorded capacity-gate verdict and effective workload).
    """
    _require_existing_run(run_id)
    ctx = phases.make_context(run_id=run_id, scale=_scale_for(run_id, scale, default="smoke"))
    c = _cell_or_fail(ctx, cell)
    # fit must precede confirm (computes each slot's confirm rate from its knee)
    _guarded("confirm", phases.run_fit, ctx, c)
    _guarded("confirm", phases.run_confirm, ctx, c)
    console.print(f"[green]confirm done[/] {cell}: {ctx.led.stage(cell, 'confirm').data}")


@app.command(epilog=ENV_HELP)
def soak(
    run_id: str = typer.Option(..., "--run-id"),
    cell: str = typer.Option("headline.mixed.gc-default", help="cell id"),
    scale: str = typer.Option(None, help=_SCALE_HELP),
) -> None:
    """Soak + stationarity (per-cell).

    Gate-guarded exactly like `ladder`.
    """
    _require_existing_run(run_id)
    ctx = phases.make_context(run_id=run_id, scale=_scale_for(run_id, scale, default="smoke"))
    _guarded("soak", phases.run_soak, ctx, _cell_or_fail(ctx, cell))
    console.print(f"[green]soak done[/] {cell}: {ctx.led.stage(cell, 'soak').data}")


@app.command()
def analyze(run_id: str = typer.Argument(..., help="results/<run_id> to analyze")) -> None:
    """Per-slot knee fits, stationarity, the paired cost verdicts,
    the CPU-vs-rate curve and the resource roll-up -> report/.

    The console table below is a summary of `report/report.md`; every caveat the
    numbers carry (what the printed knee interval is, which slot met the latency
    promise at its own rate, what each paired verdict's scope is) lives there.
    """
    from . import analyze as analyze_mod

    try:
        result = analyze_mod.analyze_run(run_id)
    except (FileNotFoundError, analyze_mod.SpecDriftError) as exc:
        console.print(f"[red]analyze failed[/]: {exc}")
        raise typer.Exit(code=1) from exc

    if not result.analyzable:
        console.print(
            f"[red]no analyzable cells[/] in {run_id}: no cell has a completed "
            f"ladder stage to fit a knee from. Run the cells phase first "
            f"(`run-all` / `ladder`)."
        )
        raise typer.Exit(code=1)

    # Column names must not promise more than the numbers are. The interval is a
    # bracketed refit spread, NOT a 95% CI (report.md explains why it cannot be
    # one at ladder rep counts).
    # ONE ROW PER SLOT: each slot has its own knee.
    table = Table(title=f"knee fits: {run_id} (SLO p99 = {result.slo_ms:g} ms)")
    table.add_column("cell")
    table.add_column("slot")
    table.add_column("knee rps")
    table.add_column("bootstrap interval")
    table.add_column("inferential?")
    table.add_column("paired cost verdict (own knee)", overflow="fold")
    for c in result.cells:
        verdict_s = (
            f"{c.paired.verdict} on {c.paired.metric} (n={c.paired.n_blocks})"
            if c.paired else "[dim]n/a[/]"
        )
        for sid, k in c.knee_by_slot.items():
            if not k.crossed:
                knee_s, ci_s, valid_s = "[dim]no crossing[/]", "n/a", "n/a"
            elif k.knee_rps is None:
                knee_s, ci_s, valid_s = "[dim]unfit[/]", "n/a", "n/a"
            else:
                knee_s = f"{k.knee_rps:.0f}"
                ci_s = f"[{k.ci_low_rps:.0f}, {k.ci_high_rps:.0f}]"
                valid_s = "[green]yes[/]" if k.ci_valid else "[yellow]NOT a CI[/]"
            table.add_row(c.cell_id, sid, knee_s, ci_s, valid_s, verdict_s)
            verdict_s = ""
    console.print(table)
    console.print(f"[green]wrote[/] {result.fits_path}")
    console.print(f"[green]wrote[/] {result.report_path}")


@app.command()
def verify(run_id: str = typer.Argument(..., help="results/<run_id> to verify")) -> None:
    """Recheck every published artifact against CHECKSUMS.sha256 (exit 1 on any fault).

    The scope is the PUBLICATION scope, and git states it directly (`git ls-files
    --cached --others --exclude-standard` under the run directory), so a green
    result means the tree a reader can obtain is the tree that was measured. An
    absent or empty map is a failure, not a pass, and so is a scope git could not
    be asked for.
    """
    from . import checksums
    from .util import paths

    res = checksums.verify(run_id)
    if res.fault:
        console.print(f"[red]verify failed[/]: {res.fault}")
        raise typer.Exit(code=1)

    for label, items in (
        ("MISMATCH", res.mismatched),
        ("MISSING", res.missing),
        ("UNLISTED", res.unlisted),
        ("OUT-OF-SCOPE", res.out_of_scope),
        ("PROBE-DIVERGENT", res.probe_divergent),
    ):
        for item in items:
            console.print(f"[red]{label}[/] {item}")

    if res.ok:
        console.print(f"[green]ok[/] {res.checked} published artifacts verified in {run_id}")
        raise typer.Exit(code=0)
    console.print(
        f"[red]verify FAILED[/] {run_id}: {res.checked} listed, "
        f"{len(res.mismatched)} mismatched, {len(res.missing)} missing, "
        f"{len(res.unlisted)} unlisted, {len(res.out_of_scope)} out-of-scope, "
        f"{len(res.probe_divergent)} probe-divergent"
    )
    # An out-of-scope or unlisted path usually means the ignore rules moved
    # between the run and the check, not that an artifact moved, and the map
    # records no vintage of its own. `check-ignore -v` is the one form of the
    # answer that names the rule AND the file it lives in -- which may be
    # `.git/info/exclude` or a `core.excludesFile`, where no reader would look.
    if res.unlisted or res.out_of_scope:
        console.print(
            "to see which rule decided a path, run `git check-ignore -v <path>` "
            f"in {paths.run_dir(run_id)}"
        )
    raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _state_markup(state: State | str) -> str:
    s = state.value if isinstance(state, State) else str(state)
    return {
        "done": "[green]done[/]",
        "running": "[yellow]running[/]",
        "pending": "[dim]pending[/]",
        "failed": "[red]FAILED[/]",
    }.get(s, s)


@app.command()
def status(run_id: str = typer.Argument(None, help="run id (default: latest ledger)")) -> None:
    """Rich phase/cell/stage table + a machine-readable STATUS summary line."""
    led = Ledger.load(run_id) if run_id else Ledger.latest()
    if led is None:
        console.print("[red]no ledger found[/] (no results/*/ledger.json yet)")
        raise typer.Exit(code=1)

    phase_table = Table(title=f"phases: {led.run_id}")
    phase_table.add_column("phase")
    phase_table.add_column("state")
    phase_table.add_column("detail", overflow="fold")
    for name in PHASE_ORDER:
        if name == "cells":
            continue
        rec = led.phases.get(name)
        st = rec.state if rec else State.PENDING
        detail = ""
        if rec and rec.data:
            keys = [k for k in ("verdict", "scale", "seed_scale", "skipped", "tier",
                                "equivalence") if k in rec.data]
            detail = ", ".join(f"{k}={rec.data[k]}" for k in keys)
        phase_table.add_row(name, _state_markup(st), detail)
    console.print(phase_table)

    if led.cells:
        cell_table = Table(title="cells")
        cell_table.add_column("cell")
        for st in ("ladder", "fit", "confirm", "soak"):
            cell_table.add_column(st)
        for c in led.cells:
            row = [c.cell_id]
            for st in ("ladder", "fit", "confirm", "soak"):
                stage = getattr(c, st)
                d, t = led.stage_counts(c.cell_id, st)
                label = _state_markup(stage.state)
                if t:
                    label += f" {d}/{t}"
                row.append(label)
            cell_table.add_row(*row)
        console.print(cell_table)

    nxt = led.next_pending()
    if led.is_complete():
        run_state = "complete"
    elif any(r.state == State.FAILED for r in led.phases.values()) or any(
        getattr(c, st).state == State.FAILED
        for c in led.cells for st in ("ladder", "fit", "confirm", "soak")
    ):
        run_state = "failed"
    else:
        run_state = "running"
    console.print(f"[bold]next:[/] {nxt}")
    console.print(f"STATUS run_id={led.run_id} state={run_state} next={nxt}")
    raise typer.Exit(code=0)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        console.print("[red]interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
