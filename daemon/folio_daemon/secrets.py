import json
import os
import platform
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional


SECRET_ENV_MAP: dict[str, tuple[str, ...]] = {
    "hindsight_api_key": ("HINDSIGHT_API_KEY",),
    "spark_memory_api_token": ("SPARK_MEMORY_API_TOKEN",),
    "notion_api_key": ("NOTION_API_KEY", "NOTION_API_TOKEN"),
    "notion_api_key_perso": ("NOTION_API_KEY_PERSO",),
    "orbrouter_api_key": ("FOLIO_ORBROUTER_API_KEY", "ORBROUTER_API_KEY"),
}

SECRET_FILE_ENV_MAP: dict[str, tuple[str, ...]] = {
    "hindsight_api_key": ("FOLIO_HINDSIGHT_KEY_FILE", "HINDSIGHT_API_KEY_FILE"),
    "spark_memory_api_token": ("FOLIO_SPARK_MEMORY_TOKEN_FILE", "SPARK_MEMORY_API_TOKEN_FILE"),
    "notion_api_key": ("FOLIO_NOTION_KEY_FILE", "NOTION_API_KEY_FILE", "NOTION_API_TOKEN_FILE"),
    "notion_api_key_perso": ("FOLIO_NOTION_PERSO_KEY_FILE", "NOTION_API_KEY_PERSO_FILE"),
    "orbrouter_api_key": ("FOLIO_ORBROUTER_KEY_FILE",),
}


