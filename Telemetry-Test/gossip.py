"""
XBOSM policy simulation: gossip propagation on a peer graph.

For each forwarding policy we run a Monte Carlo gossip simulation on a random
peer graph and measure:
  - reachability  : fraction of nodes that receive a message
  - relays_per_msg: total SEND events (the bandwidth proxy)

Policies:
  flood        : every informed node forwards to ALL its peers
  selective_kN : every informed node forwards to N randomly chosen peers

--- send-side suppression (the dedup model) ---------------------------------
Every node forwards a given message exactly once: receive-side duplicate
dropping (don't re-forward what you've already relayed) is assumed in ALL
modes -- that's standard gossip and is NOT what distinguishes the modes.

What distinguishes the modes is SEND-side suppression -- whether a node
declines to spend bandwidth on a transmission:

  "none"   : forward to the full policy set every time.
             Dedup-free UPPER BOUND on bandwidth.

  "sender" : never relay back to the peer(s) you received the message from.
             REALISTIC -- every real gossip protocol does at least this.
             Note it does NOT suppress sends to peers that got the message
             from someone else, because the sender has no way to know.

  "aware"  : never relay to a peer already informed as of this round.
             OPTIMISTIC FLOOR -- assumes good peer-state knowledge (what a
             mechanism like XRPL's squelching approximates). Still counts
             simultaneous same-round sends to a commonly-uninformed peer,
             so it's a round-granularity model, not a perfect oracle.

Reachability is identical across modes: suppression only ever skips a send to
an ALREADY-informed peer, so it can never prevent a new node being reached.
"""

import numpy as np


def build_peer_graph(n_nodes=200, degree=24, seed=0):
    """Undirected random graph, ~target degree per node. XRPL nodes
    typically hold on the order of 10-30 peers."""
    rng = np.random.default_rng(seed)
    adj = [set() for _ in range(n_nodes)]
    for i in range(n_nodes):
        attempts = 0
        while len(adj[i]) < degree and attempts < degree * 20:
            j = int(rng.integers(0, n_nodes))
            attempts += 1
            if j != i:
                adj[i].add(j)
                adj[j].add(i)
    return [np.array(sorted(a), dtype=np.int64) for a in adj]


def _propagate(adj, k, rng, mode="none"):
    """One gossip propagation from a random origin under suppression `mode`.
    Returns (reachability, relays). relays = send events = bandwidth proxy.

    `informed` is only mutated AFTER each synchronous round, so reading
    informed[v] inside a round reflects round-start state -- which is exactly
    the knowledge an "aware" node would have.
    """
    n = len(adj)
    origin = int(rng.integers(0, n))
    informed = np.zeros(n, dtype=bool)
    informed[origin] = True

    track_sender = (mode == "sender")
    received_from = {origin: set()} if track_sender else None

    frontier = [origin]
    relays = 0

    while frontier:
        new_senders = {}          # v -> set(senders this round)
        for u in frontier:
            peers = adj[u]
            if k is None or k >= len(peers):
                fwd = peers
            else:
                fwd = peers[rng.choice(len(peers), size=k, replace=False)]

            srcs = received_from.get(u) if track_sender else None
            for v in fwd:
                v = int(v)
                if track_sender and srcs and v in srcs:
                    continue                       # don't relay back to sender
                if mode == "aware" and informed[v]:
                    continue                       # peer already had it
                relays += 1                        # bandwidth spent on this send
                if not informed[v]:
                    new_senders.setdefault(v, set()).add(u)

        frontier = []
        for v, srcs in new_senders.items():
            if not informed[v]:
                informed[v] = True
                if track_sender:
                    received_from[v] = srcs
                frontier.append(v)

    return informed.sum() / n, relays


def policy_stats(adj, policies, trials=300, seed=1, mode="none"):
    """policies: dict name -> k (None = flood). Runs `trials` propagations
    per policy under the given send-side suppression `mode`."""
    rng = np.random.default_rng(seed)
    out = {}
    for name, k in policies.items():
        reach = np.empty(trials)
        relays = np.empty(trials)
        for t in range(trials):
            r, m = _propagate(adj, k, rng, mode=mode)
            reach[t] = r
            relays[t] = m
        out[name] = {
            "reachability": float(reach.mean()),
            "reach_min": float(reach.min()),
            "relays_per_msg": float(relays.mean()),
        }
    return out
