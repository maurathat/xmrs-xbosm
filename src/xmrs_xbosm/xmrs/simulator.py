"""XMRS network propagation simulator.

Builds a random graph with regional edge latencies, runs single-source message
propagation with a min-heap (Dijkstra-style event order), and evaluates **flood**
plus **selective** gossip for each configured fan-out **K** (separate rows per policy
label, e.g. ``selective_k8``).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from xmrs_xbosm.common.config import load_yaml
from xmrs_xbosm.common.io import append_jsonl
from xmrs_xbosm.common.models import TelemetryEvent, utc_now_iso

PolicyName = Literal["flood", "selective", "adaptive_weighted"]

# Default selective fan-outs when ``selective_ks`` is omitted from config.
DEFAULT_SELECTIVE_KS: tuple[int, ...] = (6, 8, 10)

__all__ = [
    "DEFAULT_SELECTIVE_KS",
    "Graph",
    "build_regional_random_graph",
    "compute_workload_network_load_index",
    "propagate_from_source",
    "run_network_simulation",
    "simulate_policies_dataframe",
    "run_simulate",
]


@dataclass(frozen=True)
class Graph:
    """Undirected weighted graph as symmetric adjacency lists."""

    n: int
    adj: list[list[tuple[int, float]]]


def build_regional_random_graph(
    n_nodes: int,
    edge_probability: float,
    n_regions: int,
    *,
    rng: np.random.Generator,
    intra_latency_mu: float = -2.0,
    intra_latency_sigma: float = 0.35,
    inter_latency_mu: float = 1.5,
    inter_latency_sigma: float = 0.45,
) -> Graph:
    """Erdős–Rényi graph with per-node region and lognormal latencies (seconds).

    Intra-region edges use a *lower* lognormal (default ``mu=-2``, sub-100 ms
    typical). Inter-region edges use a *higher* lognormal (``mu=1.5``, tens to
    hundreds of ms typical). Latencies are strictly positive.
    """
    if n_nodes < 1:
        raise ValueError(f"n_nodes must be >= 1, got {n_nodes}")
    if not 0.0 <= edge_probability <= 1.0:
        raise ValueError("edge_probability must be in [0, 1]")
    if n_regions < 1:
        raise ValueError(f"n_regions must be >= 1, got {n_regions}")

    regions = rng.integers(0, n_regions, size=n_nodes, dtype=np.int32)
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n_nodes)]

    def draw_latency(intra: bool) -> float:
        mu = intra_latency_mu if intra else inter_latency_mu
        sigma = intra_latency_sigma if intra else inter_latency_sigma
        return float(max(1e-9, rng.lognormal(mean=mu, sigma=sigma)))

    for i in range(n_nodes):
        for j in range(i + 1, n_nodes):
            if rng.random() >= edge_probability:
                continue
            intra = int(regions[i]) == int(regions[j])
            w = draw_latency(intra)
            adj[i].append((j, w))
            adj[j].append((i, w))

    return Graph(n=n_nodes, adj=adj)


def propagate_from_source(
    graph: Graph,
    source: int,
    policy: PolicyName,
    *,
    selective_k: int,
    send_spacing_s: float = 0.0,
    network_load_index: float = 0.0,
    queueing_load_scale_s: float = 0.0,
    queueing_fanout_exponent: float = 1.0,
) -> tuple[np.ndarray, int]:
    """Run propagation; return arrival times (inf = never) and total send count.

    Each node forwards **once**, immediately upon its first receipt. The priority
    queue orders nodes by first-arrival time (same best-first structure as
    Dijkstra). ``selective`` forwards to **at least** ``min(selective_k, degree(u))``
    neighbors: the ``selective_k`` lowest-latency incident edges when
    ``degree(u) >= selective_k``, otherwise **all** neighbors (cannot exceed degree).

    Latency compounds **link delay** with optional **load-aware queueing** and
    **serialization**:

    - ``network_load_index`` (from ledger ``tx_count`` / burst weighting) scales
      queueing: higher offered load ⇒ longer waits before the first outbound send.
    - ``queueing_load_scale_s * load * len(targets)**exp`` models **contention**:
      **flood** uses all neighbors so the queue term is large; **selective** uses
      fewer targets so congestion stays lower under the same load.
    - ``send_spacing_s`` adds per-send spacing after that queue, so many parallel
      sends (flood) also stretch the serialization pipeline more than selective.
    """
    if not 0 <= source < graph.n:
        raise ValueError(f"source must be in [0, {graph.n}), got {source}")
    if policy == "selective" and selective_k < 1:
        raise ValueError(f"selective_k must be >= 1 for selective policy, got {selective_k}")
    if send_spacing_s < 0.0:
        raise ValueError(f"send_spacing_s must be >= 0, got {send_spacing_s}")
    if queueing_load_scale_s < 0.0:
        raise ValueError(f"queueing_load_scale_s must be >= 0, got {queueing_load_scale_s}")
    if queueing_fanout_exponent < 0.0:
        raise ValueError(
            f"queueing_fanout_exponent must be >= 0, got {queueing_fanout_exponent}"
        )

    load = (
        max(0.0, float(network_load_index))
        if math.isfinite(float(network_load_index))
        else 0.0
    )

    n = graph.n
    dist = np.full(n, np.inf, dtype=np.float64)
    dist[source] = 0.0
    pq: list[tuple[float, int]] = [(0.0, source)]
    num_sends = 0

    while pq:
        t_u, u = heapq.heappop(pq)
        if t_u > dist[u] + 1e-15:
            continue

        neighbors = graph.adj[u]
        if not neighbors:
            continue

        if policy == "flood":
            targets = sorted(neighbors, key=lambda e: e[1])

        elif policy == "adaptive_weighted":
            k = min(selective_k, len(neighbors))

            # HELM-informed peer scoring:
            # latency_weight = 1.0
            # load/congestion_weight = 0.5
            # instability_weight = 2.0
            #
            # Neighbor tuple is assumed to be:
            # (neighbor_node, propagation_delay_or_latency)
            #
            # Because XBOSM does not yet expose full peer telemetry here,
            # we start with latency as the main observable and deterministic
            # proxy terms for load and instability.
            max_latency = max((edge[1] for edge in neighbors), default=1.0)

            def adaptive_score(edge):
                neighbor, latency = edge

                normalized_latency = latency / max(max_latency, 0.001)

                # Lightweight deterministic proxies for now.
                # These prevent the score from being purely latency-only
                # while keeping the simulation reproducible.
                normalized_load = ((neighbor * 17) % 100) / 100
                normalized_instability = ((neighbor * 31) % 100) / 100

                return (
                    1.0 * normalized_latency
                    + 0.5 * normalized_load
                    + 2.0 * normalized_instability
                )

            latency_quota = max(1, int(k * 0.75))
            weighted_quota = k - latency_quota

            latency_targets = sorted(neighbors, key=lambda e: e[1])[:latency_quota]
            selected = list(latency_targets)
            selected_nodes = {edge[0] for edge in selected}

            weighted_candidates = [
                edge
                for edge in sorted(neighbors, key=adaptive_score)
                if edge[0] not in selected_nodes
            ]

            selected.extend(weighted_candidates[:weighted_quota])
            targets = selected

        else:
            k = min(selective_k, len(neighbors))
            targets = sorted(neighbors, key=lambda e: e[1])[:k]

        m = len(targets)
        queue_delay = (
            queueing_load_scale_s
            * load
            * (m**queueing_fanout_exponent)
        )

        for j, (v, lat) in enumerate(targets):
            num_sends += 1
            slot = j * send_spacing_s if send_spacing_s > 0.0 else 0.0
            depart = t_u + queue_delay + slot
            cand = depart + lat
            if cand + 1e-15 < dist[v]:
                dist[v] = cand
                heapq.heappush(pq, (cand, v))

    return dist, num_sends


def _t95_seconds(arrivals: np.ndarray) -> float:
    finite = arrivals[np.isfinite(arrivals)]
    if finite.size == 0:
        return float("nan")
    return float(np.percentile(finite, 95))


def _reachability(arrivals: np.ndarray) -> float:
    n = arrivals.size
    if n == 0:
        return float("nan")
    reached = int(np.sum(np.isfinite(arrivals) & (arrivals < np.inf)))
    return float(reached) / float(n)


def _bandwidth_bps(num_sends: int, message_size_bytes: int, span_s: float) -> float:
    if span_s <= 0.0 or not math.isfinite(span_s):
        return float("nan")
    bits = float(num_sends * max(0, message_size_bytes) * 8)
    return bits / span_s


def compute_workload_network_load_index(
    workload_df: pd.DataFrame,
    *,
    tx_count_ref: float,
    burst_tx_weight: float = 1.75,
) -> float:
    """Map ledger workload to a congestion scalar in ``[0, +inf)``.

    Burst ledgers are up-weighted so high ``tx_count`` under **BURST** raises the
    index more than the same count in NORMAL. Used to scale ``send_spacing_s``.
    """
    if tx_count_ref <= 0.0:
        raise ValueError(f"tx_count_ref must be > 0, got {tx_count_ref}")
    if burst_tx_weight < 1.0:
        raise ValueError(f"burst_tx_weight must be >= 1, got {burst_tx_weight}")
    if "tx_count" not in workload_df.columns or "regime" not in workload_df.columns:
        raise ValueError("workload_df must include 'tx_count' and 'regime' columns")

    tx = workload_df["tx_count"].to_numpy(dtype=float)
    regime = workload_df["regime"].astype(str).to_numpy()
    w = np.where(regime == "BURST", float(burst_tx_weight), 1.0)
    weighted_mean = float((tx * w).sum() / w.sum())
    return max(0.0, weighted_mean / float(tx_count_ref))


def simulate_policies_dataframe(
    graph: Graph,
    *,
    source: int = 0,
    selective_ks: Sequence[int] = DEFAULT_SELECTIVE_KS,
    message_size_bytes: int = 1200,
    send_spacing_s: float = 0.0,
    network_load_index: float | None = None,
    queueing_load_scale_s: float = 0.0,
    queueing_fanout_exponent: float = 1.0,
) -> pd.DataFrame:
    """One row per policy variant: ``flood``, ``adaptive_weighted_k8``, then ``selective_k{K}`` for each *K*."""
    rows: list[dict[str, Any]] = []
    load_meta = (
        float(network_load_index)
        if network_load_index is not None
        else (
            float("nan")
            if (send_spacing_s > 0.0 or queueing_load_scale_s > 0.0)
            else 0.0
        )
    )
    load_prop = (
        float(network_load_index)
        if network_load_index is not None and math.isfinite(float(network_load_index))
        else 0.0
    )

    dist, sends = propagate_from_source(
        graph,
        source,
        "flood",
        selective_k=1,
        send_spacing_s=send_spacing_s,
        network_load_index=load_prop,
        queueing_load_scale_s=queueing_load_scale_s,
        queueing_fanout_exponent=queueing_fanout_exponent,
    )
    finite = dist[np.isfinite(dist) & (dist < np.inf)]
    span = float(finite.max()) if finite.size else float("nan")
    rows.append(
        {
            "policy": "flood",
            "t95_propagation_s": _t95_seconds(dist),
            "num_sends": int(sends),
            "estimated_bandwidth_bps": _bandwidth_bps(
                sends, message_size_bytes, span
            ),
            "reachability": _reachability(dist),
            "network_load_index": load_meta,
            "send_spacing_s": float(send_spacing_s),
            "queueing_load_scale_s": float(queueing_load_scale_s),
            "queueing_fanout_exponent": float(queueing_fanout_exponent),
        }
    )

    adaptive_k = 8
    dist, sends = propagate_from_source(
        graph,
        source,
        "adaptive_weighted",
        selective_k=adaptive_k,
        send_spacing_s=send_spacing_s,
        network_load_index=load_prop,
        queueing_load_scale_s=queueing_load_scale_s,
        queueing_fanout_exponent=queueing_fanout_exponent,
    )
    finite = dist[np.isfinite(dist) & (dist < np.inf)]
    span = float(finite.max()) if finite.size else float("nan")
    rows.append(
        {
            "policy": f"adaptive_weighted_k{adaptive_k}",
            "t95_propagation_s": _t95_seconds(dist),
            "num_sends": int(sends),
            "estimated_bandwidth_bps": _bandwidth_bps(
                sends, message_size_bytes, span
            ),
            "reachability": _reachability(dist),
            "network_load_index": load_meta,
            "send_spacing_s": float(send_spacing_s),
            "queueing_load_scale_s": float(queueing_load_scale_s),
            "queueing_fanout_exponent": float(queueing_fanout_exponent),
        }
    )

    for k in selective_ks:
        kk = int(k)
        if kk < 1:
            raise ValueError(f"selective K must be >= 1, got {kk}")
        label = f"selective_k{kk}"
        dist, sends = propagate_from_source(
            graph,
            source,
            "selective",
            selective_k=kk,
            send_spacing_s=send_spacing_s,
            network_load_index=load_prop,
            queueing_load_scale_s=queueing_load_scale_s,
            queueing_fanout_exponent=queueing_fanout_exponent,
        )
        finite = dist[np.isfinite(dist) & (dist < np.inf)]
        span = float(finite.max()) if finite.size else float("nan")
        rows.append(
            {
                "policy": label,
                "t95_propagation_s": _t95_seconds(dist),
                "num_sends": int(sends),
                "estimated_bandwidth_bps": _bandwidth_bps(
                    sends, message_size_bytes, span
                ),
                "reachability": _reachability(dist),
                "network_load_index": load_meta,
                "send_spacing_s": float(send_spacing_s),
                "queueing_load_scale_s": float(queueing_load_scale_s),
                "queueing_fanout_exponent": float(queueing_fanout_exponent),
            }
        )

    return pd.DataFrame(rows)


def _parse_selective_ks(cfg: dict[str, Any]) -> tuple[int, ...]:
    """Resolve ``selective_ks`` list, or legacy ``selective_k`` scalar, or default."""
    if "selective_ks" in cfg:
        raw = cfg["selective_ks"]
        if not isinstance(raw, (list, tuple)):
            raise ValueError("selective_ks must be a list of integers")
        return tuple(int(x) for x in raw)
    if "selective_k" in cfg:
        return (int(cfg["selective_k"]),)
    return DEFAULT_SELECTIVE_KS


def run_network_simulation(cfg: dict[str, Any]) -> pd.DataFrame:
    """Build one graph from parameters and evaluate flood, adaptive_weighted (K=8), and selective for each *K*.

    **Config keys** (``network`` block of YAML, or a flat dict)::

        n_nodes, edge_probability, n_regions,
        intra_latency_mu, intra_latency_sigma,
        inter_latency_mu, inter_latency_sigma,
        source (default 0),
        selective_ks (list of ints, default ``[6, 8, 10]``),
        selective_k (legacy: single int → one selective row ``selective_k{k}``),
        message_size_bytes (default 1200), seed (optional),
        send_spacing_s (default 0): seconds between consecutive sends from one node,
        network_load_index (optional): tx_count-derived load; drives queueing when
            ``queueing_load_scale_s`` > 0,
        queueing_load_scale_s (default 0): ``load * fanout**exp`` queue before sends,
        queueing_fanout_exponent (default 1.0): exponent on outbound fan-out for queueing
    """
    seed = cfg.get("seed")
    rng = np.random.default_rng(seed)

    graph = build_regional_random_graph(
        int(cfg["n_nodes"]),
        float(cfg["edge_probability"]),
        int(cfg["n_regions"]),
        rng=rng,
        intra_latency_mu=float(cfg.get("intra_latency_mu", -2.0)),
        intra_latency_sigma=float(cfg.get("intra_latency_sigma", 0.35)),
        inter_latency_mu=float(cfg.get("inter_latency_mu", 1.5)),
        inter_latency_sigma=float(cfg.get("inter_latency_sigma", 0.45)),
    )

    spacing = float(cfg.get("send_spacing_s", 0.0))
    load_idx = cfg.get("network_load_index")
    load_arg = float(load_idx) if load_idx is not None else None
    q_scale = float(cfg.get("queueing_load_scale_s", 0.0))
    q_exp = float(cfg.get("queueing_fanout_exponent", 1.0))

    return simulate_policies_dataframe(
        graph,
        source=int(cfg.get("source", 0)),
        selective_ks=_parse_selective_ks(cfg),
        message_size_bytes=int(cfg.get("message_size_bytes", 1200)),
        send_spacing_s=spacing,
        network_load_index=load_arg,
        queueing_load_scale_s=q_scale,
        queueing_fanout_exponent=q_exp,
    )


def run_simulate(config_path: str) -> Path:
    """YAML entrypoint: legacy tick stream **or** network simulation + JSONL summary."""
    cfg = load_yaml(config_path)
    out = Path(cfg["output_jsonl"])
    source = str(cfg.get("source_name", "xmrs-simulator"))

    if isinstance(cfg.get("network"), dict):
        df = run_network_simulation(cfg["network"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        for _, row in df.iterrows():
            ev = TelemetryEvent(
                ts=utc_now_iso(),
                source=source,
                kind="xmrs.network_propagation",
                payload={
                    "policy": row["policy"],
                    "t95_propagation_s": float(row["t95_propagation_s"]),
                    "num_sends": int(row["num_sends"]),
                    "estimated_bandwidth_bps": float(row["estimated_bandwidth_bps"]),
                    "reachability": float(row["reachability"]),
                    "network": {k: cfg["network"].get(k) for k in ("n_nodes", "n_regions", "edge_probability", "seed")},
                },
            )
            append_jsonl(out, ev.to_json_dict())
        csv_out = cfg.get("network_metrics_csv")
        if csv_out:
            df.to_csv(Path(csv_out), index=False)
        return out

    # Legacy: synthetic load ticks
    steps = int(cfg.get("steps", 5))
    base_load = float(cfg.get("base_load", 0.35))
    drift = float(cfg.get("load_drift_per_step", 0.05))

    if out.exists():
        out.unlink()

    load = base_load
    for i in range(steps):
        ev = TelemetryEvent(
            ts=utc_now_iso(),
            source=source,
            kind="xmrs.tick",
            payload={"step": i, "load": round(load, 4), "scenario": cfg.get("scenario", "default")},
        )
        append_jsonl(out, ev.to_json_dict())
        load = min(0.99, max(0.01, load + drift))

    return out
