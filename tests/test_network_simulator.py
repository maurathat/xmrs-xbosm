from __future__ import annotations

import numpy as np
import pandas as pd

from xmrs_xbosm.xmrs.simulator import (
    DEFAULT_SELECTIVE_KS,
    Graph,
    build_regional_random_graph,
    compute_workload_network_load_index,
    propagate_from_source,
    run_network_simulation,
    simulate_policies_dataframe,
)


def test_line_graph_flood_reaches_all() -> None:
    # 0 --1 -- 2, unit latency
    g = Graph(
        n=3,
        adj=[
            [(1, 1.0)],
            [(0, 1.0), (2, 1.0)],
            [(1, 1.0)],
        ],
    )
    dist, sends = propagate_from_source(g, 0, "flood", selective_k=1)
    np.testing.assert_allclose(dist, [0.0, 1.0, 2.0])
    assert sends == 4  # 0->1, 1->0+1->2 but 1->0 counted...0 sends to 1 (1), 1 sends to 0 and 2 (2), 2 sends to 1 (1) = 4


def test_selective_star_prefers_low_latency() -> None:
    # source 0 connected to 1 (cheap) and 2 (expensive); K=1 should only use1
    g = Graph(
        n=3,
        adj=[
            [(1, 0.01), (2, 100.0)],
            [(0, 0.01)],
            [(0, 100.0)],
        ],
    )
    dist, _ = propagate_from_source(g, 0, "selective", selective_k=1)
    assert np.isfinite(dist[1])
    assert not np.isfinite(dist[2]) or dist[2] == np.inf


def test_dataframe_columns_and_run_network() -> None:
    rng = np.random.default_rng(0)
    g = build_regional_random_graph(
        80,
        0.12,
        4,
        rng=rng,
        intra_latency_mu=-3.0,
        intra_latency_sigma=0.2,
        inter_latency_mu=0.5,
        inter_latency_sigma=0.3,
    )
    df = simulate_policies_dataframe(
        g, source=0, selective_ks=[3], message_size_bytes=512
    )
    required = [
        "policy",
        "t95_propagation_s",
        "num_sends",
        "estimated_bandwidth_bps",
        "reachability",
        "network_load_index",
        "send_spacing_s",
        "queueing_load_scale_s",
        "queueing_fanout_exponent",
    ]
    assert list(df.columns) == required
    assert len(df) == 3
    assert set(df["policy"]) == {"flood", "adaptive_weighted_k8", "selective_k3"}

    df2 = run_network_simulation(
        {
            "n_nodes": 60,
            "edge_probability": 0.15,
            "n_regions": 3,
            "seed": 1,
            "selective_k": 2,
            "message_size_bytes": 256,
        }
    )
    assert isinstance(df2, pd.DataFrame)
    assert len(df2) == 3
    assert list(df2["policy"]) == ["flood", "adaptive_weighted_k8", "selective_k2"]

    df3 = run_network_simulation(
        {
            "n_nodes": 40,
            "edge_probability": 0.2,
            "n_regions": 2,
            "seed": 0,
            "selective_ks": [6, 8, 10],
        }
    )
    assert len(df3) == 5
    assert list(df3["policy"]) == [
        "flood",
        "adaptive_weighted_k8",
        "selective_k6",
        "selective_k8",
        "selective_k10",
    ]


def test_burst_weighted_load_index_exceeds_plain_mean() -> None:
    df = pd.DataFrame(
        {
            "tx_count": [10.0, 10.0, 10.0, 40.0],
            "regime": ["NORMAL", "NORMAL", "NORMAL", "BURST"],
        }
    )
    plain = df["tx_count"].mean() / 10.0
    weighted = compute_workload_network_load_index(
        df, tx_count_ref=10.0, burst_tx_weight=2.0
    )
    assert weighted > plain


def test_queueing_fanout_raises_flood_t95_above_selective() -> None:
    n = 12
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for j in range(1, n):
        adj[0].append((j, 1.0))
        adj[j].append((0, 1.0))
    g = Graph(n=n, adj=adj)
    df = simulate_policies_dataframe(
        g,
        source=0,
        selective_ks=[3],
        send_spacing_s=0.0,
        network_load_index=2.0,
        queueing_load_scale_s=0.02,
        queueing_fanout_exponent=1.0,
    )
    flood_t95 = float(df.loc[df["policy"] == "flood", "t95_propagation_s"].iloc[0])
    sel_t95 = float(df.loc[df["policy"] == "selective_k3", "t95_propagation_s"].iloc[0])
    assert flood_t95 > sel_t95


def test_serialization_spacing_raises_flood_t95_above_selective() -> None:
    # Hub 0 with many equal edges: flood schedules all sends; selective takes K first.
    n = 12
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for j in range(1, n):
        adj[0].append((j, 1.0))
        adj[j].append((0, 1.0))
    g = Graph(n=n, adj=adj)
    spacing = 0.05
    df = simulate_policies_dataframe(
        g,
        source=0,
        selective_ks=[3],
        send_spacing_s=spacing,
        network_load_index=1.5,
    )
    flood_t95 = float(df.loc[df["policy"] == "flood", "t95_propagation_s"].iloc[0])
    sel_t95 = float(df.loc[df["policy"] == "selective_k3", "t95_propagation_s"].iloc[0])
    assert flood_t95 > sel_t95


def test_run_network_uses_default_selective_ks_when_omitted() -> None:
    df = run_network_simulation(
        {
            "n_nodes": 30,
            "edge_probability": 0.25,
            "n_regions": 2,
            "seed": 0,
        }
    )
    expected = ["flood", "adaptive_weighted_k8"] + [f"selective_k{k}" for k in DEFAULT_SELECTIVE_KS]
    assert list(df["policy"]) == expected
