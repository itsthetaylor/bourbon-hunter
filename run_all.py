"""
Bourbon Hunter — full weekly run.
1. Check Drive for new bottle photos, identify them, add to collection.
2. Scrape prices for all bottles, log to history.
"""

import sys
import traceback


def run_step(name, module_name):
    """Run a step's main() function. Print clear separators and don't crash on error."""
    print("\n" + "=" * 70)
    print(f"  STEP: {name}")
    print("=" * 70)
    try:
        module = __import__(module_name)
        module.main()
    except Exception as e:
        print(f"\n  ⚠️  {name} failed: {e}")
        print("  Traceback:")
        traceback.print_exc()
        print(f"\n  Continuing to next step...")


def main():
    print("\n" + "#" * 70)
    print("#  BOURBON HUNTER — FULL WEEKLY RUN")
    print("#" * 70)

    run_step("Photo Intake (Google Drive)", "drive_intake")
    run_step("Price Pipeline", "pipeline")

    print("\n" + "#" * 70)
    print("#  RUN COMPLETE")
    print("#" * 70 + "\n")


if __name__ == "__main__":
    main()