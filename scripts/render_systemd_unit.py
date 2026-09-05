from __future__ import annotations

import argparse
from pathlib import Path


def quote_systemd(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_unit(app_dir: Path, env_file: Path, python_bin: Path) -> str:
    return f"""[Unit]
Description=CAS Gallery integrated web service
After=network.target

[Service]
Type=simple
WorkingDirectory={app_dir}
EnvironmentFile={env_file}
ExecStart={quote_systemd(str(python_bin))} -m app.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the CAS Gallery user service")
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--python-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_unit(
            args.app_dir.resolve(),
            args.env_file.resolve(),
            args.python_bin.resolve(),
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
