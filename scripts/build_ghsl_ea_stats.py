#!/usr/bin/env python3
"""Create EA-level GHSL built-up surface statistics from the public JRC AWS bucket.

GHSL values are square metres of built-up surface per grid cell. The script
reads only the Gambia raster window from the public COGs and sums pixel values
inside each Census EA polygon. No Earth Engine account is required.
"""
import csv
import json
import math
import os
import re
import sys
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.config import Config
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import Window
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

OWNER = "silkysingz94-oss"
REPO = "Gambia-Urban-Expansion---Census-Intelligence"
BASE = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/main"
GEOJSON_URL = f"{BASE}/Census_EAs_Geo.json"
CENSUS_URL = f"{BASE}/Census_2013_2023.csv"
OUT = Path("Gambia_GHSL_BuiltUp_LandUse_Efficiency.csv")
YEARS = list(range(1975, 2031, 5))
BUCKET = "jrc-ghsl"
PREFIX = "ghs-built-s/r2023a/"

def download(url, path):
    import requests
    response = requests.get(url, timeout=180)
    response.raise_for_status()
    path.write_bytes(response.content)

def norm(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").strip().upper())

def first_value(obj, names):
    for name in names:
        value = obj.get(name)
        if value not in (None, ""):
            try:
                if isinstance(value, str):
                    value = value.replace(",", "").strip()
                return float(value)
            except (TypeError, ValueError):
                continue
    return None

def first_text(obj, names):
    for name in names:
        value = obj.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""

def load_census(path):
    lookup = {}
    # The source CSV may be Windows-1252/ANSI rather than UTF-8. Try UTF-8
    # first, then fall back to CP1252 so accented settlement names do not
    # prevent the numeric EA attributes from being processed.
    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = raw.decode("cp1252")
    import io
    reader = csv.DictReader(io.StringIO(decoded, newline=""))
        for row in reader:
            key = first_text(row, ["EA_code", "ea_code", "feacode", "FEACODE", "EA_CODE", "fsacode"])
            if key:
                lookup[norm(key)] = row
    return lookup

def list_raster_keys():
    client = boto3.client("s3", region_name="eu-central-1",
                          config=Config(signature_version=UNSIGNED))
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.lower().endswith((".tif", ".tiff")):
                keys.append(key)
    return keys

def find_key(keys, year):
    # Prefer geographic 3-arcsecond (~100 m) COGs; fall back to the
    # 100 m World Mollweide COG if that geographic variant is not present.
    token = f"GHS_BUILT_S_E{year}_GLOBE_R2023A_4326_3SS"
    matches = [k for k in keys if token.lower() in k.lower()]
    if not matches:
        token = f"GHS_BUILT_S_E{year}_GLOBE_R2023A_54009_100"
        matches = [k for k in keys if token.lower() in k.lower()]
    if not matches:
        sample = "\n".join(keys[:30])
        raise RuntimeError(f"Could not locate GHSL raster for {year}. S3 TIFF keys found: {len(keys)}. Sample:\n{sample}")
    return matches[0]

def open_s3_raster(key):
    # GDAL's /vsis3/ driver performs HTTP range reads against the public bucket.
    os.environ["AWS_NO_SIGN_REQUEST"] = "YES"
    os.environ["AWS_REGION"] = "eu-central-1"
    os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = "EMPTY_DIR"
    os.environ["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"] = ".tif,.tiff"
    return rasterio.open(f"/vsis3/{BUCKET}/{key}")

def main():
    work = Path("work")
    work.mkdir(exist_ok=True)
    geo_path, census_path = work / "Census_EAs_Geo.json", work / "Census_2013_2023.csv"
    print("Downloading EA boundaries and census attributes from GitHub...")
    download(GEOJSON_URL, geo_path)
    download(CENSUS_URL, census_path)

    with geo_path.open("r", encoding="utf-8-sig") as f:
        geo = json.load(f)
    features = geo.get("features", [])
    if not features:
        raise RuntimeError("Census_EAs_Geo.json contains no GeoJSON features.")
    census = load_census(census_path)
    print(f"Loaded {len(features):,} EA features and {len(census):,} census rows.")

    # Resolve each EA geometry, identifiers, area, and census values.
    prepared = []
    area_transform = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True).transform
    for index, feature in enumerate(features):
        props = feature.get("properties") or {}
        geom = shape(feature["geometry"])
        if geom.is_empty:
            continue
        code = first_text(props, ["EA_code", "feacode", "FEACODE", "fsacode", "eaname"])
        if not code:
            code = str(index + 1)
        row = census.get(norm(code), {})
        pop = first_value(props, ["Census_pop_23", "map_pop_23", "ls_pop_23", "sum_pop"])
        if pop is None:
            pop = first_value(row, ["Census_pop_23", "pop_23", "population_2023", "POP_2023", "population"])
        hh = first_value(props, ["Census_hhs_23", "map_hhs_23", "ls_hhs_23", "sum_hhs"])
        if hh is None:
            hh = first_value(row, ["Census_hhs_23", "hhs_23", "households_2023", "HH_2023", "households"])
        area_ha = shp_transform(area_transform, geom).area / 10000.0
        prepared.append({
            "props": props, "geom": geom, "code": code,
            "lga": first_text(props, ["lganame", "LGA", "region"]),
            "district": first_text(props, ["distname", "district"]),
            "ward": first_text(props, ["wardname", "ward"]),
            "ea_name": first_text(props, ["eaname", "EA_name", "settlement"]),
            "population": pop, "households": hh, "land_area_ha": area_ha,
        })
    if not prepared:
        raise RuntimeError("No usable EA geometries were found.")

    keys = list_raster_keys()
    print(f"Found {len(keys)} public GHSL GeoTIFF objects.")
    built = {ea["code"]: {} for ea in prepared}
    for year in YEARS:
        key = find_key(keys, year)
        print(f"Processing GHSL {year}: {key}")
        with open_s3_raster(key) as ds:
            # Convert EA geometries to the raster CRS where needed.
            transformer = None
            if ds.crs and ds.crs.to_string() not in ("EPSG:4326", "OGC:CRS84"):
                transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
            raster_geoms = []
            bounds = []
            for ea in prepared:
                g = shp_transform(transformer, ea["geom"]) if transformer else ea["geom"]
                raster_geoms.append(g)
                bounds.append(g.bounds)
            # Read the national Gambia window once. Individual EA masks are
            # then rasterized only over their own small pixel windows.
            minx = min(b[0] for b in bounds); miny = min(b[1] for b in bounds)
            maxx = max(b[2] for b in bounds); maxy = max(b[3] for b in bounds)
            win = rasterio.windows.from_bounds(minx, miny, maxx, maxy, ds.transform)
            win = win.round_offsets().round_lengths()
            win = win.intersection(Window(0, 0, ds.width, ds.height))
            country = ds.read(1, window=win, masked=True)
            country_transform = ds.window_transform(win)
            for ea, geom in zip(prepared, raster_geoms):
                left, bottom, right, top = geom.bounds
                try:
                    ew = rasterio.windows.from_bounds(left, bottom, right, top, ds.transform)
                    ew = ew.round_offsets().round_lengths()
                    # Clip to the already-loaded Gambia array.
                    col0 = max(0, int(ew.col_off - win.col_off))
                    row0 = max(0, int(ew.row_off - win.row_off))
                    col1 = min(country.shape[1], int(ew.col_off + ew.width - win.col_off))
                    row1 = min(country.shape[0], int(ew.row_off + ew.height - win.row_off))
                    if col1 <= col0 or row1 <= row0:
                        total_m2 = 0.0
                    else:
                        local = country[row0:row1, col0:col1]
                        local_transform = rasterio.windows.transform(
                            Window(col0, row0, col1-col0, row1-row0), country_transform)
                        inside = geometry_mask([geom], out_shape=local.shape,
                                                transform=local_transform, invert=True,
                                                all_touched=False)
                        values = np.ma.array(local, mask=np.ma.getmaskarray(local) | ~inside)
                        total_m2 = float(values.sum()) if values.count() else 0.0
                except Exception as exc:
                    raise RuntimeError(f"EA {ea['code']} mask failed for {year}: {exc}") from exc
                built[ea["code"]][year] = total_m2 / 10000.0

    fields = [
        "EA_code", "EA_name", "LGA", "District", "Ward", "EA_land_area_ha",
        "Population_2023", "Households_2023",
    ]
    for year in YEARS:
        fields += [f"GHSL_BU_{year}_ha", f"GHSL_BU_{year}_share_pct",
                   f"GHSL_BU_{year}_m2_per_person", f"GHSL_BU_{year}_m2_per_household",
                   f"Population_per_BU_ha_{year}", f"Households_per_BU_ha_{year}"]
    fields += ["GHSL_BU_expansion_2010_2020_ha", "GHSL_BU_expansion_2020_2025_ha",
               "GHSL_BU_expansion_2010_2020_pct", "GHSL_BU_expansion_2020_2025_pct",
               "GHSL_source", "GHSL_resolution", "GHSL_license"]

    def ratio(a, b):
        return (a / b) if a is not None and b not in (None, 0) else ""

    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for ea in prepared:
            vals = built[ea["code"]]
            out = {
                "EA_code": ea["code"], "EA_name": ea["ea_name"], "LGA": ea["lga"],
                "District": ea["district"], "Ward": ea["ward"],
                "EA_land_area_ha": round(ea["land_area_ha"], 4),
                "Population_2023": ea["population"] if ea["population"] is not None else "",
                "Households_2023": ea["households"] if ea["households"] is not None else "",
            }
            for year in YEARS:
                ha = vals[year]
                pop, hh = ea["population"], ea["households"]
                out[f"GHSL_BU_{year}_ha"] = round(ha, 4)
                out[f"GHSL_BU_{year}_share_pct"] = round(100 * ha / ea["land_area_ha"], 4) if ea["land_area_ha"] else ""
                out[f"GHSL_BU_{year}_m2_per_person"] = round(ha * 10000 / pop, 4) if pop else ""
                out[f"GHSL_BU_{year}_m2_per_household"] = round(ha * 10000 / hh, 4) if hh else ""
                out[f"Population_per_BU_ha_{year}"] = round(pop / ha, 4) if pop is not None and ha > 0 else ""
                out[f"Households_per_BU_ha_{year}"] = round(hh / ha, 4) if hh is not None and ha > 0 else ""
            d1 = vals[2020] - vals[2010]
            d2 = vals[2025] - vals[2020]
            out["GHSL_BU_expansion_2010_2020_ha"] = round(d1, 4)
            out["GHSL_BU_expansion_2020_2025_ha"] = round(d2, 4)
            out["GHSL_BU_expansion_2010_2020_pct"] = round(100*d1/vals[2010], 4) if vals[2010] else ""
            out["GHSL_BU_expansion_2020_2025_pct"] = round(100*d2/vals[2020], 4) if vals[2020] else ""
            out["GHSL_source"] = "European Commission Joint Research Centre (JRC), GHSL GHS-BUILT-S R2023A"
            out["GHSL_resolution"] = "3 arc-second WGS84 (~100 m); native pixel built-up area in m2"
            out["GHSL_license"] = "CC BY 4.0"
            writer.writerow(out)

    print(f"SUCCESS: wrote {OUT} with {len(prepared):,} EA rows and {len(YEARS)} epochs.")
    if OUT.stat().st_size < 1000:
        raise RuntimeError("Output CSV is unexpectedly small; refusing to publish.")
    # Basic integrity checks: no duplicate EA identifiers and all epochs present.
    with OUT.open("r", encoding="utf-8-sig", newline="") as f:
        check = list(csv.DictReader(f))
    codes = [r["EA_code"] for r in check]
    if len(codes) != len(set(codes)):
        raise RuntimeError("Duplicate EA codes found; refusing to publish.")
    for year in YEARS:
        if any(r[f"GHSL_BU_{year}_ha"] == "" for r in check):
            raise RuntimeError(f"Missing built-up values for epoch {year}; refusing to publish.")

if __name__ == "__main__":
    main()
