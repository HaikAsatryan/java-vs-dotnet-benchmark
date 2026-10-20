# Next round: what I found after this run, and what I will change

Written 2026-10-20. Next year, with newer Java and .NET releases, I will most probably run this
benchmark again. Before that I will fold in the changes below. This file records what I found
after the run, so the next one starts from it.

## 1. The host: the kernel's preferred-core packing (ITMT)

**What it is.** On this machine (Ryzen 9 7950X) amd-pstate ranks the cores unevenly, from the
firmware's per-core performance values, and from that ranking the kernel turns on ITMT, x86's
asym packing. The scheduler then keeps pulling running work onto the higher-ranked CPUs. Inside
this run's SUT cpuset (0-5) the ranking reads 236, 226, 236, 221, 231, 216 (read on this machine
on 2026-10-20). Turbo boost off, which `infra/host-setup.sh` applies, makes it worse: no core can
boost, so the ranking prefers cores for headroom they do not have.

**How I found it.** In a later benchmark on this same machine, a service with a two-worker async
runtime on 2 CPUs had latency-collapse episodes: both workers sat runnable on the higher-ranked
CPU while the other one idled, for up to about a second. Turning ITMT off
(`/sys/kernel/debug/x86/sched_itmt_enabled` set to 0), with nothing else changed, removed the
episodes.

**Was it on in this run?** Almost certainly. The run read back kernel `7.1.8-100.fc43`, scaling
driver `amd-pstate-epp` (active mode), boost 0 on every policy, governor `performance`, and a
kernel command line without `amd_prefcore=disable`. With those, the kernel enables ITMT on this
CPU. The run did not read the ITMT knob or the per-CPU ranking, and `host-setup.sh` never changes
either.

**What the data shows: no sign of the stall.**

- No window has the clean-or-collapsed shape the stall produces. The ladder rises gradually,
  and the confirm windows spread around the promise (Java 17.5 to 39.2 ms p99, .NET 16.3 to
  33.7 ms).
- Almost nothing is excluded, so nothing hides behind the gates: .NET used 18 of its 20 confirm
  windows and 40 of 42 ladder windows; Java used all 10 and all 42.
- Both stacks cross 20 ms at about the same rate: Java at 4,799 rps, .NET at 4,833.

I still cannot rule ITMT out completely without an on/off comparison. When I run that check on
this machine for an earlier benchmark, I will most probably add this repo's headline cell to it.
The machine now runs Ubuntu, so that comparison gives the on/off difference, not a re-measurement
of the published numbers.

## 2. What limits the crossing

At the crossing no container is close to its CPU budget. Median over the confirm and soak
windows at each stack's knee:

| At | SUT | PostgreSQL | vegeta | Redis |
|---|---|---|---|---|
| 4,799 rps, Java | 1.13 of 6 cores | 0.48 of 3 | 0.40 of 3 | 0.03 of 1 |
| 4,833 rps, .NET | 2.34 of 6 cores | 0.55 of 3 | 0.41 of 3 | 0.03 of 1 |

Two stacks with very different CPU cost crossing at the same rate, with this much idle CPU
everywhere, points to a shared limit that is not the SUTs' CPU. Candidates are database waits
(row locks on the create path, WAL fsync), the connection pools, Docker networking, or the host
scheduler. This run does not record enough to tell which.

If the limit is shared, the headline rate measures that limit rather than each stack's own
capacity. The cost ratio there is still a fair same-rate comparison, but "the rate where p99
crosses 20 ms" then says little about either stack.

## 3. What I will change for the next run

**Host**

- Turn ITMT off for both stacks at every run start and resume, read it back, and restore it at
  the end. Booting with `amd_prefcore=disable` is the other way to do it.
- Record the preferred-core state and the per-CPU ranking in `host_state.json`, beside the
  settings it already reads.
- Since real servers may run with ITMT on, run a small ITMT-on sensitivity check and publish it
  beside the headline.

**Measurement**

- Find the limit before choosing the headline rate. Record, per window: PostgreSQL wait events
  (sampled `pg_stat_activity`), lock waits, WAL fsync time, pool wait time on both stacks, and
  per-CPU scheduler counters.
- Check the generator's own headroom, so a generator or network limit is never read as a SUT
  limit.
- If the crossing turns out to be shared again, move the shared limit out of the way (more
  database cores, a different create path, host networking) before the freeze. If it can't
  move, publish it as a shared limit.
- Publish per-second p99 beside pooled p99 for every window, so an episode would be visible.
- Several full runs on different nights, so a result that replicates can be told apart from one
  that does not.

**Process**

- Pre-register the rules before any measured data and keep the hostile review before the freeze.
- The then-current Java, .NET, PostgreSQL and kernel versions, pinned and recorded.
