"""Smoke tests: report emits valid markdown."""

from __future__ import annotations

import numpy as np
import pandas as pd

from analysis import report
from analysis.paired import paired_difference


def test_verdict_word_uses_the_callers_labels():
    """The rendered name comes from the caller, never from a table in here.

    The verdict token is side-neutral by design; a hard-coded {"go_better":
    "Go better"} map renders the wrong stack's name as soon as the compared pair
    changes, and P2 compares two .NET slots.
    """
    rng = np.random.default_rng(1)
    ref = 100 + rng.normal(0, 1, 12)
    sub = 80 + rng.normal(0, 1, 12)
    r = paired_difference(
        sub, ref, metric="cpu_ms_per_req",
        subject_id="dotnet-efcore", reference_id="dotnet-ado",
    )
    labels = {"dotnet-efcore": ".NET/EF Core", "dotnet-ado": ".NET/ADO"}
    assert r.verdict == "subject_better"
    assert report.verdict_word(
        r.verdict, subject=r.subject, reference=r.reference, labels=labels
    ) == ".NET/EF Core better"

    flipped = paired_difference(
        ref, sub, metric="cpu_ms_per_req",
        subject_id="dotnet-efcore", reference_id="dotnet-ado",
    )
    assert report.verdict_word(
        flipped.verdict, subject=flipped.subject, reference=flipped.reference,
        labels=labels,
    ) == ".NET/ADO better"


def test_verdict_word_renders_a_tie_without_a_slot_name():
    rng = np.random.default_rng(3)
    ref = 100 + rng.normal(0, 1.0, 12)
    sub = ref + rng.normal(0, 0.3, 12)
    r = paired_difference(sub, ref, metric="cpu_ms_per_req", margin_pct=5.0)
    assert r.is_tie
    assert report.verdict_word(
        r.verdict, subject=r.subject, reference=r.reference, labels={}
    ) == "tie (within +-margin)"


def test_cost_per_million_formula():
    inputs = report.CostInputs(
        vcpu_hour_usd=0.04,
        cpu_ms_per_req_by_slot={"java": 0.5, "dotnet": 0.8},
        labels={"java": "Java", "dotnet": ".NET"},
        sensitivity_pct=20.0,
    )
    c = report.cost_per_million(inputs)["by_slot"]
    # 0.5 ms * $0.04 / 3.6 = $0.005555.. per million
    assert abs(c["java"]["usd_per_million"] - (0.5 * 0.04 / 3.6)) < 1e-12
    assert c["java"]["low"] < c["java"]["usd_per_million"] < c["java"]["high"]


def test_cost_table_labels_every_slot_it_was_given():
    inputs = report.CostInputs(
        vcpu_hour_usd=0.04,
        cpu_ms_per_req_by_slot={"dotnet-efcore": 0.6, "dotnet-ado": 0.5},
        labels={"dotnet-efcore": ".NET/EF Core", "dotnet-ado": ".NET/ADO"},
    )
    md = report.cost_table(inputs)
    assert ".NET/EF Core" in md and ".NET/ADO" in md
    assert "Illustrative only" in md


def test_df_to_md_empty():
    assert "no data" in report._df_to_md(pd.DataFrame())


def test_cost_table_publishes_a_figure_instead_of_a_rounded_zero():
    """At sub-millisecond CPU and a cents-per-hour price, `$/M` lands in the
    thousandths and `:.2f` published every slot as `$0.00` -- a zero for a
    figure the run measured."""
    inputs = report.CostInputs(
        vcpu_hour_usd=0.04,
        cpu_ms_per_req_by_slot={"dotnet-efcore": 0.3, "dotnet-ado": 0.1},
        labels={"dotnet-efcore": ".NET/EF Core", "dotnet-ado": ".NET/ADO"},
    )
    md = report.cost_table(inputs)
    assert "$0.0033" in md and "$0.0011" in md
    assert "$0.00 " not in md and "$0.00]" not in md


def test_cost_table_reads_the_formula_back_instead_of_restating_it():
    """The note used to carry the formula as a literal while `cost_per_million`
    returned its own copy; the two agreed by inspection only."""
    inputs = report.CostInputs(
        vcpu_hour_usd=0.04, cpu_ms_per_req_by_slot={"a": 1.0},
    )
    assert report.cost_per_million(inputs)["formula"] in report.cost_table(inputs)
