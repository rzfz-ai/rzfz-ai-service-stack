#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Generates the OpenSearch Dashboards saved objects (NDJSON) of the Wazuh module.

Run `python3 generate.py [OUT_DIR] [--internal OUT_DIR]` to rewrite the files;
the unit tests fail when the committed NDJSON differs from this output.
Object ids are stable (rzfz-...), so importing again overwrites in place.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

IP_ALERTS, IP_ARCHIVES = "rzfz-ip-alerts", "rzfz-ip-archives"
IDX_REF = "kibanaSavedObjectMeta.searchSourceJSON.index"


def _j(o) -> str:
    return json.dumps(o, separators=(",", ":"), sort_keys=True)


def index_pattern(oid, title):
    return {"id": oid, "type": "index-pattern", "attributes": {"title": title, "timeFieldName": "timestamp"},
            "references": [], "migrationVersion": {"index-pattern": "7.6.0"}}


def _source(query, ref):
    src = {"query": {"language": "kuery", "query": query}, "filter": []}
    kind, rid = ref
    if kind == "index-pattern":
        src["indexRefName"] = IDX_REF
        return src, {}, [{"name": IDX_REF, "type": "index-pattern", "id": rid}]
    return src, {"savedSearchRefName": "search_0"}, [{"name": "search_0", "type": "search", "id": rid}]


def search(oid, title, ip, query, columns, desc=""):
    src = {"query": {"language": "kuery", "query": query}, "filter": [], "indexRefName": IDX_REF}
    return {"id": oid, "type": "search", "migrationVersion": {"search": "7.9.3"},
            "attributes": {"title": title, "description": desc, "hits": 0, "columns": columns,
                           "sort": [["timestamp", "desc"]], "version": 1,
                           "kibanaSavedObjectMeta": {"searchSourceJSON": _j(src)}},
            "references": [{"name": IDX_REF, "type": "index-pattern", "id": ip}]}


def _agg(i, typ, schema, params=None):
    return {"id": str(i), "enabled": True, "type": typ, "schema": schema, "params": params or {}}


COUNT = _agg(1, "count", "metric")


def date_hist(i=2):
    return _agg(i, "date_histogram", "segment", {"field": "timestamp", "useNormalizedOpenSearchInterval": True,
                                                  "scaleMetricValues": False, "interval": "auto",
                                                  "drop_partials": False, "min_doc_count": 1, "extended_bounds": {}})


def terms(i, field, schema, size=10, order_by="1"):
    return _agg(i, "terms", schema, {"field": field, "orderBy": order_by, "order": "desc", "size": size,
                                     "otherBucket": False, "otherBucketLabel": "Other",
                                     "missingBucket": False, "missingBucketLabel": "Missing"})


def _bar_params(kind):
    horizontal = kind == "horizontal_bar"
    return {"type": kind, "grid": {"categoryLines": False},
            "categoryAxes": [{"id": "CategoryAxis-1", "type": "category", "position": "left" if horizontal else "bottom",
                              "show": True, "style": {}, "scale": {"type": "linear"},
                              "labels": {"show": True, "filter": True, "truncate": 100}, "title": {}}],
            "valueAxes": [{"id": "ValueAxis-1", "name": "ValueAxis-1", "type": "value",
                           "position": "bottom" if horizontal else "left", "show": True, "style": {},
                           "scale": {"type": "linear", "mode": "normal"},
                           "labels": {"show": True, "rotate": 0, "filter": True, "truncate": 100},
                           "title": {"text": "Count"}}],
            "seriesParams": [{"show": True, "type": "histogram", "mode": "stacked",
                              "data": {"label": "Count", "id": "1"}, "valueAxis": "ValueAxis-1",
                              "drawLinesBetweenPoints": True, "showCircles": True}],
            "addTooltip": True, "addLegend": True, "legendPosition": "right", "times": [],
            "addTimeMarker": False, "labels": {"show": False}}


PIE = {"type": "pie", "addTooltip": True, "addLegend": True, "legendPosition": "right", "isDonut": True,
       "labels": {"show": False, "values": True, "last_level": True, "truncate": 100}}
METRIC = {"addTooltip": True, "addLegend": False, "type": "metric",
          "metric": {"percentageMode": False, "useRanges": False, "colorSchema": "Green to Red",
                     "metricColorMode": "None", "colorsRange": [{"from": 0, "to": 10000}],
                     "labels": {"show": True}, "invertColors": False,
                     "style": {"bgFill": "#000", "bgColor": False, "labelColor": False, "subText": "", "fontSize": 40}}}
TABLE = {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False,
         "sort": {"columnIndex": None, "direction": None}, "showTotal": False, "totalFunc": "sum", "percentageCol": ""}