class CredentialResolver:
    """Resolves credentials exclusively in-process via runtime env, private 0600 key files,
    local Orb connection (`~/.orb/connection.json` proxy-key provisioning), optional external
    secret helper command, or OS keyring (`secret-tool` on Linux, Keychain `security` on macOS).
    Never logs, echoes, or writes secret values into SQLite or transcripts.
    """

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self._overrides: dict[str, str] = {}
        self._cache: dict[str, str] = {}
        self._state_dir = state_dir

    def set_ephemeral(self, logical_name: str, value: str) -> None:
        cleaned = value.strip()
        if cleaned:
            self._overrides[logical_name] = cleaned
            self._cache[logical_name] = cleaned
        else:
            self._overrides.pop(logical_name, None)
            self._cache.pop(logical_name, None)

    def clear_cache(self) -> None:
        self._cache.clear()

    def get(self, logical_name: str) -> Optional[str]:
        if logical_name in self._overrides:
            return self._overrides[logical_name]
        if logical_name in self._cache:
            return self._cache[logical_name]

        for env_key in SECRET_ENV_MAP.get(logical_name, ()):
            val = os.environ.get(env_key, "").strip()
            if val:
                self._cache[logical_name] = val
                return val

        for file_env in SECRET_FILE_ENV_MAP.get(logical_name, ()):
            file_path = os.environ.get(file_env, "").strip()
            if file_path:
                val = self._read_secret_file(Path(file_path))
                if val:
                    self._cache[logical_name] = val
                    return val

        secrets_dir = os.environ.get("FOLIO_SECRETS_DIR", "").strip()
        if secrets_dir:
            for env_key in SECRET_ENV_MAP.get(logical_name, ()):
                val = self._read_secret_file(Path(secrets_dir) / env_key)
                if val:
                    self._cache[logical_name] = val
                    return val

        if logical_name == "orbrouter_api_key":
            orb_val = self._resolve_orbrouter_key()
            if orb_val:
                self._cache[logical_name] = orb_val
                return orb_val

        helper = os.environ.get("FOLIO_SECRET_HELPER", "").strip()
        if helper and os.path.isfile(helper) and os.access(helper, os.X_OK):
            for env_key in SECRET_ENV_MAP.get(logical_name, ()):
                try:
                    res = subprocess.run(
                        [helper, env_key],
                        capture_output=True,
                        text=True,
                        timeout=4,
                        check=False,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        val = res.stdout.strip()
                        self._cache[logical_name] = val
                        return val
                except Exception:
                    pass

        keyring_val = self._lookup_os_keyring(logical_name)
        if keyring_val:
            self._cache[logical_name] = keyring_val
            return keyring_val

        return None

    def has(self, logical_name: str) -> bool:
        return bool(self.get(logical_name))

    def source_kind(self, logical_name: str) -> str:
        if logical_name in self._overrides:
            return "session-memory"
        for env_key in SECRET_ENV_MAP.get(logical_name, ()):
            if os.environ.get(env_key, "").strip():
                return f"env:{env_key}"
        for file_env in SECRET_FILE_ENV_MAP.get(logical_name, ()):
            p = os.environ.get(file_env, "").strip()
            if p and Path(p).is_file():
                return f"file:{file_env}"
        secrets_dir = os.environ.get("FOLIO_SECRETS_DIR", "").strip()
        if secrets_dir:
            for env_key in SECRET_ENV_MAP.get(logical_name, ()):
                if (Path(secrets_dir) / env_key).is_file():
                    return "secrets-dir"
        if logical_name == "orbrouter_api_key":
            home = Path.home()
            xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
            if (xdg_cfg / "folio" / "orbrouter.key").is_file():
                return "orb-folio-key"
            if (xdg_cfg / "opencode" / "orbrouter.key").is_file():
                return "orb-opencode-key"
            if (home / ".orb" / "connection.json").is_file():
                return "orb-connection"
        if self._lookup_os_keyring(logical_name):
            return "os-keyring"
        return "unconfigured"

    def _resolve_orbrouter_key(self) -> Optional[str]:
        home = Path.home()
        xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        folio_key_file = xdg_cfg / "folio" / "orbrouter.key"
        val = self._read_secret_file(folio_key_file)
        if val:
            return val

        opencode_key_file = xdg_cfg / "opencode" / "orbrouter.key"
        val = self._read_secret_file(opencode_key_file)
        if val:
            return val

        # Auto-provision a dedicated 0600 Folio proxy key from local Orb connection (`~/.orb/connection.json`)
        orb_dir = Path(os.environ.get("ORB_STATE_DIR") or (home / ".orb"))
        conn_file = orb_dir / "connection.json"
        if conn_file.is_file() and not conn_file.is_symlink():
            try:
                conn = json.loads(conn_file.read_text(encoding="utf-8"))
                api_url = str(conn.get("api_url") or "").rstrip("/")
                token = str(conn.get("token") or "").strip()
                if api_url and token:
                    host_label = socket.gethostname() or "desktop"
                    req = urllib.request.Request(
                        f"{api_url}/api/proxy-keys",
                        data=json.dumps({"name": f"{host_label}-folio"}).encode("utf-8"),
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "User-Agent": "folio-desktop/0.2.0",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        created = json.loads(resp.read().decode("utf-8", errors="replace"))
                    raw_key = str(created.get("key") or "").strip()
                    if raw_key:
                        folio_key_file.parent.mkdir(parents=True, exist_ok=True)
                        fd = os.open(str(folio_key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            fh.write(raw_key + "\n")
                        return raw_key
            except Exception:
                pass
        return None

    @staticmethod
    def _read_secret_file(path: Path) -> Optional[str]:
        try:
            if not path.is_file() or path.is_symlink():
                return None
            val = path.read_text(encoding="utf-8").strip()
            return val or None
        except Exception:
            return None

    @staticmethod
    def _resolve_binary(name: str, darwin_fallback: Optional[str] = None) -> Optional[str]:
        found = shutil.which(name)
        if found:
            return found
        nix_sys = Path("/run/current-system/sw/bin") / name
        if nix_sys.is_file() and os.access(nix_sys, os.X_OK):
            return str(nix_sys)
        if darwin_fallback and platform.system().lower() == "darwin":
            fb = Path(darwin_fallback)
            if fb.is_file() and os.access(fb, os.X_OK):
                return str(fb)
        return None

    @staticmethod
    def _lookup_os_keyring(logical_name: str) -> Optional[str]:
        sys_name = platform.system().lower()
        candidates: list[str] = []
        for k in (*SECRET_ENV_MAP.get(logical_name, ()), logical_name, logical_name.upper()):
            if k and k not in candidates:
                candidates.append(k)
        if sys_name == "linux":
            secret_tool = CredentialResolver._resolve_binary("secret-tool")
            if secret_tool:
                for key_name in candidates:
                    for attr_pair in (
                        ("service", "folio", "account", key_name),
                        ("application", "folio", "key", key_name),
                    ):
                        try:
                            res = subprocess.run(
                                [secret_tool, "lookup", *attr_pair],
                                capture_output=True,
                                text=True,
                                timeout=2,
                                check=False,
                            )
                            if res.returncode == 0 and res.stdout.strip():
                                return res.stdout.strip()
                        except Exception:
                            pass
        elif sys_name == "darwin":
            security_bin = CredentialResolver._resolve_binary("security", darwin_fallback="/usr/bin/security")
            if security_bin:
                for key_name in candidates:
                    try:
                        res = subprocess.run(
                            [security_bin, "find-generic-password", "-s", "folio", "-a", key_name, "-w"],
                            capture_output=True,
                            text=True,
                            timeout=2,
                            check=False,
                        )
                        if res.returncode == 0 and res.stdout.strip():
                            return res.stdout.strip()
                    except Exception:
                        pass
        return None
