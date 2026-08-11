"""CLI: python -m corpusgen --seed 42 --out data/corpus --manifest data/manifest.json

Determinism contract (docs/plan.md D6): ONE random.Random(seed) plus a
Faker seeded from it drive everything; the same seed reproduces the
manifest byte-for-byte (PDF internals are pinned via reportlab's invariant
mode, DOCX/XLSX core properties via a fixed date). Generation always ends
with a full validation pass; --validate runs that pass standalone against
an existing corpus (profile read from the manifest).
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path

from faker import Faker

from corpusgen import config, identities, manifest as manifest_mod, templates, validate
from corpusgen.scenarios import BuildContext, build_scenarios


def generate(
    seed: int, cfg: config.CorpusConfig, out_dir: Path, manifest_path: Path
) -> dict:
    """Generate one corpus + manifest (no validation pass). Extracted from
    main() so tests can run the whole pipeline against tiny configs."""
    rng = random.Random(seed)
    faker = Faker("en_US")
    faker.seed_instance(rng.getrandbits(64))

    pool = identities.generate_identities(rng, faker, cfg)
    staff = templates.make_staff(rng, faker)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    ctx = BuildContext(
        rng=rng, faker=faker, cfg=cfg, pool=pool, staff=staff, out_dir=out_dir
    )
    for scenario in build_scenarios(cfg):
        scenario.emit(ctx)

    data = manifest_mod.build(seed, cfg, pool, ctx.documents)
    manifest_mod.write(manifest_path, data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corpusgen",
        description="Generate the synthetic breach corpus + ground-truth manifest.",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default 42)")
    parser.add_argument(
        "--out", type=Path, default=Path("data/corpus"),
        help="output corpus directory (wiped and regenerated)",
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("data/manifest.json"),
        help="manifest JSON path",
    )
    parser.add_argument(
        "--mini", action="store_true",
        help="~60-doc mini corpus (40 identities, scaled quotas)",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="validate an existing corpus+manifest instead of generating",
    )
    args = parser.parse_args(argv)

    if args.validate:
        return validate.run(args.manifest, args.out)

    cfg = config.MINI if args.mini else config.FULL
    data = generate(args.seed, cfg, args.out, args.manifest)
    counts = data["generated_counts"]
    print(
        f"Generated {counts['documents']} documents / {counts['identities']} identities "
        f"({cfg.profile}, seed {args.seed}) -> {args.out}"
    )
    print(f"Manifest: {args.manifest} ({counts['plantings']} plantings)\n")
    return validate.run(args.manifest, args.out)


if __name__ == "__main__":
    sys.exit(main())
