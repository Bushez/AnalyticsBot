import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def workspace_directory(root, value):
    path = (root / value).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("Build directories must stay inside the project.")
    return path


def main():
    parser = argparse.ArgumentParser(description="Build AnalyticsBot.app and a clean ZIP on macOS")
    parser.add_argument("--output-directory", default="gui-dist-macos")
    parser.add_argument("--work-directory", default="gui-build-macos")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Run this script on a Mac using Python for the target architecture.")
    root = Path(__file__).resolve().parent
    output = workspace_directory(root, args.output_directory)
    work = workspace_directory(root, args.work_directory)
    if output == work or output.is_relative_to(work) or work.is_relative_to(output):
        parser.error("Output and work directories must be separate.")
    release = output / "AnalyticsBot-macOS"
    archive = output / f"AnalyticsBot-macOS-{platform.machine()}.zip"
    if release.exists() or archive.exists():
        parser.error("Output already exists. Choose another --output-directory to preserve existing files.")
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / "cache"))
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "AnalyticsBot", "--osx-bundle-identifier", "com.analyticsbot.desktop",
        "--distpath", str(work / "output"), "--workpath", str(work / "build"),
        "--specpath", str(work), "--collect-data", "googleapiclient", "--collect-data", "tzdata",
        str(root / "gui_entry.py"),
    ], cwd=root, env=env, check=True)
    bundle = work / "output" / "AnalyticsBot.app"
    subprocess.run([str(bundle / "Contents" / "MacOS" / "AnalyticsBot"), "--self-test"], check=True, timeout=60)
    release.mkdir(parents=True)
    shutil.copytree(bundle, release / bundle.name, symlinks=True)
    shutil.copy2(root / "MACOS_README.txt", release / "README.txt")
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(release), str(archive)], check=True)
    print(f"Ready: {archive}")


if __name__ == "__main__":
    main()
