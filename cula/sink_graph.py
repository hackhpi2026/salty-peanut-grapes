"""
Build a NetworkX directed graph from a :class:`~cula.models.Sink`.

The graph merges:

- Registry structure: organisations, sites, ownership, capture site, primary material.
- Material flow: materials ↔ containers (filled).
- LCA: databases → activities; activities ↔ events where referenced.
- Lifecycle: :attr:`~cula.models.Sink.eventGraph` — edges follow **forward** supply-chain
  direction (predecessor event → successor event via ``relation="supply_chain"``).
  API back-links are reversed so chronology reads left-to-right toward the root event,
  which is linked to the sink node with ``relation="to_sink"``.

Nodes are string keys ``"{kind}:{id}"`` with attributes ``label`` (display name) and
``kind`` (``sink``, ``org``, ``site``, ``material``, ``container``, ``event``,
``lca_db``, ``lca_activity``). Edges carry ``relation`` (purpose of the link).
"""

from __future__ import annotations

from uuid import UUID

import networkx as nx

from cula.models import Event, MaterialContainer, Sink


def sink_title(sink: Sink) -> str:
    loc = sink.location
    if loc and loc.city:
        return f"Sink — {loc.city}"
    if sink.id:
        return f"Sink — {str(sink.id)[:8]}…"
    return "Sink"


def _container_label(container: MaterialContainer) -> str:
    names: list[str] = []
    for amount in container.content:
        m = amount.material
        if m and m.name:
            names.append(m.name)
    if names:
        text = ", ".join(names)
        return text if len(text) <= 72 else text[:69] + "…"
    return f"Container {str(container.id)[:8]}…"


def _event_label(event: Event) -> str:
    return event.type.value.replace("_", " ").title()


def _ensure_site(G: nx.DiGraph, site_id: UUID, sites: dict[UUID, str]) -> str:
    key = f"site:{site_id}"
    if key not in G:
        label = sites.get(site_id, f"Site {str(site_id)[:8]}…")
        G.add_node(key, label=label, kind="site")
    return key


def build_entity_graph(sink: Sink) -> nx.DiGraph:
    """
    Construct a :class:`networkx.DiGraph` for *sink*.

    Requires ``sink.id`` to be set. Raises :class:`ValueError` otherwise.
    """
    G = nx.DiGraph()

    if not sink.id:
        raise ValueError("Sink has no id")

    sink_key = f"sink:{sink.id}"
    G.add_node(sink_key, label=sink_title(sink), kind="sink")

    site_labels: dict[UUID, str] = {}
    for s in sink.sites or []:
        site_labels[s.siteRef] = s.name

    for s in sink.sites or []:
        key = f"site:{s.siteRef}"
        G.add_node(key, label=s.name, kind="site")

    org_keys: dict[UUID, str] = {}
    for org in sink.organisations or []:
        key = f"org:{org.id}"
        org_keys[org.id] = key
        G.add_node(key, label=org.name, kind="org")

    for s in sink.sites or []:
        if s.organisationRef and s.organisationRef in org_keys:
            G.add_edge(org_keys[s.organisationRef], f"site:{s.siteRef}", relation="organisation")

    if sink.ownerOrganisationId and sink.ownerOrganisationId in org_keys:
        G.add_edge(org_keys[sink.ownerOrganisationId], sink_key, relation="owns_sink")

    if sink.carbonCaptureSiteId:
        sk = _ensure_site(G, sink.carbonCaptureSiteId, site_labels)
        G.add_edge(sk, sink_key, relation="capture_site")

    mat_keys: dict[UUID, str] = {}
    for m in sink.materials or []:
        key = f"material:{m.id}"
        mat_keys[m.id] = key
        G.add_node(key, label=m.name, kind="material")

    if sink.materialId and sink.materialId in mat_keys:
        G.add_edge(mat_keys[sink.materialId], sink_key, relation="sink_material")

    cont_keys: dict[UUID, str] = {}
    for c in sink.utilizedContainers or []:
        key = f"container:{c.id}"
        cont_keys[c.id] = key
        G.add_node(key, label=_container_label(c), kind="container")
        for amount in c.content:
            mid = amount.material.id if amount.material else None
            if mid and mid in mat_keys:
                G.add_edge(mat_keys[mid], key, relation="filled")

    for db in sink.lcaDatabases or []:
        key = f"lca_db:{db.id}"
        G.add_node(
            key,
            label=f"{db.type.value} v{db.version}",
            kind="lca_db",
        )

    for act in sink.lcaActivities or []:
        key = f"lca_activity:{act.id}"
        title = act.title
        label = title if len(title) <= 56 else title[:53] + "…"
        G.add_node(key, label=label, kind="lca_activity")
        db_key = f"lca_db:{act.lcaDatabaseId}"
        if db_key in G:
            G.add_edge(db_key, key, relation="activity")

    eg = sink.eventGraph
    if not eg or not eg.nodes:
        return G

    for _eid, info in eg.nodes.items():
        ev = info.event
        if ev is None:
            continue
        ek = f"event:{ev.eventRef}"
        G.add_node(ek, label=_event_label(ev), kind="event")

    for _eid, info in eg.nodes.items():
        ev = info.event
        if ev is None:
            continue
        ek = f"event:{ev.eventRef}"

        for link in info.links or []:
            pred = f"event:{link.eventRef}"
            if pred not in G:
                G.add_node(pred, label=f"Event {link.eventRef[:8]}…", kind="event")
            G.add_edge(pred, ek, relation="supply_chain")

        if ev.senderSiteId:
            sk = _ensure_site(G, ev.senderSiteId, site_labels)
            G.add_edge(sk, ek, relation="sender")
        if ev.receiverSiteId:
            sk = _ensure_site(G, ev.receiverSiteId, site_labels)
            G.add_edge(sk, ek, relation="receiver")
        if ev.siteId:
            sk = _ensure_site(G, ev.siteId, site_labels)
            G.add_edge(sk, ek, relation="site")

        for containers in (ev.payload, ev.input, ev.output):
            if not containers:
                continue
            for c in containers:
                ck = f"container:{c.id}"
                if ck not in G:
                    G.add_node(ck, label=_container_label(c), kind="container")
                G.add_edge(ek, ck, relation="container")

        for act in ev.lcaActivities or []:
            ak = f"lca_activity:{act.id}"
            if ak not in G:
                label = act.title if len(act.title) <= 56 else act.title[:53] + "…"
                G.add_node(ak, label=label, kind="lca_activity")
            G.add_edge(ak, ek, relation="lca")

    if eg.root:
        rk = f"event:{eg.root}"
        if rk in G:
            G.add_edge(rk, sink_key, relation="to_sink")

    return G


def graph_summary(G: nx.DiGraph) -> dict[str, int | dict[str, int]]:
    """Count nodes by ``kind`` and return edge count."""
    by_kind: dict[str, int] = {}
    for _n, data in G.nodes(data=True):
        k = str(data.get("kind", "unknown"))
        by_kind[k] = by_kind.get(k, 0) + 1
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "nodes_by_kind": dict(sorted(by_kind.items())),
    }
