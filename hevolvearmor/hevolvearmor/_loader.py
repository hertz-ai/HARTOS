"""
ArmoredFinder — sys.meta_path import hook for encrypted .enc modules.

Intercepts `import hevolveai` (and submodules), decrypts .enc → .pyc
via the Rust native decryptor, then loads the code object.

Thread-safe, supports packages and submodules, caches decrypted code.
"""
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import marshal
import os
import sys
import types


class ArmoredFinder(importlib.abc.MetaPathFinder):
    """Finds and loads encrypted Python modules from .enc files."""

    def __init__(self, modules_dir: str, key: bytes, package_names: list = None):
        """
        Args:
            modules_dir: path to directory containing encrypted .enc files
            key: 32-byte AES decryption key
            package_names: top-level package names to intercept
                          (default: auto-detect from modules_dir subdirs)
        """
        self._modules_dir = os.path.abspath(modules_dir)
        self._key = key
        self._code_cache = {}  # module_name → code object

        if package_names is not None:
            self._package_names = set(package_names)
        else:
            # Auto-detect: each subdirectory with __init__.enc is a package
            self._package_names = set()
            if os.path.isdir(self._modules_dir):
                for entry in os.listdir(self._modules_dir):
                    entry_dir = os.path.join(self._modules_dir, entry)
                    if os.path.isdir(entry_dir) and os.path.isfile(
                            os.path.join(entry_dir, '__init__.enc')):
                        self._package_names.add(entry)

    def _should_handle(self, fullname: str) -> bool:
        """Check if this module belongs to an armored package."""
        top = fullname.split('.')[0]
        return top in self._package_names

    def _find_enc_path(self, fullname: str):
        """Map module name to .enc file path.

        Returns (enc_path, is_package) or (None, False).
        """
        parts = fullname.split('.')
        # Try as package: foo/bar/__init__.enc
        pkg_path = os.path.join(self._modules_dir, *parts, '__init__.enc')
        if os.path.isfile(pkg_path):
            return pkg_path, True

        # Try as module: foo/bar.enc
        mod_path = os.path.join(self._modules_dir, *parts[:-1], parts[-1] + '.enc')
        if os.path.isfile(mod_path):
            return mod_path, False

        return None, False

    def find_module(self, fullname, path=None):
        """Legacy finder interface (Python < 3.12 compat)."""
        if not self._should_handle(fullname):
            return None
        enc_path, _ = self._find_enc_path(fullname)
        if enc_path is not None:
            return self
        return None

    def find_spec(self, fullname, path, target=None):
        """Modern finder interface (PEP 451)."""
        if not self._should_handle(fullname):
            return None

        enc_path, is_package = self._find_enc_path(fullname)
        if enc_path is None:
            return None

        origin = enc_path

        if is_package:
            submodule_search_locations = [
                os.path.join(self._modules_dir, *fullname.split('.'))
            ]
        else:
            submodule_search_locations = None

        spec = importlib.machinery.ModuleSpec(
            fullname,
            loader=ArmoredLoader(self, enc_path, is_package),
            origin=origin,
            is_package=is_package,
        )
        if submodule_search_locations is not None:
            spec.submodule_search_locations = submodule_search_locations
        return spec

    def _decrypt_and_unmarshal(self, enc_path: str):
        """Decrypt .enc file and unmarshal the code object."""
        if enc_path in self._code_cache:
            return self._code_cache[enc_path]

        from hevolvearmor._native import armor_load_module
        pyc_bytes = bytes(armor_load_module(enc_path, self._key))

        # .pyc format: magic(4) + flags(4) + timestamp(4) + size(4) + code
        if len(pyc_bytes) < 16:
            raise ImportError(f"Decrypted .pyc too short: {enc_path}")

        code = marshal.loads(pyc_bytes[16:])
        self._code_cache[enc_path] = code
        return code


class ArmoredLoader(importlib.abc.Loader):
    """Loads a single encrypted module."""

    def __init__(self, finder: ArmoredFinder, enc_path: str, is_package: bool):
        self._finder = finder
        self._enc_path = enc_path
        self._is_package = is_package

    def create_module(self, spec):
        """Use default module creation."""
        return None

    def exec_module(self, module):
        """Decrypt and execute the module code."""
        code = self._finder._decrypt_and_unmarshal(self._enc_path)
        exec(code, module.__dict__)


# ─── Public API ───────────────────────────────────────────────────────────────

_installed_finders = []


def install_loader(modules_dir: str, key: bytes,
                   package_names: list = None) -> ArmoredFinder:
    """Install the armored import hook into sys.meta_path.

    Args:
        modules_dir: path to encrypted modules directory
        key: 32-byte AES key
        package_names: package names to intercept (auto-detect if None)

    Returns:
        The installed ArmoredFinder instance.
    """
    finder = ArmoredFinder(modules_dir, key, package_names)
    # Insert at position 0 so we intercept before the default finders
    sys.meta_path.insert(0, finder)
    _installed_finders.append(finder)
    return finder


def uninstall_loader(finder: ArmoredFinder = None):
    """Remove armored finder(s) from sys.meta_path."""
    if finder is not None:
        try:
            sys.meta_path.remove(finder)
            _installed_finders.remove(finder)
        except ValueError:
            pass
    else:
        for f in list(_installed_finders):
            try:
                sys.meta_path.remove(f)
            except ValueError:
                pass
        _installed_finders.clear()