def vis(oid, title, typ, params, aggs, ref, query="", desc=""):
    src, extra, refs = _source(query, ref)
    state = {"title": title, "type": typ, "params": params, "aggs": aggs}
    attrs = {"title": title, "visState": _j(state), "uiStateJSON": "{}", "description": desc, "version": 1,
             "kibanaSavedObjectMeta": {"searchSourceJSON": _j(src)}}
    attrs.update(extra)
    return {"id": oid, "type": "visualization", "migrationVersion": {"visualization": "7.10.0"},
            "attributes": attrs, "references": refs}


def timeline(oid, title, ref, query, split=None, size=8, desc=""):
    aggs = [COUNT, date_hist()] + ([terms(3, split, "group", size)] if split else [])
    return vis(oid, title, "histogram", _bar_params("histogram"), aggs, ref, query, desc)


def top(oid, title, ref, query, field, size=10, desc=""):
    return vis(oid, title, "horizontal_bar", _bar_params("horizontal_bar"),
               [COUNT, terms(2, field, "segment", size)], ref, query, desc)


def pie(oid, title, ref, query, field, size=10, desc=""):
    return vis(oid, title, "pie", PIE, [COUNT, terms(2, field, "segment", size)], ref, query, desc)


def metric(oid, title, ref, query, desc=""):
    return vis(oid, title, "metric", METRIC, [COUNT], ref, query, desc)


def table(oid, title, ref, query, fields, metrics=None, size=50, desc=""):
    aggs = [COUNT] + (metrics or [])
    for n, f in enumerate(fields):
        aggs.append(terms(10 + n, f, "bucket", size, order_by="1"))
    return vis(oid, title, "table", TABLE, aggs, ref, query, desc)


def dashboard(oid, title, desc, panels):
    """panels: list of (object, x, y, w, h); a saved search is embedded as a search panel."""
    plist, refs = [], []
    for n, (obj, x, y, w, h) in enumerate(panels):
        plist.append({"version": "2.0.0", "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(n)},
                      "panelIndex": str(n), "embeddableConfig": {}, "panelRefName": f"panel_{n}"})
        refs.append({"name": f"panel_{n}", "type": obj["type"], "id": obj["id"]})
    src = {"query": {"language": "kuery", "query": ""}, "filter": []}
    return {"id": oid, "type": "dashboard", "migrationVersion": {"dashboard": "7.9.3"},
            "attributes": {"title": title, "description": desc, "hits": 0, "panelsJSON": _j(plist),
                           "optionsJSON": _j({"hidePanelTitles": False, "useMargins": True}),
                           "timeRestore": False, "version": 1,
                           "kibanaSavedObjectMeta": {"searchSourceJSON": _j(src)}},
            "references": refs}


A, R = ("index-pattern", IP_ALERTS), ("index-pattern", IP_ARCHIVES)
ALERT_COLS = ["agent.name", "rule.level", "rule.id", "rule.description"]

# ---- saved searches: one per dashboard theme ------------------------------------------------------
S_SEC = search("rzfz-search-security-high", "rzfz: alerts at level 10 and above", IP_ALERTS,
               "rule.level >= 10", ALERT_COLS)
S_UNIFI = search("rzfz-search-unifi-alerts", "rzfz: UniFi alerts", IP_ALERTS, "rule.groups:unifi",
                 ["rule.level", "rule.id", "data.srcip", "data.dstuser", "data.unifi.name"])
S_UNIFI_RAW = search("rzfz-search-unifi-archives", "rzfz: UniFi raw events (archives)", IP_ARCHIVES,
                     'decoder.name:"rzfz-unifi-cef"', ["location", "data.unifi.name", "data.srcip", "full_log"],
                     "Needs archives enabled on the manager.")
ACCESS_Q = ('rule.groups:(gitea or rzfz_authentik or rzfz_audit) or rule.id:(100401 or 100402 or 100406)')
S_ACCESS = search("rzfz-search-access", "rzfz: access (Authentik, Gitea, stack audit)", IP_ALERTS, ACCESS_Q,
                  ALERT_COLS)
S_FAILED = search("rzfz-search-failed-logins", "rzfz: failed logins", IP_ALERTS,
                  "rule.groups:(authentication_failed or authentication_failures)",
                  ["agent.name", "rule.level", "rule.id", "rule.description", "data.srcip"])
S_ADMIN = search("rzfz-search-admin-token-actions", "rzfz: admin and token actions", IP_ALERTS,
                 "rule.groups:(admin_action or rzfz_authentik_admin) or rule.id:(100655 or 100704 or 100718)",
                 ALERT_COLS)
