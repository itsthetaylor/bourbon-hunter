"""
msrp_lookup.py - populate the blank `msrp` column for active bottles.

WHAT THIS DOES
  For every ACTIVE bottle in data/bottles.csv whose `msrp` is blank, fill in the
  bottle's TRUE MSRP - the distillery/brand's suggested retail (sticker) price,
  NOT a retailer's allocated street price. Market value is already tracked
  separately in price_history.csv by the pipeline, so MSRP here is the stable
  reference the dashboard shows next to "Paid".

WHY THE VALUES ARE A CURATED TABLE (not live scraping)
  The originally-requested sources don't work for automated lookup:
    * Drizly shut down (Uber closed it, April 2024) - the site no longer exists.
    * Total Wine and ReserveBar are Cloudflare/bot-protected, location-gated, and
      JS-rendered; requests + BeautifulSoup get blocked or empty pages.
  So the MSRPs below were gathered via web research from primary/secondary
  sources (Buffalo Trace, Heaven Hill, Beam Suntory press + reputable bourbon
  references) and are listed here with their source for review. Re-running this
  script applies the same reviewed table; update PROPOSED to change a value.

SAFETY
  Writes go through archive_bottle.save_bottles() - the project's atomic
  (temp file + os.replace) writer that preserves the exact header order. Dry-run
  is the default; nothing is written without --apply.

USAGE
  python msrp_lookup.py            # dry-run: print the proposed table, write nothing
  python msrp_lookup.py --apply    # write proposed MSRPs into data/bottles.csv
"""

import argparse
import sys

from archive_bottle import BOTTLES_CSV, load_bottles, read_header, save_bottles

# bottle_id -> (proposed_msrp | None, source, note)
#   proposed_msrp is None when the bottle has no official US MSRP (export-only);
#   those are left blank unless you fill them in deliberately.
PROPOSED = {
    "weller_single_barrel": (49.99, "Buffalo Trace MSRP", "high confidence"),
    "stagg_kentucky_straight_bourbon_whiskey": (
        59.99, "Stagg (Jr) MSRP", "cited $50-70; NOT George T. Stagg/BTAC ($150)"),
    "blantons_single_barrel_japanese_export": (
        None, "export-only", "no official US MSRP (Japanese export / Black)"),
    "blantons_single_barrel_bourbon_121_8": (
        None, "export-only", "no official US MSRP (Straight From The Barrel)"),
    "blantons_gold_edition": (
        None, "export-only", "no official US MSRP (Gold); intl SRP ~$120"),
    "eagle_rare_10_year_single_barrel_select": (39.99, "Buffalo Trace MSRP", "high confidence"),
    "little_book_chapter_5_the_invitation": (124.99, "Beam Suntory MSRP", "high confidence"),
    "little_book_chapter_08_path_not_taken": (149.99, "Beam Suntory MSRP", "high confidence"),
    "thomas_h_handy_sazerac_straight_rye_whiskey": (
        149.99, "Buffalo Trace Antique Collection 2025 SRP", "high confidence"),
    "colonel_eh_taylor_straight_rye_bottled_in_bond": (
        69.99, "Buffalo Trace MSRP", "classic $69.99; recent releases ~$79.99"),
    "colonel_eh_taylor_barrel_proof": (
        69.99, "Buffalo Trace MSRP", "classic $69.99; recent releases ~$79.99"),
    "penelope_bourbon_blend_of_straight_whiskeys": (
        79.99, "Penelope SRP (state ABC listing)", "medium confidence"),
    "weller_antique_107": (49.99, "Buffalo Trace MSRP", "high confidence"),
    "weller_12_year": (49.99, "Buffalo Trace MSRP", "high confidence"),
    "weller_full_proof": (49.99, "Buffalo Trace MSRP", "high confidence"),
    "old_fitzgerald_bottled_in_bond_9_year": (129.99, "Heaven Hill MSRP", "high confidence"),
}


def candidates(bottles):
    """Active bottles whose msrp is currently blank - the ones we may fill."""
    out = []
    for b in bottles:
        status = (b.get("status") or "active").strip()
        msrp = (b.get("msrp") or "").strip()
        if status == "active" and msrp == "":
            out.append(b)
    return out


def fmt_price(v):
    return f"${v:,.2f}" if v is not None else "-"


def print_table(bottles):
    rows = candidates(bottles)
    id_w = max([len(b["bottle_id"]) for b in rows] + [len("bottle_id")])
    header = f"{'bottle_id':<{id_w}}  {'current':>9}  {'proposed':>9}  source"
    print(header)
    print("-" * len(header))
    for b in rows:
        bid = b["bottle_id"]
        current = (b.get("msrp") or "").strip()
        proposed, source, note = PROPOSED.get(bid, (None, "NOT FOUND", "no proposed value"))
        prop_str = fmt_price(proposed)
        src = source if proposed is not None else f"{source} - {note}"
        print(f"{bid:<{id_w}}  {current or '-':>9}  {prop_str:>9}  {src}")
    print()
    fillable = [b for b in rows if PROPOSED.get(b["bottle_id"], (None,))[0] is not None]
    blanks = [b for b in rows if PROPOSED.get(b["bottle_id"], (None,))[0] is None]
    print(f"{len(rows)} active bottles with blank MSRP.")
    print(f"  {len(fillable)} have a proposed MSRP to write.")
    if blanks:
        print(f"  {len(blanks)} left blank (no official US MSRP): "
              + ", ".join(b["bottle_id"] for b in blanks))


def apply(bottles):
    fieldnames = read_header(BOTTLES_CSV)
    changed = []
    for b in candidates(bottles):
        proposed = PROPOSED.get(b["bottle_id"], (None,))[0]
        if proposed is None:
            continue
        b["msrp"] = f"{proposed:.2f}"
        changed.append((b["bottle_id"], proposed))
    if not changed:
        print("Nothing to write (no proposed MSRPs for the blank active bottles).")
        return
    save_bottles(bottles, fieldnames)
    print(f"Wrote {len(changed)} MSRPs to {BOTTLES_CSV} (atomic):")
    for bid, v in changed:
        print(f"  {bid}: {fmt_price(v)}")


def main():
    parser = argparse.ArgumentParser(description="Populate blank MSRPs for active bottles.")
    parser.add_argument("--apply", action="store_true",
                        help="Write the proposed MSRPs (default is a dry run).")
    args = parser.parse_args()

    bottles = load_bottles()
    if not bottles:
        print(f"No bottles found in {BOTTLES_CSV}", file=sys.stderr)
        sys.exit(1)

    if args.apply:
        apply(bottles)
    else:
        print_table(bottles)
        print("\nDry run - nothing written. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
