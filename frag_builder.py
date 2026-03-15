"""
frag_builder.py
Constructs a FHIR Resource Access Graph (FRAG) from a transaction log.

FRAG G = (P, R, E, λ, τ, S) where:
  P = set of processes (EHR, PHARMACY, LAB)
  R = set of FHIR resources
  E = directed access edges (process → resource)
  λ = edge label: READ or WRITE
  τ = timestamp of each access event
  S = synchronization constraint set (ETag-enforced pairs)
"""

import json
import networkx as nx
from pathlib import Path


def build_frag(log_data):
    """
    Build a FRAG from a parsed log dictionary.

    Returns:
        G      : networkx.DiGraph with nodes and labeled edges
        events : flat list of access events (for baseline scanner)
        S      : synchronization constraint set as set of (process, resource_id) pairs
    """
    G = nx.MultiDiGraph()
    events = log_data["events"]
    S = set()  # synchronization constraints: (p_i, p_j) tuples

    for ev in events:
        proc  = ev["process"]
        rid   = ev["resource_id"]
        rtype = ev["resource_type"]
        op    = ev["operation"]
        ts    = ev["timestamp_ms"]
        vid   = ev["version_id"]
        etag  = ev["etag_used"]

        # Add nodes if not present
        if not G.has_node(proc):
            G.add_node(proc, node_type="process")
        if not G.has_node(rid):
            G.add_node(rid, node_type="resource", resource_type=rtype)

        # Add directed edge: process → resource
        G.add_edge(
            proc, rid,
            event_id   = ev["event_id"],
            operation  = op,
            timestamp  = ts,
            version_id = vid,
            etag_used  = etag,
            is_race    = ev["is_race"],
            race_class = ev["race_class"],
        )

        # If ETag was used on a WRITE, record synchronization constraint.
        # ETag-enforced writes create an ordering constraint — they cannot
        # race with a preceding read that had the same version_id.
        if etag and op == "WRITE":
            S.add((proc, rid))

    return G, events, S


def get_edges_for_resource(G, resource_id):
    """Return all edges (process, resource_id, edge_data) for a given resource."""
    return [
        (u, v, d)
        for u, v, d in G.edges(data=True)
        if v == resource_id
    ]


def get_write_edges(G, resource_id):
    """Return only WRITE edges for a resource."""
    return [
        (u, v, d)
        for u, v, d in get_edges_for_resource(G, resource_id)
        if d["operation"] == "WRITE"
    ]


def get_read_edges(G, resource_id):
    """Return only READ edges for a resource."""
    return [
        (u, v, d)
        for u, v, d in get_edges_for_resource(G, resource_id)
        if d["operation"] == "READ"
    ]


def load_log(fpath):
    with open(fpath) as f:
        return json.load(f)


if __name__ == "__main__":
    # Quick smoke test
    import sys
    if len(sys.argv) > 1:
        log = load_log(sys.argv[1])
        G, events, S = build_frag(log)
        processes = [n for n, d in G.nodes(data=True) if d.get("node_type") == "process"]
        resources = [n for n, d in G.nodes(data=True) if d.get("node_type") == "resource"]
        print(f"Log: {log['log_id']}")
        print(f"  Processes : {processes}")
        print(f"  Resources : {len(resources)}")
        print(f"  Edges     : {G.number_of_edges()}")
        print(f"  S (sync)  : {len(S)} constraints")
        print(f"  GT races  : {log['n_races']}")