PLAT_Q = "rule.groups:(rzfz_audit or rzfz_authentik or openbao) and rule.level >= 6"
S_PLAT = search("rzfz-search-platform-changes", "rzfz: platform changes", IP_ALERTS, PLAT_Q, ALERT_COLS)
S_BACKUP = search("rzfz-search-backups", "rzfz: backups restored or deleted", IP_ALERTS,
                  "rule.id:(100703 or 100707 or 100708)", ALERT_COLS)
S_AUDITD = search("rzfz-search-auditd", "rzfz: host audit key events", IP_ALERTS, "rule.groups:rzfz_auditd",
                  ["agent.name", "rule.level", "rule.id", "data.audit.key", "data.audit.exe", "data.audit.file.name"])
S_FIM = search("rzfz-search-fim", "rzfz: file integrity changes", IP_ALERTS, "rule.groups:syscheck",
               ["agent.name", "rule.level", "syscheck.event", "syscheck.path"])
S_SCA = search("rzfz-search-sca", "rzfz: SCA summaries", IP_ALERTS, "rule.groups:sca and data.sca.type:summary",
               ["agent.name", "data.sca.policy", "data.sca.score", "data.sca.passed", "data.sca.failed"])
SEARCHES = [S_SEC, S_UNIFI, S_UNIFI_RAW, S_ACCESS, S_FAILED, S_ADMIN, S_PLAT, S_BACKUP, S_AUDITD, S_FIM, S_SCA]


def _sref(s):
    return ("search", s["id"])


# ---- dashboards -----------------------------------------------------------------------------------
def build():
    ips = [index_pattern(IP_ALERTS, "wazuh-alerts-4.x-*"), index_pattern(IP_ARCHIVES, "wazuh-archives-4.x-*")]
    out = {"00-index-patterns": ips, "01-searches": SEARCHES}

    v = [timeline("rzfz-vis-sec-by-level", "Alerts over time by level", A, "", "rule.level", 15),
         top("rzfz-vis-sec-agents", "Top agents", A, "", "agent.name", 10),
         pie("rzfz-vis-sec-groups", "Top rule groups", A, "", "rule.groups", 12)]
    out["10-security-overview"] = v + [dashboard(
        "rzfz-dash-security", "rzfz: Security overview", "Alerts across all agents.",
        [(v[0], 0, 0, 48, 14), (v[1], 0, 14, 24, 14), (v[2], 24, 14, 24, 14), (S_SEC, 0, 28, 48, 16)])]

    v = [timeline("rzfz-vis-net-blocks", "Firewall blocks over time", A, "rule.groups:unifi and rule.groups:firewall_drop"),
         timeline("rzfz-vis-net-ips", "IPS alerts over time", A, "rule.groups:unifi and rule.groups:ids"),
         metric("rzfz-vis-net-admin-logins", "Admin login count", A, "rule.id:100301"),
         top("rzfz-vis-net-top-src", "Top blocked source IPs", A,
             "rule.groups:unifi and rule.groups:firewall_drop", "data.srcip", 10),
         table("rzfz-vis-net-admins", "Admin logins", A, "rule.id:100301", ["data.dstuser", "data.srcip"])]
    out["20-network-unifi"] = v + [dashboard(
        "rzfz-dash-network", "rzfz: Network (UniFi)", "UniFi firewall, IPS and admin events.",
        [(v[2], 0, 0, 12, 8), (v[0], 12, 0, 36, 14), (v[1], 0, 14, 24, 14), (v[3], 24, 14, 24, 14),
         (v[4], 0, 28, 24, 12), (S_UNIFI, 24, 28, 24, 12), (S_UNIFI_RAW, 0, 40, 48, 14)])]

    v = [timeline("rzfz-vis-acc-by-source", "Access events over time by rule", A, ACCESS_Q, "rule.id", 12),
         timeline("rzfz-vis-acc-failed", "Failed logins over time", A,
                  "rule.groups:(authentication_failed or authentication_failures)", "rule.description", 8),
         top("rzfz-vis-acc-failed-src", "Failed logins by source", A,
             "rule.groups:(authentication_failed or authentication_failures)", "data.srcip", 10),
         timeline("rzfz-vis-acc-admin", "Admin and token actions over time", A,
                  "rule.groups:(admin_action or rzfz_authentik_admin) or rule.id:(100655 or 100704 or 100718)",
                  "rule.id", 10)]
    out["30-access"] = v + [dashboard(
        "rzfz-dash-access", "rzfz: Access", "Authentik, Gitea and stack audit: logins, admin and token actions.",
        [(v[0], 0, 0, 48, 12), (v[1], 0, 12, 24, 14), (v[2], 24, 12, 24, 14), (v[3], 0, 26, 48, 12),
         (S_FAILED, 0, 38, 24, 14), (S_ADMIN, 24, 38, 24, 14)])]

    v = [timeline("rzfz-vis-plat-by-class", "Platform changes over time", A, PLAT_Q, "rule.groups", 8),
         top("rzfz-vis-plat-rules", "Changes by rule", A, PLAT_Q, "rule.description", 12),
         metric("rzfz-vis-plat-backups", "Backups restored or deleted", A, "rule.id:(100703 or 100707 or 100708)")]
    out["40-platform-changes"] = v + [dashboard(
        "rzfz-dash-platform", "rzfz: Platform changes",
        "Stack audit, Authentik, OpenBao and backup restore or delete.",
        [(v[2], 0, 0, 12, 8), (v[0], 12, 0, 36, 14), (v[1], 0, 14, 48, 14), (S_PLAT, 0, 28, 24, 14),
         (S_BACKUP, 24, 28, 24, 14)])]

    sca_avg = _agg(5, "avg", "metric", {"field": "data.sca.score"})
    seen_max = _agg(6, "max", "metric", {"field": "timestamp"})
    v = [timeline("rzfz-vis-host-auditd", "Host audit events by key", A, "rule.groups:rzfz_auditd",
                  "data.audit.key", 12),
         timeline("rzfz-vis-host-fim", "File integrity changes over time", A, "rule.groups:syscheck",
                  "syscheck.event", 5),
         top("rzfz-vis-host-fim-agents", "File integrity changes by agent", A, "rule.groups:syscheck",
             "agent.name", 10),
         vis("rzfz-vis-host-sca", "SCA score per agent", "table", TABLE,
             [COUNT, sca_avg, terms(10, "agent.name", "bucket", 50, order_by="5")], _sref(S_SCA)),
         vis("rzfz-vis-host-seen", "Last event seen per agent", "table", TABLE,
             [COUNT, seen_max, terms(10, "agent.name", "bucket", 50, order_by="6")], R,
             "", "Latest archived event per agent: a quiet agent shows an old time.")]
    out["50-hosts"] = v + [dashboard(
        "rzfz-dash-hosts", "rzfz: Hosts", "Audit keys, file integrity, configuration assessment and agent activity.",
        [(v[0], 0, 0, 48, 14), (v[1], 0, 14, 24, 14), (v[2], 24, 14, 24, 14), (v[3], 0, 28, 24, 14),
         (v[4], 24, 28, 24, 14), (S_AUDITD, 0, 42, 24, 14), (S_FIM, 24, 42, 24, 14), (S_SCA, 0, 56, 48, 12)])]
    return out


