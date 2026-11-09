"""Download the public CCLA forms and the EMNIST handwriting data into _downloads/ (git-ignored, never committed).

    python download.py            (skips files already present; records sha256 + date in _downloads/manifest.json)

No sign-in is needed for any of these sources.  Nothing here is ever sent anywhere."""
from __future__ import annotations

import datetime
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOWNLOADS = HERE / "_downloads"
MANIFEST = DOWNLOADS / "manifest.json"

CCLA_URL = "https://www.ccla.co.uk/documents/{slug}/download?inline"
CCLA_SLUGS = [
    # shared (all funds)
    "change-address-form", "email-instruction-authority-form", "change-organisation-name", "change-signatory-form",
    "change-correspondent-form",
    # mandate forms
    "coif-charities-funds-mandate-form", "caif-catholic-investment-fund-mandate-form", "cbf-church-england-funds-mandate-form",
    "public-sector-deposit-fund-mandate-form", "local-authorities-property-fund-mandate-form",
    # income & withdrawal payments
    "coif-charities-funds-amendment-income-and-withdrawal-payments-form",
    "caif-catholic-investment-fund-amendment-income-and-withdrawal-payments-form",
    "cbf-church-england-funds-amendment-income-and-withdrawal-payments-form",
    "psdf-amendment-income-and-withdrawal-payments", "cbf-church-england-funds-diocesan-income-and-withdrawal-payments-form",
]
EMNIST_BASE = "https://huggingface.co/datasets/Royc30ne/emnist-balanced/resolve/main/"
EMNIST_FILES = ["emnist-balanced-test-images-idx3-ubyte.gz", "emnist-balanced-test-labels-idx1-ubyte.gz",
                "emnist-balanced-mapping.txt"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, target: Path, manifest: dict) -> None:
    key = target.relative_to(DOWNLOADS).as_posix()
    if target.exists() and target.stat().st_size > 0:
        manifest.setdefault(key, {"url": url, "sha256": sha256(target), "downloaded": "before manifest"})
        print(f"present  {key}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (synthetic-test-downloader)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    manifest[key] = {"url": url, "sha256": sha256(target), "bytes": len(data),
                     "downloaded": datetime.date.today().isoformat()}
    print(f"saved    {key} ({len(data)} bytes)")


def main() -> int:
    manifest = json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {}
    failed = []
    for slug in CCLA_SLUGS:
        try:
            fetch(CCLA_URL.format(slug=slug), DOWNLOADS / "ccla" / f"{slug}.pdf", manifest)
        except Exception as e:                      # keep going: one missing form only limits the generator
            failed.append(f"{slug}: {e}")
    for name in EMNIST_FILES:
        try:
            fetch(EMNIST_BASE + name, DOWNLOADS / "emnist" / name, manifest)
        except Exception as e:
            failed.append(f"{name}: {e}")
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1), "utf-8")
    for f in failed:
        print("FAILED  ", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
