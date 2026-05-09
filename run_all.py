"""
Bourbon Hunter — full weekly run.
1. Check Drive for new bottle photos, identify them, add to collection.
2. Scrape prices for all bottles, log to history.
3. Rebuild dashboard.
4. Push changes to GitHub.
"""

import sys
import subprocess
import traceback


def run_step(name, module_name):
    """Run a Python module's main() function."""
    print("\n" + "=" * 70)
    print(f"  STEP: {name}")
    print("=" * 70)
    try:
        module = __import__(module_name)
        module.main()
    except Exception as e:
        print(f"\n  ⚠️  {name} failed: {e}")
        traceback.print_exc()
        print(f"\n  Continuing to next step...")


def run_shell(name, *commands):
    """Run a sequence of shell commands."""
    print("\n" + "=" * 70)
    print(f"  STEP: {name}")
    print("=" * 70)
    for cmd in commands:
        print(f"  $ {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout)
        if result.returncode != 0:
            print(f"  ⚠️  Command failed (exit {result.returncode})")
            if result.stderr:
                print(result.stderr)
            return False
    return True


def main():
    print("\n" + "#" * 70)
    print("#  BOURBON HUNTER — FULL WEEKLY RUN")
    print("#" * 70)

    run_step("Photo Intake (Google Drive)", "drive_intake")
    run_step("Price Pipeline", "pipeline")
    run_step("Build Dashboard", "build_dashboard")

    # Push to GitHub if there are changes
    print("\n" + "=" * 70)
    print("  STEP: Push to GitHub")
    print("=" * 70)
    
    # Check if there are changes
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if not status.stdout.strip():
        print("  No changes to push.")
    else:
        print("  Changes detected. Committing and pushing...")
        subprocess.run(["git", "add", "data/", "docs/"], capture_output=True, text=True)
        commit = subprocess.run(
            ["git", "commit", "-m", "Auto-update: weekly run"],
            capture_output=True, text=True
        )
        if commit.returncode == 0:
            print("  Commit created.")
            push = subprocess.run(["git", "push"], capture_output=True, text=True)
            if push.returncode == 0:
                print("  Pushed to GitHub. Dashboard will be live in ~1 minute.")
            else:
                print(f"  ⚠️  Push failed: {push.stderr}")
        else:
            print(f"  ⚠️  Commit failed: {commit.stderr}")

    print("\n" + "#" * 70)
    print("#  RUN COMPLETE")
    print("#" * 70 + "\n")


if __name__ == "__main__":
    main()