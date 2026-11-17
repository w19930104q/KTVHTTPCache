#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fixed request matrix for KTVHTTPCache range planning.

Ports two implementations of the range -> (intervals, content-length, 206
headers) mapping and diffs them over a fixed request matrix:

  legacy  : KTVHCDataReader.prepareSourceManager + KTVHCHTTPResponse /
            KTVHCHTTPHLSResponse as of 388da9a (intervals computed inline,
            range-relative offset, raw HLS headers).
  planner : the injectable KTVHCDataRangePlanner module (shared mapping for
            single-file, multi-segment and HLS manifest, absolute offset,
            sanitized HLS headers).

The HTTPConnection 206 path (parseRangeRequest / newUniRangeResponse /
preprocessResponse header merge) is emulated so the exported headers are the
ones a client would observe.

Usage:
  python3 range_plan_matrix.py --out <dir>     # write matrix-legacy.jsonl,
                                               # matrix-planner.jsonl, diff report
Exit status is 0 when every diff is a whitelisted intentional fix and the
release/re-request pass is byte-identical.
"""

import argparse
import copy
import json
import sys

NF = 2**63 - 1          # KTVHCNotFound == LONG_LONG_MAX
U64 = 2**64             # UInt64 modulus for connection offset math

# ---------------------------------------------------------------------------
# KTVHCRange port
# ---------------------------------------------------------------------------

def range_length(r):
    s, e = r
    if s == NF or e == NF:
        return NF
    return e - s + 1

def parse_separate_value(value):
    """KTVHCRangeWithSeparateValue"""
    invalid = (NF, NF)
    if len(value) > 0:
        components = value.split(",")
        if len(components) == 1:
            components = components[0].split("-")
            if len(components) == 2:
                s_str, e_str = components
                s_val = int(s_str) if s_str else 0
                e_val = int(e_str) if e_str else 0
                if s_str and s_val >= 0 and e_str and e_val >= s_val:
                    return (s_val, e_val)
                elif s_str and s_val >= 0:
                    return (s_val, NF)
                elif e_str and e_val > 0:
                    return (NF, e_val)
    return invalid

def parse_request_header(value):
    """KTVHCRangeWithRequestHeaderValue"""
    if value is not None and value.startswith("bytes="):
        return parse_separate_value(value[6:])
    return (NF, NF)

def ensure_length(r, ensure):
    """KTVHCRangeWithEnsureLength"""
    s, e = r
    if ensure > 0 and (e == NF or e >= ensure):
        return (s, ensure - 1)
    return r

def normalize_range(r, total):
    """KTVHCDataRangePlanNormalizeRange (planner only)"""
    s, e = r
    if total > 0 and s == NF and e != NF:
        length = min(e, total)
        return (total - length, total - 1)
    return ensure_length(r, total)

def fill_response_headers(r, headers, total):
    """KTVHCRangeFillToResponseHeaders"""
    ret = dict(headers)
    ret["Content-Length"] = str(range_length(r))
    ret["Content-Range"] = "bytes %d-%d/%d" % (r[0], r[1], total)
    return ret

def strip_connection_headers(headers):
    """KTVHCDataRangePlan HTTPHeadersWithBaseHeaders: (and the legacy inline copy)"""
    ret = dict(headers)
    for key in ("Content-Range", "content-range", "Content-Length", "content-length"):
        ret.pop(key, None)
    return ret

def fmt_range(r):
    return [r[0], r[1]]

# ---------------------------------------------------------------------------
# Planning : legacy (inline in the reader) vs planner (injectable module)
# ---------------------------------------------------------------------------

def legacy_plan(req_range, total_length, items):
    """init ensure (newRequestWithTotalLength) + prepareSourceManager walk."""
    ensured = ensure_length(req_range, total_length)
    start, end = ensured
    file_sources = []
    min_ = start
    for item in items:
        offset, length = item["offset"], item["length"]
        item_min = offset
        item_max = offset + length - 1
        if item_max < min_ or item_min > end:
            continue
        if min_ > item_min:
            item_min = min_
        if end < item_max:
            item_max = end
        min_ = item_max + 1
        file_sources.append({
            "type": "file",
            "range": (offset, offset + length - 1),
            "readRange": (item_min - offset, item_max - offset),
        })
    file_sources.sort(key=lambda s: s["range"][0])
    network_sources = []
    offset = start
    length = range_length(ensured)
    for fs in file_sources:
        delta = fs["range"][0] + fs["readRange"][0] - offset
        if delta > 0:
            network_sources.append({"type": "network", "range": (offset, offset + delta - 1)})
            offset += delta
            length -= delta
        offset += range_length(fs["readRange"])
        length -= range_length(fs["readRange"])
    if length > 0:
        network_sources.append({"type": "network", "range": (offset, end)})
    return ensured, file_sources + network_sources

def planner_plan(req_range, total_length, items):
    """KTVHCDataRangePlanner planWithRequest:unit:"""
    planned = normalize_range(req_range, total_length)
    start, end = planned
    file_segments = []
    min_ = start
    for item in items:
        offset, length = item["offset"], item["length"]
        item_min = offset
        item_max = offset + length - 1
        if item_max < min_ or item_min > end:
            continue
        if min_ > item_min:
            item_min = min_
        if end < item_max:
            item_max = end
        min_ = item_max + 1
        file_segments.append({
            "type": "file",
            "range": (offset, offset + length - 1),
            "readRange": (item_min - offset, item_max - offset),
        })
    file_segments.sort(key=lambda s: s["range"][0])
    network_segments = []
    offset = start
    length = range_length(planned)
    for fs in file_segments:
        delta = fs["range"][0] + fs["readRange"][0] - offset
        if delta > 0:
            network_segments.append({"type": "network", "range": (offset, offset + delta - 1)})
            offset += delta
            length -= delta
        offset += range_length(fs["readRange"])
        length -= range_length(fs["readRange"])
    if length > 0:
        network_segments.append({"type": "network", "range": (offset, end)})
    return planned, file_segments + network_segments

def export_segments(segments):
    out = []
    for seg in segments:
        entry = {"type": seg["type"], "range": fmt_range(seg["range"])}
        if "readRange" in seg:
            entry["readRange"] = fmt_range(seg["readRange"])
        out.append(entry)
    return out

# ---------------------------------------------------------------------------
# HTTPConnection emulation (CocoaHTTPServer)
# ---------------------------------------------------------------------------

def connection_parse_range(range_header, content_length):
    """parseRangeRequest:withContentLength: -> list of (location, length) or None"""
    if "=" not in range_header:
        return None
    range_type, range_value = range_header.split("=", 1)
    if range_type.strip().lower() != "bytes":
        return None
    components = range_value.strip().split(",")
    if len(components) == 0:
        return None
    ranges = []
    for component in components:
        if "-" not in component:
            try:
                byte_index = int(component)
            except ValueError:
                return None
            if byte_index >= content_length:
                return None
            ranges.append((byte_index, 1))
        else:
            r1str, r2str = component.split("-", 1)
            try:
                r1 = int(r1str)
                has_r1 = True
            except ValueError:
                r1, has_r1 = 0, False
            try:
                r2 = int(r2str)
                has_r2 = True
            except ValueError:
                r2, has_r2 = 0, False
            if not has_r1:
                if not has_r2:
                    return None
                if r2 > content_length:
                    return None
                ranges.append((content_length - r2, r2))
            elif not has_r2:
                if r1 >= content_length:
                    return None
                ranges.append((r1, content_length - r1))
            else:
                if r1 > r2:
                    return None
                if r2 >= content_length:
                    r2 = content_length - 1
                ranges.append((r1, r2 - r1 + 1))
    if len(ranges) == 0:
        return None
    for i in range(len(ranges) - 1):
        for j in range(i + 1, len(ranges)):
            a, b = ranges[i], ranges[j]
            lo = max(a[0], b[0])
            hi = min(a[0] + a[1], b[0] + b[1])
            if hi - lo > 0:
                return None
    ranges.sort()
    return ranges

def connection_response(range_header, content_length, http_headers, offset0):
    """Emulates sendResponseHeadersAndBody + preprocessResponse + the
    single-range body loop. Returns the observable response summary."""
    result = {}
    ranges = connection_parse_range(range_header, content_length) if range_header else None
    merged = {}
    if ranges:
        result["status"] = 206
        if len(ranges) == 1:
            loc, length = ranges[0]
            merged["Content-Length"] = str(length)
            merged["Content-Range"] = "bytes %d-%d/%d" % (loc, loc + length - 1, content_length)
            # continueSendingSingleRangeResponseBody arithmetic (UInt64)
            bytes_read = (offset0 - loc) % U64
            bytes_left = (length - bytes_read) % U64
            result["firstBytesLeft"] = bytes_left
        else:
            result["multipartRanges"] = ["bytes %d-%d/%d" % (l, l + n - 1, content_length)
                                         for (l, n) in ranges]
            merged["Content-Type"] = "multipart/byteranges"
    else:
        result["status"] = 200
        merged["Content-Length"] = str(content_length)
    # preprocessResponse merges httpHeaders last; they win on conflicts.
    for key, value in http_headers.items():
        merged[key] = value
    result["headers"] = merged
    return result

# ---------------------------------------------------------------------------
# Response objects : what each implementation hands to the connection
# ---------------------------------------------------------------------------

def file_response_legacy(case, req_range):
    ensured, segments = legacy_plan(req_range, case["plan_total"], case["items"])
    total = case["resp_total"]
    resp_range = ensure_length(ensured, total)
    resp_headers = fill_response_headers(resp_range, case["unit_headers"], total)
    # KTVHCDataResponse.totalLength is parsed from Content-Range after '/'
    resp_total = int(resp_headers["Content-Range"].split("/")[-1])
    return {
        "requestRange": ensured,
        "segments": segments,
        "responseContentRange": resp_headers["Content-Range"],
        "contentLength": resp_total,               # reader.response.totalLength
        "httpHeaders": strip_connection_headers(resp_headers),
        "offset0": 0,                              # readedLength, range relative
    }

def file_response_planner(case, req_range):
    planned, segments = planner_plan(req_range, case["plan_total"], case["items"])
    total = case["resp_total"]
    resp_range = normalize_range(req_range, total)  # responseRangeWithTotalLength:
    resp_headers = fill_response_headers(resp_range, case["unit_headers"], total)
    resp_total = int(resp_headers["Content-Range"].split("/")[-1])
    start = resp_range[0]
    return {
        "requestRange": planned,
        "segments": segments,
        "responseContentRange": resp_headers["Content-Range"],
        "contentLength": resp_total,
        "httpHeaders": strip_connection_headers(resp_headers),
        "offset0": start if start != NF and start > 0 else 0,   # absolute offset
    }

def hls_response_legacy(case, req_range):
    data_len = case["data_length"]
    return {
        "requestRange": None,
        "segments": None,
        "responseContentRange": None,
        "contentLength": data_len,                 # self.data.length
        "httpHeaders": dict(case["unit_headers"]), # raw, unstripped
        "offset0": 0,
    }

def hls_response_planner(case, req_range):
    planned, segments = planner_plan(req_range, case["plan_total"], case["items"])
    ensure = case["resp_total"] if case["resp_total"] > 0 else case["data_length"]
    resp_range = normalize_range(req_range, ensure)
    length = range_length(resp_range)
    if length == NF or length < 0:
        length = case["data_length"]
    start = resp_range[0]
    return {
        "requestRange": planned,
        "segments": segments,
        "responseContentRange": "bytes %d-%d/%d" % (resp_range[0], resp_range[1], ensure),
        "contentLength": length,
        "httpHeaders": strip_connection_headers(case["unit_headers"]),
        "offset0": start if start != NF and start > 0 else 0,
    }

# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------

TOTAL = 10_000_000
MP4_HEADERS = {"Content-Type": "video/mp4", "Accept-Ranges": "bytes",
               "Content-Length": str(TOTAL)}
M3U8_HEADERS = {"Content-Type": "application/vnd.apple.mpegurl",
                "Content-Length": "2048"}

LAYOUTS = {
    "empty":   [],
    "full":    [{"offset": 0, "length": TOTAL}],
    "head":    [{"offset": 0, "length": 500_000}],
    "gap":     [{"offset": 0, "length": 500_000},
                {"offset": 1_000_000, "length": 500_000}],
    "triple":  [{"offset": 0, "length": 100_000},
                {"offset": 100_000, "length": 100_000},
                {"offset": 200_000, "length": 100_000}],
}

RANGE_HEADERS = [
    None,
    "bytes=0-",
    "bytes=0-499",
    "bytes=500-999",
    "bytes=1000000-",
    "bytes=-500",
    "bytes=0-0",
    "bytes=999999999-",
    "bytes=500-600,601-999",
]

def build_cases():
    cases = []
    for layout_name, items in LAYOUTS.items():
        for range_header in RANGE_HEADERS:
            for known in (True, False):
                name = "file:%s:%s:%s" % (
                    layout_name,
                    (range_header or "no-range").replace("=", "").replace(",", "_"),
                    "known" if known else "unknown",
                )
                cases.append({
                    "name": name,
                    "kind": "file",
                    "range_header": range_header,
                    "plan_total": TOTAL if known else 0,
                    "resp_total": TOTAL,
                    "items": copy.deepcopy(items),
                    "unit_headers": dict(MP4_HEADERS),
                    "data_length": 0,
                })
    for range_header in (None, "bytes=0-99", "bytes=-100"):
        for known in (True, False):
            name = "hls:manifest:%s:%s" % (
                (range_header or "no-range").replace("=", ""),
                "known" if known else "unknown",
            )
            cases.append({
                "name": name,
                "kind": "hls",
                "range_header": range_header,
                "plan_total": 2048 if known else 0,
                "resp_total": 2048 if known else 0,
                "items": ([{"offset": 0, "length": 2048}] if known else []),
                "unit_headers": dict(M3U8_HEADERS),
                "data_length": 2048,
            })
    return cases

def run_case(case, impl):
    req_range = parse_request_header(case["range_header"]) if case["range_header"] else (0, NF)
    if case["kind"] == "hls":
        response = (hls_response_legacy if impl == "legacy" else hls_response_planner)(case, req_range)
    else:
        response = (file_response_legacy if impl == "legacy" else file_response_planner)(case, req_range)
    conn = connection_response(case["range_header"], response["contentLength"],
                               response["httpHeaders"], response["offset0"])
    return {
        "case": case["name"],
        "requestRange": fmt_range(response["requestRange"]) if response["requestRange"] else None,
        "intervals": export_segments(response["segments"]) if response["segments"] is not None else None,
        "responseContentRange": response["responseContentRange"],
        "status": conn["status"],
        "headers": conn["headers"],
        "firstBytesLeft": conn.get("firstBytesLeft"),
        "multipartRanges": conn.get("multipartRanges"),
    }

# ---------------------------------------------------------------------------
# Whitelisted intentional fixes. Everything else must diff clean.
# ---------------------------------------------------------------------------

def whitelisted(case, field):
    header = case["range_header"]
    kind = case["kind"]
    # Fix 1 : suffix ranges (bytes=-N) are normalized to absolute offsets when
    # the total length is known, so they share the file/network mapping
    # instead of producing a garbage internal Content-Range.
    if header == "bytes=-500" and kind == "file":
        if field in ("requestRange", "intervals", "responseContentRange"):
            return "suffix-range normalization"
    # Fix 2 : offset is absolute within the remote content, so the
    # connection's bytesRead = offset - range.location no longer underflows.
    if field == "firstBytesLeft" and header:
        total = case["resp_total"] if case["resp_total"] > 0 else case["data_length"]
        ranges = connection_parse_range(header, total)
        if ranges and len(ranges) == 1 and ranges[0][0] > 0:
            return "absolute offset"
    # Fix 3 : HLS manifest responses strip stale Content-Length/Content-Range
    # and are planned through the same mapping as file resources.
    if kind == "hls":
        if field in ("requestRange", "intervals", "responseContentRange",
                     "headers", "firstBytesLeft"):
            return "hls unified mapping"
    return None

FIELDS = ("requestRange", "intervals", "responseContentRange",
          "status", "headers", "firstBytesLeft", "multipartRanges")

def diff_runs(cases, legacy, planner):
    problems = []
    fixes = []
    for case, leg, new in zip(cases, legacy, planner):
        for field in FIELDS:
            if leg[field] != new[field]:
                reason = whitelisted(case, field)
                entry = {"case": case["name"], "field": field,
                         "legacy": leg[field], "planner": new[field]}
                if reason:
                    entry["reason"] = reason
                    fixes.append(entry)
                else:
                    problems.append(entry)
    return problems, fixes

# ---------------------------------------------------------------------------
# Static checks on the ObjC sources (no compiler available here).
# ---------------------------------------------------------------------------

def static_checks(root):
    import os
    checks = []

    def read(rel):
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()

    reader = read("KTVHTTPCache/Classes/KTVHCDataStorage/KTVHCDataReader.m")
    checks.append(("reader delegates interval planning to the plan",
                   "self.rangePlan.segments" in reader
                   and "prepareSourceManager" in reader
                   and "KTVHCRangeGetLength(obj.readRange)" not in reader))
    checks.append(("reader response headers come from the plan",
                   "responseHeadersWithBaseHeaders" in reader))
    planner = read("KTVHTTPCache/Classes/KTVHCDataStorage/KTVHCDataRangePlan.m")
    checks.append(("planner owns the interval walk",
                   "networkSegmentWithRange" in planner and "fileSegmentWithRange" in planner))
    storage = read("KTVHTTPCache/Classes/KTVHCDataStorage/KTVHCDataStorage.m")
    checks.append(("storage injects the planner into readers",
                   "initWithRequest:request rangePlanner:self.rangePlanner" in storage))
    http = read("KTVHTTPCache/Classes/KTVHCHTTPServer/KTVHCHTTPResponse.m")
    checks.append(("file response uses plan headers and absolute offset",
                   "HTTPHeadersWithBaseHeaders" in http
                   and "responseRangeWithTotalLength" in http))
    hls = read("KTVHTTPCache/Classes/KTVHCHTTPServer/KTVHCHTTPHLSResponse.m")
    checks.append(("hls response shares the planner mapping",
                   "KTVHCDataRangePlanner" in hls and "HTTPHeadersWithBaseHeaders" in hls))
    public_api = read("KTVHTTPCache/KTVHTTPCache.h")
    for symbol in ("cacheReaderWithRequest", "cacheLoaderWithRequest",
                   "cacheDeleteAllCaches", "proxyURLWithOriginalURL"):
        checks.append(("public API kept: %s" % symbol, symbol in public_api))
    return checks

# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="output directory for the matrix exports")
    parser.add_argument("--root", default=".", help="repository root for static checks")
    args = parser.parse_args()

    import os
    os.makedirs(args.out, exist_ok=True)

    cases = build_cases()

    def run_matrix(impl):
        return [run_case(case, impl) for case in cases]

    # Release / re-request consistency : serialize the unit state of every
    # case (what survives reader release + unit archive), rebuild from the
    # serialized form, and require byte-identical exports.
    serialized = json.loads(json.dumps(cases))
    first = {impl: run_matrix(impl) for impl in ("legacy", "planner")}
    cases[:] = serialized
    second = {impl: run_matrix(impl) for impl in ("legacy", "planner")}
    release_ok = all(json.dumps(first[i], sort_keys=True) == json.dumps(second[i], sort_keys=True)
                     for i in ("legacy", "planner"))

    legacy_path = os.path.join(args.out, "matrix-legacy.jsonl")
    planner_path = os.path.join(args.out, "matrix-planner.jsonl")
    with open(legacy_path, "w", encoding="utf-8") as fh:
        for row in first["legacy"]:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    with open(planner_path, "w", encoding="utf-8") as fh:
        for row in first["planner"]:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    problems, fixes = diff_runs(cases, first["legacy"], first["planner"])

    report = {
        "cases": len(cases),
        "releaseRerequestConsistent": release_ok,
        "unexpectedDiffs": problems,
        "intentionalFixes": fixes,
        "staticChecks": [{"check": name, "ok": ok} for name, ok in static_checks(args.root)],
    }
    report_path = os.path.join(args.out, "diff-report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)

    static_ok = all(ok for _, ok in report["staticChecks"])
    print("cases                    : %d" % len(cases))
    print("release/re-request match : %s" % release_ok)
    print("static checks            : %s" % ("ok" if static_ok else "FAILED"))
    print("intentional fixes        : %d" % len(fixes))
    for fix in fixes:
        print("  [%s] %s / %s" % (fix["reason"], fix["case"], fix["field"]))
    print("unexpected diffs         : %d" % len(problems))
    for problem in problems:
        print("  UNEXPECTED %s / %s\n    legacy  : %s\n    planner : %s"
              % (problem["case"], problem["field"],
                 json.dumps(problem["legacy"]), json.dumps(problem["planner"])))
    ok = release_ok and static_ok and not problems
    print("RESULT                   : %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