def build_internal():
    ips = [index_pattern(IP_ALERTS, "wazuh-alerts-4.x-*")]
    q = "rule.groups:warpgate"
    s = search("rzfz-search-warpgate", "rzfz: Warpgate events", IP_ALERTS, q,
               ["rule.level", "rule.id", "rule.description", "data.srcip", "data.dstuser"])
    v = [timeline("rzfz-vis-wg-by-rule", "Warpgate events over time by rule", A,
                  "rule.id >= 100601 and rule.id <= 100615", "rule.id", 15),
         top("rzfz-vis-wg-failed", "Failed logins by source", A, "rule.id:(100603 or 100605 or 100615)",
             "data.srcip", 10),
         metric("rzfz-vis-wg-breakglass", "Break-glass admin logins", A, "rule.id:100602"),
         metric("rzfz-vis-wg-denied", "Sessions denied", A, "rule.id:100607"),
         timeline("rzfz-vis-wg-admin", "Admin changes and tickets", A, "rule.id:(100611 or 100612 or 100613 or 100614)",
                  "rule.id", 5)]
    return {"00-warpgate": ips + [s] + v + [dashboard(
        "rzfz-dash-warpgate", "rzfz: Warpgate access", "Bastion logins, sessions and admin changes.",
        [(v[2], 0, 0, 12, 8), (v[3], 12, 0, 12, 8), (v[0], 0, 8, 48, 14), (v[1], 0, 22, 24, 14),
         (v[4], 24, 22, 24, 14), (s, 0, 36, 48, 14)])]}


def write(out_dir: Path, files: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, objs in files.items():
        (out_dir / f"{name}.ndjson").write_text("".join(_j(o) + "\n" for o in objs), encoding="utf-8")


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    args = sys.argv[1:]
    internal = None
    if "--internal" in args:
        i = args.index("--internal")
        internal = Path(args[i + 1])
        del args[i:i + 2]
    write(Path(args[0]) if args else here, build())
    if internal:
        write(internal, build_internal())
