"""Build the self-contained desktop executable (maintainer tool, never user setup)."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from importlib.metadata import distribution
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Windows desktop executable")
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Build ResearchPilot.exe on Windows.")
    import PyInstaller.__main__

    from researchpilot.desktop.launcher import make_icon

    root = Path(__file__).resolve().parents[1]
    dist = (root / args.dist_dir).resolve()
    build = root / "build" / "desktop"
    build.mkdir(parents=True, exist_ok=True)
    icon = build / "researchpilot.ico"
    make_icon().save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    licenses = build / "licenses"
    python_license = licenses / "Python" / "LICENSE.txt"
    python_license.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", python_license)
    for name in (
        "pydantic",
        "pydantic-core",
        "pydantic-settings",
        "fastapi",
        "starlette",
        "uvicorn",
        "httpx",
        "httpcore",
        "PyYAML",
        "jieba",
        "pystray",
        "Pillow",
        "anyio",
        "certifi",
        "idna",
        "h11",
        "click",
        "colorama",
        "typing_extensions",
        "typing-inspection",
        "annotated-types",
        "python-dotenv",
        "six",
        "pyinstaller",
    ):
        package = distribution(name)
        for file in package.files or []:
            if "license" in file.name.lower() or file.name.lower() in {"copying", "notice"}:
                license_target = licenses / name / file.name
                license_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(package.locate_file(file)), license_target)
    arguments = [
        str(root / "scripts" / "desktop_entry.py"),
        "--name=ResearchPilot",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--noupx",
        f"--icon={icon}",
        f"--distpath={dist}",
        f"--workpath={build / 'work'}",
        f"--specpath={build}",
        f"--paths={root}",
        "--collect-data=jieba",
        "--collect-submodules=uvicorn",
        "--hidden-import=pystray._win32",
        "--hidden-import=tkinter",
        "--hidden-import=tkinter.ttk",
    ]
    for source, target in (
        (root / "researchpilot" / "api" / "static", "researchpilot/api/static"),
        (root / "data" / "knowledge_base", "data/knowledge_base"),
        (root / "data" / "web_corpus", "data/web_corpus"),
        (root / "configs", "configs"),
        (root / "LICENSE", "."),
        (licenses, "licenses"),
    ):
        arguments.append(f"--add-data={source};{target}")
    PyInstaller.__main__.run(arguments)
    print(f"Built: {dist / 'ResearchPilot.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
