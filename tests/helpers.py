from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil


@dataclass
class FakePaths:
    codex: str
    claude: str
    agy: str
    log: Path

    @classmethod
    def create(cls, tmp_path, monkeypatch):
        fake_root = Path(__file__).parent / "fakes"
        fixture_root = Path(__file__).parent / "fixtures"
        bin_root = tmp_path / "bin"
        bin_root.mkdir()
        paths = {}
        for executable_name in ("codex", "claude", "agy"):
            target = bin_root / executable_name
            shutil.copyfile(fake_root / executable_name, target)
            target.chmod(0o755)
            paths[executable_name] = str(target)

        log = tmp_path / "fake.log"
        log.touch()
        monkeypatch.setenv("MAO_FAKE_LOG", str(log))
        monkeypatch.setenv("MAO_FAKE_FIXTURES", str(fixture_root))
        return cls(log=log, **paths)

    def rows(self, executable_name: str) -> list[dict]:
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if row["executable"] == executable_name]

    def last_args(self, executable_name: str) -> list[str]:
        return self.rows(executable_name)[-1]["args"]
