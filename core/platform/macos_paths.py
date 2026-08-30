"""macOS path identity provider for the W0.2B.1 platform boundary."""
from __future__ import annotations

import hashlib
import os
from os import PathLike

from .contracts import PathIdentity, PathIdentityError


def _coerce_path(path: str | PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise PathIdentityError("path_type")
    if not value:
        raise PathIdentityError("empty_path")
    if "\0" in value:
        raise PathIdentityError("nul_character")
    return value


def _identity_from_stat(value: os.stat_result) -> str:
    return f"macos-file:v1:{int(value.st_dev):x}:{int(value.st_ino):x}"


def _missing_child_identity(parent: os.stat_result, leaf: str) -> str:
    digest = hashlib.sha256(os.fsencode(leaf)).hexdigest()
    return (
        f"macos-child:v1:{int(parent.st_dev):x}:{int(parent.st_ino):x}:"
        f"{digest}"
    )


class MacOSPathService:
    """Describe existing paths or one missing final component on macOS."""

    def describe(
        self,
        path: str | PathLike[str],
        *,
        source_root: str | PathLike[str] | None = None,
    ) -> PathIdentity:
        configured = _coerce_path(path)
        display_path = os.path.abspath(configured)
        operational_path = os.path.realpath(display_path)

        try:
            value = os.stat(operational_path)
        except FileNotFoundError:
            parent_path = os.path.dirname(operational_path)
            leaf = os.path.basename(operational_path)
            if not leaf:
                raise PathIdentityError("missing_final_component") from None
            try:
                parent = os.stat(parent_path)
            except OSError as exc:
                raise PathIdentityError(
                    "missing_parent",
                    native_error=exc.errno,
                ) from None
            if not os.path.isdir(parent_path):
                raise PathIdentityError("parent_not_directory")
            identity_key = _missing_child_identity(parent, leaf)
        except OSError as exc:
            raise PathIdentityError(
                "stat_failed",
                native_error=exc.errno,
            ) from None
        else:
            identity_key = _identity_from_stat(value)

        source_relative_path = ""
        if source_root is not None:
            source_relative_path = self._source_relative_path(
                operational_path,
                source_root,
            )

        return PathIdentity(
            display_path=display_path,
            operational_path=operational_path,
            identity_key=identity_key,
            source_relative_path=source_relative_path,
        )

    @staticmethod
    def _source_relative_path(
        operational_path: str,
        source_root: str | PathLike[str],
    ) -> str:
        configured_root = _coerce_path(source_root)
        root_path = os.path.realpath(os.path.abspath(configured_root))
        try:
            root_value = os.stat(root_path)
        except OSError as exc:
            raise PathIdentityError(
                "source_root_unavailable",
                native_error=exc.errno,
            ) from None
        if not os.path.isdir(root_path):
            raise PathIdentityError("source_root_not_directory")
        try:
            common = os.path.commonpath((root_path, operational_path))
        except ValueError:
            raise PathIdentityError("source_root_escape") from None
        if common != root_path:
            raise PathIdentityError("source_root_escape")
        relative = os.path.relpath(operational_path, root_path)
        if relative == ".":
            return ""
        return relative.replace(os.sep, "/")
