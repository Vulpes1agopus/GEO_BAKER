#!/usr/bin/env python3
"""
Build a global airport dataset (IATA/ICAO) and plot China airports.

Data source:
- OpenFlights airports.dat (airport metadata)
- OpenFlights routes.dat (route edges used as a traffic estimate)

Outputs:
- airports/global_airports_iata_icao.csv
- airports/global_airports_iata_icao.jsonl.gz
- airports/china_airports_iata_icao.csv
- images/china_airports_route_estimate.png
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
from pathlib import Path
from typing import Dict, List, Set

import matplotlib.pyplot as plt
import requests
import urllib3


AIRPORTS_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
ROUTES_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build global IATA/ICAO airport dataset and plot China airports")
    parser.add_argument("--out-dir", type=str, default="airports", help="Directory for airport data outputs")
    parser.add_argument("--img-path", type=str, default="images/china_airports_route_estimate.png", help="Output image path")
    parser.add_argument("--global-img-path", type=str, default="images/global_airports_route_estimate.png", help="Global output image path")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP request timeout seconds")
    parser.add_argument("--top-labels", type=int, default=20, help="Label top-N China airports by route_count")
    parser.add_argument(
        "--metric",
        type=str,
        default="route_count",
        choices=["route_count", "outbound_routes", "inbound_routes", "unique_destinations", "unique_airlines"],
        help="Metric used for symbol size/color in the China plot",
    )
    parser.add_argument(
        "--active-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep only active airports (default: true, defined as route_count > 0)",
    )
    parser.add_argument("--allow-insecure-ssl", action="store_true", help="Allow verify=False fallback when SSL cert validation fails")
    return parser.parse_args()


def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def fetch_text(url: str, timeout: int, allow_insecure_ssl: bool) -> str:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError:
        if not allow_insecure_ssl:
            raise
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, timeout=timeout, verify=False)
        resp.raise_for_status()
        return resp.text


def read_airports_csv(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    reader = csv.reader(io.StringIO(text))
    for r in reader:
        if len(r) < 14:
            continue

        airport_id = _safe_int(r[0], -1)
        name = r[1].strip()
        city = r[2].strip()
        country = r[3].strip()
        iata = r[4].strip()
        icao = r[5].strip()
        lat = _safe_float(r[6], 0.0)
        lon = _safe_float(r[7], 0.0)

        if airport_id < 0:
            continue
        if iata == "\\N":
            iata = ""
        if icao == "\\N":
            icao = ""
        if not iata and not icao:
            continue

        rows.append(
            {
                "airport_id": airport_id,
                "name": name,
                "city": city,
                "country": country,
                "iata": iata,
                "icao": icao,
                "lat": lat,
                "lon": lon,
            }
        )
    return rows


def compute_route_metrics(text: str) -> Dict[int, Dict[str, object]]:
    metrics: Dict[int, Dict[str, object]] = {}

    def get_bucket(airport_id: int) -> Dict[str, object]:
        if airport_id not in metrics:
            metrics[airport_id] = {
                "route_count": 0,
                "outbound_routes": 0,
                "inbound_routes": 0,
                "unique_destinations_set": set(),
                "unique_airlines_set": set(),
            }
        return metrics[airport_id]

    reader = csv.reader(io.StringIO(text))
    for r in reader:
        if len(r) < 9:
            continue
        airline = (r[0] or "").strip()
        src_code = (r[2] or "").strip()
        src_id = _safe_int(r[3], -1)
        dst_code = (r[4] or "").strip()
        dst_id = _safe_int(r[5], -1)

        if src_id >= 0:
            m = get_bucket(src_id)
            m["route_count"] = int(m["route_count"]) + 1
            m["outbound_routes"] = int(m["outbound_routes"]) + 1
            if dst_code and dst_code != "\\N":
                cast_set = m["unique_destinations_set"]
                assert isinstance(cast_set, set)
                cast_set.add(dst_code)
            if airline and airline != "\\N":
                cast_air = m["unique_airlines_set"]
                assert isinstance(cast_air, set)
                cast_air.add(airline)

        if dst_id >= 0:
            m = get_bucket(dst_id)
            m["route_count"] = int(m["route_count"]) + 1
            m["inbound_routes"] = int(m["inbound_routes"]) + 1
            if src_code and src_code != "\\N":
                cast_set = m["unique_destinations_set"]
                assert isinstance(cast_set, set)
                cast_set.add(src_code)
            if airline and airline != "\\N":
                cast_air = m["unique_airlines_set"]
                assert isinstance(cast_air, set)
                cast_air.add(airline)

    finalized: Dict[int, Dict[str, object]] = {}
    for airport_id, m in metrics.items():
        dests = m["unique_destinations_set"]
        airlines = m["unique_airlines_set"]
        assert isinstance(dests, set)
        assert isinstance(airlines, set)
        finalized[airport_id] = {
            "route_count": int(m["route_count"]),
            "outbound_routes": int(m["outbound_routes"]),
            "inbound_routes": int(m["inbound_routes"]),
            "unique_destinations": len(dests),
            "unique_airlines": len(airlines),
        }
    return finalized


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "airport_id",
                "name",
                "city",
                "country",
                "iata",
                "icao",
                "lat",
                "lon",
                "route_count",
                "outbound_routes",
                "inbound_routes",
                "unique_destinations",
                "unique_airlines",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["airport_id"],
                    r["name"],
                    r["city"],
                    r["country"],
                    r["iata"],
                    r["icao"],
                    f"{float(r['lat']):.6f}",
                    f"{float(r['lon']):.6f}",
                    r["route_count"],
                    r["outbound_routes"],
                    r["inbound_routes"],
                    r["unique_destinations"],
                    r["unique_airlines"],
                ]
            )


def write_jsonl_gz(path: Path, rows: List[Dict[str, object]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def plot_china_airports(rows: List[Dict[str, object]], out_path: Path, top_labels: int, metric: str) -> None:
    # Broad China extent for quick visual inspection.
    xmin, xmax = 73.0, 135.0
    ymin, ymax = 17.0, 54.0

    china_rows = [r for r in rows if r["country"] == "China" and xmin <= float(r["lon"]) <= xmax and ymin <= float(r["lat"]) <= ymax]
    if not china_rows:
        raise RuntimeError("No China airports found in dataset")

    xs = [float(r["lon"]) for r in china_rows]
    ys = [float(r["lat"]) for r in china_rows]
    sizes = [12.0 + min(180.0, (float(r[metric]) ** 0.6) * 2.5) for r in china_rows]
    colors = [float(r[metric]) for r in china_rows]

    fig, ax = plt.subplots(figsize=(11, 8))
    sc = ax.scatter(xs, ys, s=sizes, c=colors, cmap="viridis", alpha=0.85, edgecolors="black", linewidths=0.3)
    cbar = fig.colorbar(sc, ax=ax, shrink=0.82)
    cbar.set_label(f"{metric} (route-derived estimate)")

    top = sorted(china_rows, key=lambda r: int(r[metric]), reverse=True)[: max(0, top_labels)]
    for r in top:
        name = str(r["name"]).strip()
        code = str(r["iata"]).strip() or str(r["icao"]).strip()
        label = f"{name} ({code})" if code else name
        ax.text(float(r["lon"]), float(r["lat"]), label, fontsize=7, ha="left", va="bottom")

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(f"China Airports (IATA/ICAO) by {metric}")
    ax.grid(alpha=0.25, linewidth=0.5)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_global_airports(rows: List[Dict[str, object]], out_path: Path, top_labels: int, metric: str) -> None:
    if not rows:
        raise RuntimeError("No airports to plot")

    xs = [float(r["lon"]) for r in rows]
    ys = [float(r["lat"]) for r in rows]
    sizes = [6.0 + min(100.0, (float(r[metric]) ** 0.55) * 1.8) for r in rows]
    colors = [float(r[metric]) for r in rows]

    fig, ax = plt.subplots(figsize=(14, 7))
    sc = ax.scatter(xs, ys, s=sizes, c=colors, cmap="viridis", alpha=0.75, edgecolors="none")
    cbar = fig.colorbar(sc, ax=ax, shrink=0.82)
    cbar.set_label(f"{metric} (route-derived estimate)")

    top = sorted(rows, key=lambda r: int(r[metric]), reverse=True)[: max(0, top_labels)]
    for r in top:
        code = str(r["iata"]).strip() or str(r["icao"]).strip()
        label = code if code else str(r["name"]).strip()
        ax.text(float(r["lon"]), float(r["lat"]), label, fontsize=6, ha="left", va="bottom")

    ax.set_xlim(-180, 180)
    ax.set_ylim(-60, 85)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(f"Global Active Airports (IATA/ICAO) by {metric}")
    ax.grid(alpha=0.22, linewidth=0.45)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Downloading OpenFlights datasets...")
    airports_text = fetch_text(AIRPORTS_URL, args.timeout, args.allow_insecure_ssl)
    routes_text = fetch_text(ROUTES_URL, args.timeout, args.allow_insecure_ssl)

    airports = read_airports_csv(airports_text)
    route_metrics = compute_route_metrics(routes_text)

    merged: List[Dict[str, object]] = []
    for r in airports:
        rid = int(r["airport_id"])
        merged.append(
            {
                **r,
                "route_count": int(route_metrics.get(rid, {}).get("route_count", 0)),
                "outbound_routes": int(route_metrics.get(rid, {}).get("outbound_routes", 0)),
                "inbound_routes": int(route_metrics.get(rid, {}).get("inbound_routes", 0)),
                "unique_destinations": int(route_metrics.get(rid, {}).get("unique_destinations", 0)),
                "unique_airlines": int(route_metrics.get(rid, {}).get("unique_airlines", 0)),
            }
        )

    if args.active_only:
        merged = [r for r in merged if int(r["route_count"]) > 0]

    merged.sort(key=lambda x: int(x["route_count"]), reverse=True)
    china = [r for r in merged if r["country"] == "China"]

    global_csv = out_dir / "global_airports_iata_icao.csv"
    global_jsonl = out_dir / "global_airports_iata_icao.jsonl.gz"
    china_csv = out_dir / "china_airports_iata_icao.csv"

    write_csv(global_csv, merged)
    write_jsonl_gz(global_jsonl, merged)
    write_csv(china_csv, china)

    img_path = Path(args.img_path)
    global_img_path = Path(args.global_img_path)
    plot_global_airports(merged, global_img_path, args.top_labels, args.metric)
    plot_china_airports(merged, img_path, args.top_labels, args.metric)

    print(f"Global airports: {len(merged)}")
    print(f"China airports: {len(china)}")
    print(f"Saved CSV: {global_csv}")
    print(f"Saved JSONL.GZ: {global_jsonl}")
    print(f"Saved China CSV: {china_csv}")
    print(f"Saved global image: {global_img_path}")
    print(f"Saved image: {img_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
