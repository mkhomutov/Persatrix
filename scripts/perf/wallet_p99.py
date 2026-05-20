"""Wallet acquire+settle loopback p99 latency harness (RFC 0023 §Goal #6).

Drives N acquire+settle cycles against the orchestrator's WalletService
gRPC and reports p50/p95/p99 of the (acquire+settle) latency. The RFC
target is p99 ≤ 5 ms (informational, not a release blocker).

Usage:
    python scripts/perf/wallet_p99.py --target localhost:9090 --cycles 1500 --warmup 100
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
import time
from pathlib import Path

import grpc

# agents is laid out as a real package with generated/*.py emitted using
# relative imports (`from . import …`), so importing through the package
# works once the repo root is on sys.path. Resolve from __file__ so the
# script is robust to CWD (scripts/perf/, repo root, anywhere else).
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from agents.generated import wallet_pb2 as walletpb  # noqa: E402
from agents.generated import wallet_pb2_grpc as walletgrpc  # noqa: E402

# Per-RPC timeout. Picked generously vs. the 5 ms RFC target so a hung
# orchestrator surfaces as a clean DEADLINE_EXCEEDED instead of blocking
# the harness indefinitely.
_RPC_TIMEOUT_S = 5.0


def _nearest_rank(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile (1-indexed: ceil(p*N), 0-indexed: ceil(p*N)-1).

    This matches the convention used by most observability tooling
    (Prometheus `histogram_quantile`, NumPy `interpolation="lower"` on
    integer ranks). Earlier revs of this harness used `int(p*N)` which is
    one slot higher than nearest-rank for evenly-divisible n; baselines
    captured under that convention are within tail-noise of these values.
    """
    n = len(sorted_values)
    idx = max(0, math.ceil(p * n) - 1)
    return sorted_values[idx]


async def run(target: str, cycles: int, warmup: int) -> None:
    channel = grpc.aio.insecure_channel(target)
    stub = walletgrpc.WalletServiceStub(channel)
    latencies_ms: list[float] = []

    model = "claude-haiku-4-5-20251001"
    n = 0
    run_id = int(time.time())

    async def one_cycle() -> float:
        nonlocal n
        # Rotate agent_id every 20 cycles so each agent stays well under the
        # orchestrator's 60-calls/60s rate-limiter bucket (acquire+settle = 2
        # calls per cycle, so 20 cycles = 40 calls per bucket). The run_id
        # prefix makes each harness invocation use fresh buckets.
        agent_id = f"perf-{run_id}-{n // 20}"
        n += 1
        meta = (("x-agent-id", agent_id),)
        req = walletpb.LeaseRequest(
            agent_id=agent_id,
            model=model,
            estimated_input_tokens=100,
            estimated_max_output_tokens=100,
            cause=walletpb.CAUSE_UNSPECIFIED,
        )
        t0 = time.perf_counter()
        resp = await stub.AcquireLease(req, metadata=meta, timeout=_RPC_TIMEOUT_S)
        outcome = resp.WhichOneof("outcome")
        if outcome != "grant":
            raise RuntimeError(f"unexpected outcome: {outcome}")
        lease_id = resp.grant.lease_id
        ack = await stub.SettleLease(walletpb.SettlementRequest(
            lease_id=lease_id,
            actual_input_tokens=80,
            actual_output_tokens=50,
        ), metadata=meta, timeout=_RPC_TIMEOUT_S)
        elapsed = (time.perf_counter() - t0) * 1000.0
        if not ack.success:
            raise RuntimeError(f"settle failed: {ack.error_message}")
        return elapsed

    try:
        print(f"Warming up {warmup} cycles (untimed) ...", flush=True)
        for i in range(warmup):
            try:
                await one_cycle()
            except Exception as e:
                print(f"warmup cycle {i} failed (n={n}): {e}", flush=True)
                raise

        print(f"Measuring {cycles} cycles ...", flush=True)
        for i in range(cycles):
            try:
                latencies_ms.append(await one_cycle())
            except Exception as e:
                print(f"measure cycle {i} failed (n={n}): {e}", flush=True)
                raise
            if (i + 1) % 500 == 0:
                print(f"  ... {i+1}/{cycles}", flush=True)
    finally:
        await channel.close()

    sorted_ms = sorted(latencies_ms)
    p50 = _nearest_rank(sorted_ms, 0.50)
    p95 = _nearest_rank(sorted_ms, 0.95)
    p99 = _nearest_rank(sorted_ms, 0.99)
    mean = statistics.fmean(sorted_ms)
    print()
    print(f"cycles:  {len(sorted_ms)}")
    print(f"mean:    {mean:.3f} ms")
    print(f"p50:     {p50:.3f} ms")
    print(f"p95:     {p95:.3f} ms")
    print(f"p99:     {p99:.3f} ms")
    print(f"max:     {sorted_ms[-1]:.3f} ms")
    print(f"min:     {sorted_ms[0]:.3f} ms")
    print()
    print(f"RFC 0023 Goal #6 target: p99 <= 5 ms -- {'PASS' if p99 <= 5.0 else 'INFO (above target)'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="localhost:9090")
    ap.add_argument("--cycles", type=int, default=1500)
    ap.add_argument("--warmup", type=int, default=100)
    args = ap.parse_args()
    asyncio.run(run(args.target, args.cycles, args.warmup))


if __name__ == "__main__":
    main()
