"""Edit config.yaml from the GUI while keeping its comments and layout (ruamel.yaml round-trip)."""
from __future__ import annotations

import shutil
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .utils import atomic_write_text


class ConfigEditor:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.indent(mapping=2, sequence=4, offset=2)
        self.yaml.width = 4096
        with open(self.path, encoding="utf-8") as f:
            self.data = self.yaml.load(f) or CommentedMap()

    # ---------- generic settings ----------
    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = CommentedMap()
            node = node[part]
        node[parts[-1]] = value

    # ---------- podcasts ----------
    def _podcasts(self) -> CommentedSeq:
        if not isinstance(self.data.get("podcasts"), list):
            self.data["podcasts"] = CommentedSeq()
        return self.data["podcasts"]

    def podcasts(self) -> list[dict]:
        return [dict(p) for p in self._podcasts() if isinstance(p, dict)]

    def find(self, name: str):
        for p in self._podcasts():
            if isinstance(p, dict) and str(p.get("name", "")).strip().lower() == name.strip().lower():
                return p
        return None

    def add_podcast(self, fields: dict) -> None:
        if self.find(fields["name"]):
            raise ValueError(f"A podcast named '{fields['name']}' already exists")
        entry = CommentedMap()
        for k, v in fields.items():
            if v not in (None, ""):
                entry[k] = v
        self._podcasts().append(entry)

    def update_podcast(self, name: str, fields: dict) -> None:
        entry = self.find(name)
        if entry is None:
            raise ValueError(f"No podcast named '{name}'")
        new_name = fields.get("name")
        if new_name and new_name.lower() != name.lower() and self.find(new_name):
            raise ValueError(f"A podcast named '{new_name}' already exists")
        for k, v in fields.items():
            if v in (None, ""):
                entry.pop(k, None)
            else:
                entry[k] = v

    def remove_podcast(self, name: str) -> None:
        seq = self._podcasts()
        for i, p in enumerate(seq):
            if isinstance(p, dict) and str(p.get("name", "")).strip().lower() == name.strip().lower():
                del seq[i]
                return
        raise ValueError(f"No podcast named '{name}'")

    # ---------- save ----------
    def save(self) -> None:
        import io
        buf = io.StringIO()
        self.yaml.dump(self.data, buf)
        text = buf.getvalue()
        # validate before replacing the user's file
        from .config import ConfigError, load_config
        tmp = self.path.with_suffix(".check.yaml")
        tmp.write_text(text, encoding="utf-8")
        try:
            load_config(tmp)
        except ConfigError:
            raise
        finally:
            tmp.unlink(missing_ok=True)
        backup = self.path.with_suffix(".yaml.bak")
        try:
            shutil.copyfile(self.path, backup)
        except OSError:
            pass
        atomic_write_text(self.path, text)
