"""XMRS + XBoSM demo dashboard — workload (left) vs propagation metrics (right).

Run from the repository root::

    streamlit run app/dashboard.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
import pandas as pd
import streamlit as st

# --- import package without editable install ---
_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from xmrs_xbosm.xbosm.generator import REGIMES, generate_ledger_workload  # noqa: E402
from xmrs_xbosm.xmrs.simulator import run_network_simulation  # noqa: E402

_WORKLOAD_TEMPLATE = _REPO / "configs" / "workload_ledger.example.json"
_COMBINED_CSV = _REPO / "outputs" / "simulation" / "run_combined.csv"

_REGIME_COLORS = {
    "NORMAL": "#22c55e",
    "STRESSED": "#eab308",
    "BURST": "#ef4444",
}


def _load_workload_template() -> dict[str, Any]:
    if not _WORKLOAD_TEMPLATE.is_file():
        raise FileNotFoundError(f"Missing workload template: {_WORKLOAD_TEMPLATE}")
    return json.loads(_WORKLOAD_TEMPLATE.read_text(encoding="utf-8"))


def _renormalize_markov(markov: dict[str, dict[str, float]]) -> None:
    for r in REGIMES:
        row = markov[r]
        s = sum(float(row[c]) for c in REGIMES)
        for c in REGIMES:
            row[c] = float(row[c]) / s


def _apply_burst_intensity(cfg: dict[str, Any], burst_intensity: float) -> None:
    """burst_intensity:0 (calm) → 10 (aggressive burst traffic + more BURST time)."""
    t = max(0.0, min(10.0, float(burst_intensity))) / 10.0
    if t <= 0:
        return
    burst_ln = cfg["lognormal"]["BURST"]
    burst_ln["tx_count"]["mu"] = float(burst_ln["tx_count"]["mu"]) + t * 1.4
    burst_ln["tx_count"]["sigma"] = float(burst_ln["tx_count"]["sigma"]) + t * 0.12
    burst_ln["interval_s"]["mu"] = float(burst_ln["interval_s"]["mu"]) - t * 0.18
    burst_ln["interval_s"]["sigma"] = max(
        0.08, float(burst_ln["interval_s"]["sigma"]) - t * 0.04
    )

    markov = cfg["markov"]
    for r in REGIMES:
        if r == "BURST":
            continue
        row = markov[r]
        row["BURST"] = float(row["BURST"]) + t * 0.1
        row["NORMAL"] = float(row["NORMAL"]) - t * 0.05
        row["STRESSED"] = float(row["STRESSED"]) - t * 0.05
    for r in REGIMES:
        row = markov[r]
        for c in REGIMES:
            row[c] = max(0.0, float(row[c]))
    _renormalize_markov(markov)


def _build_workload_config(
    *,
    steps: int,
    seed: int,
    burst_intensity: float,
) -> dict[str, Any]:
    cfg = copy.deepcopy(_load_workload_template())
    cfg.pop("output_csv", None)
    cfg["steps"] = int(steps)
    cfg["seed"] = int(seed)
    cfg["start_ledger_index"] = int(cfg.get("start_ledger_index", 100_001))
    _apply_burst_intensity(cfg, burst_intensity)
    return cfg


def _style_axes(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, alpha=0.25, linestyle="-")


def _burst_stress_reference_df() -> pd.DataFrame:
    """Reference t95 (s) vs burst load multiplier for flood vs selective policies."""
    return pd.DataFrame(
        {
            "burst_load_multiplier": [1.0, 1.5, 2.0, 2.5, 3.0],
            "flood": [2.57, 2.95, 3.55, 4.35, 5.40],
            "selective_k8": [2.57, 2.72, 3.00, 3.28, 3.68],
            "selective_k6": [2.57, 2.68, 2.90, 3.15, 3.45],
        }
    )


def _fanout_k_from_policy(policy: str, approx_mean_degree: int) -> int:
    """Effective fan-out for plotting: flood ≈ ER expected degree; selective_kN → N."""
    if str(policy) == "flood":
        return max(1, int(approx_mean_degree))
    parts = str(policy).split("_k")
    if len(parts) >= 2:
        try:
            return max(1, int(parts[-1]))
        except ValueError:
            return 1
    return 1


@st.cache_data(show_spinner=False)
def _cached_workload(
    steps: int,
    seed: int,
    burst_intensity: float,
) -> pd.DataFrame:
    cfg = _build_workload_config(
        steps=steps, seed=seed, burst_intensity=burst_intensity
    )
    return generate_ledger_workload(cfg)


def _load_network_from_run_combined_csv() -> pd.DataFrame | None:
    """Network policy rows from ``outputs/simulation/run_combined.csv`` if it exists."""
    if not _COMBINED_CSV.is_file():
        return None
    df = pd.read_csv(_COMBINED_CSV)
    network_df = df[df["record_type"] == "network"]
    if network_df.empty:
        return None
    return network_df.reset_index(drop=True)


@st.cache_data(show_spinner=False)
def _cached_network(n_nodes: int, seed: int, selective_ks: tuple[int, ...]) -> pd.DataFrame:
    edge_p = float(np.clip(14.0 / max(n_nodes, 1), 0.05, 0.22))
    return run_network_simulation(
        {
            "n_nodes": int(n_nodes),
            "edge_probability": edge_p,
            "n_regions": max(3, min(8, n_nodes // 25)),
            "seed": int(seed),
            "source": 0,
            "selective_ks": list(selective_ks),
            "message_size_bytes": 1200,
        },
    )


def main() -> None:
    st.set_page_config(
        page_title="XMRS | XBoSM",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.2rem; max-width: 1280px; }
        h1 { font-weight: 600; letter-spacing: -0.02em; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("XRPL propagation lab")
    st.caption("Synthetic ledger workload (XBoSM) | gossip simulation (XMRS)")

    with st.sidebar:
        st.header("Controls")
        n_nodes = st.slider("Number of nodes", min_value=40, max_value=400, value=160, step=20)
        burst_intensity = st.slider(
            "Burst intensity",
            min_value=0.0,
            max_value=10.0,
            value=2.0,
            step=0.5,
            help="Higher → heavier BURST regime traffic and more time spent in BURST.",
        )
        ledger_steps = st.slider("Ledgers to simulate", 60, 400, 140, 20)
        seed = st.number_input("Random seed", min_value=0, value=42, step=1)
        selective_ks_choice = st.multiselect(
            "Selective fan-out K values (on-the-fly sim only)",
            options=[2, 3, 4, 6, 8, 10, 12, 16],
            default=[6, 8, 10],
            help="Each K runs as policy selective_k{K} alongside flood when no run_combined.csv is present.",
        )
        st.divider()
        st.caption(
            "Network charts use outputs/simulation/run_combined.csv when that file exists; otherwise an on-the-fly flood + selective run using the K list above."
        )

    edge_p = float(np.clip(14.0 / max(n_nodes, 1), 0.05, 0.22))

    df_w = _cached_workload(ledger_steps, int(seed), float(burst_intensity))
    df_n = _load_network_from_run_combined_csv()
    network_source = "csv"
    if df_n is None:
        ks = tuple(sorted(int(k) for k in selective_ks_choice)) if selective_ks_choice else (6, 8, 10)
        df_n = _cached_network(n_nodes, int(seed), ks)
        network_source = "simulated"

    left, right = st.columns([1.15, 1.0], gap="large")

    with left:
        st.subheader("Workload")
        st.caption("Transactions per closed ledger and Markov regime over time.")

        fig1, ax1 = plt.subplots(figsize=(7.2, 2.8), dpi=120)
        ax1.fill_between(
            df_w["ledger_index"],
            0,
            df_w["tx_count"],
            color="#3b82f6",
            alpha=0.25,
            linewidth=0,
        )
        ax1.plot(
            df_w["ledger_index"],
            df_w["tx_count"],
            color="#1d4ed8",
            linewidth=1.3,
        )
        ax1.set_xlabel("Ledger index")
        ax1.set_ylabel("Tx count")
        _style_axes(ax1)
        fig1.tight_layout()
        st.pyplot(fig1, clear_figure=True)
        plt.close(fig1)

        fig2, ax2 = plt.subplots(figsize=(7.2, 2.4), dpi=120)
        x0 = df_w["ledger_index"].to_numpy()
        step = float(x0[1] - x0[0]) if len(x0) > 1 else 1.0
        for i in range(len(df_w)):
            r = str(df_w["regime"].iloc[i])
            c = _REGIME_COLORS.get(r, "#64748b")
            x_s = float(x0[i])
            x_e = float(x0[i + 1]) if i + 1 < len(x0) else x_s + step
            ax2.axvspan(x_s, x_e, color=c, alpha=0.85, linewidth=0)
        ax2.set_xlim(x0.min(), x0.max())
        ax2.set_ylim(0, 1)
        ax2.set_yticks([])
        ax2.set_xlabel("Ledger index")
        ax2.set_title("Regime", fontsize=10, pad=6)
        _style_axes(ax2)
        ax2.legend(
            handles=[Patch(color=_REGIME_COLORS[r], label=r) for r in REGIMES],
            loc="upper center",
            ncol=3,
            frameon=False,
            bbox_to_anchor=(0.5, 1.18),
            fontsize=8,
        )
        fig2.tight_layout()
        st.pyplot(fig2, clear_figure=True)
        plt.close(fig2)

    with right:
        st.subheader("Simulation")
        if network_source == "csv":
            st.caption(f"Network metrics from `{_COMBINED_CSV.relative_to(_REPO)}`")
        else:
            ks_disp = ",".join(str(k) for k in sorted(selective_ks_choice)) if selective_ks_choice else "6,8,10"
            st.caption(
                f"On-the-fly regional graph | p={edge_p:.3f} | regions={max(3, min(8, n_nodes // 25))} | selective K={ks_disp}"
            )

        network_df = df_n.copy()
        st.caption("t95 propagation latency (seconds)")
        st.bar_chart(network_df.set_index("policy")["t95_propagation_s"])
        st.caption("Estimated bandwidth (bits per second, during propagation span)")
        st.bar_chart(network_df.set_index("policy")["estimated_bandwidth_bps"])

        approx_deg = max(1, int(round(edge_p * max(n_nodes - 1, 1))))
        curve_df = network_df.copy()
        curve_df["fanout_k"] = curve_df["policy"].apply(
            lambda pol: _fanout_k_from_policy(str(pol), approx_deg)
        )
        curve_df = curve_df.sort_values("fanout_k", ascending=True)

        st.caption("Estimated bandwidth vs effective fan-out K (flood uses ~p*(n-1))")
        fig3, ax3 = plt.subplots(figsize=(7.0, 2.9), dpi=120)
        ax3.plot(
            curve_df["fanout_k"],
            curve_df["estimated_bandwidth_bps"],
            marker="o",
            color="#7c3aed",
            linewidth=1.4,
            markersize=6,
        )
        ax3.set_xlabel("Fan-out K")
        ax3.set_ylabel("Estimated bandwidth (bps)")
        ax3.set_title("Efficiency vs fan-out K", fontsize=11, pad=8)
        _style_axes(ax3)
        fig3.tight_layout()
        st.pyplot(fig3, clear_figure=True)
        plt.close(fig3)

        burst_df = _burst_stress_reference_df()
        st.caption("Burst stress test (reference t95 sweep vs burst load multiplier)")
        fig4, ax4 = plt.subplots(figsize=(7.0, 2.9), dpi=120)
        burst_styles = {
            "flood": {"color": "#dc2626", "linewidth": 1.5},
            "selective_k8": {"color": "#2563eb", "linewidth": 1.5},
            "selective_k6": {"color": "#059669", "linewidth": 1.5},
        }
        for col in ("flood", "selective_k8", "selective_k6"):
            style = burst_styles[col]
            ax4.plot(
                burst_df["burst_load_multiplier"],
                burst_df[col],
                marker="o",
                label=col,
                markersize=5,
                **style,
            )
        ax4.set_xlabel("Burst load multiplier")
        ax4.set_ylabel("t95 propagation (s)")
        ax4.set_title("Burst stress test", fontsize=11, pad=8)
        ax4.legend(frameon=False, loc="upper left", fontsize=8)
        _style_axes(ax4)
        fig4.tight_layout()
        st.pyplot(fig4, clear_figure=True)
        plt.close(fig4)

        with st.expander("Raw metrics"):
            st.dataframe(df_n, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
