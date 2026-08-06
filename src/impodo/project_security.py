"""Secure the filesystem root before databases or artifacts are created.

Production mode requires a local, contained, owner-private location: protected
Windows DACLs reject synced/network/reparse/Git paths, while POSIX permissions
are forced to owner-only. Explicit development mode relaxes this for disposable
data and is reported in ``ProjectRootSecurityStatus``.
"""

from __future__ import annotations

from collections.abc import Mapping
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat


DEVELOPMENT_MODE_ENV = "IMPODO_DEVELOPMENT_MODE"
PROJECT_ROOT_ENV = "IMPODO_PROJECT_ROOT"
WINDOWS_SYNC_ROOT_ENVIRONMENTS = (
    "OneDrive",
    "OneDriveCommercial",
    "OneDriveConsumer",
)


class ProjectRootSecurityError(RuntimeError):
    """Raised when the project-data root cannot meet the local security policy."""


@dataclass(frozen=True, slots=True)
class ProjectRootSecurityStatus:
    """Result of preparing the local project-data root."""

    root: Path
    development_mode: bool
    access_control: str


def development_mode_enabled(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the explicit disposable-data development mode is enabled."""

    current = environment if environment is not None else os.environ
    return current.get(DEVELOPMENT_MODE_ENV, "").strip() == "1"


def prepare_project_root(
    root: str | Path,
    *,
    development_mode: bool = False,
    environment: Mapping[str, str] | None = None,
) -> ProjectRootSecurityStatus:
    """Create, contain, and verify the root before any project data is opened.

    Returns the effective access-control mode for startup diagnostics and
    raises without partially accepting a production root that fails policy.
    """

    current = environment if environment is not None else os.environ
    candidate = _absolute_path(root)

    if development_mode:
        candidate.mkdir(parents=True, exist_ok=True)
        return ProjectRootSecurityStatus(
            root=candidate,
            development_mode=True,
            access_control="development-mode-not-enforced",
        )

    if os.name == "nt":
        _validate_windows_location(candidate, current)
        candidate.mkdir(parents=True, exist_ok=True)
        _apply_private_windows_acl(candidate)
        _verify_private_windows_acl(candidate)
        access_control = "windows-protected-dacl"
    else:
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        candidate.chmod(0o700)
        mode = stat.S_IMODE(candidate.stat().st_mode)
        if mode & 0o077:
            raise ProjectRootSecurityError(
                f"Project root permissions are broader than owner-only: {candidate}"
            )
        access_control = "posix-owner-only"

    return ProjectRootSecurityStatus(
        root=candidate,
        development_mode=False,
        access_control=access_control,
    )


def _absolute_path(value: str | Path) -> Path:
    expanded = Path(value).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _validate_windows_location(
    root: Path,
    environment: Mapping[str, str],
) -> None:
    _reject_reparse_points(root)

    drive_type = _windows_drive_type(root)
    if root.anchor.startswith("\\\\") or drive_type != 3:
        raise ProjectRootSecurityError(
            "The protected project root must be on a fixed local Windows drive."
        )

    resolved = root.resolve(strict=False)
    for variable in WINDOWS_SYNC_ROOT_ENVIRONMENTS:
        configured = environment.get(variable)
        if configured and _is_relative_to(resolved, _absolute_path(configured).resolve()):
            raise ProjectRootSecurityError(
                f"The protected project root cannot be inside {variable}."
            )

    if _inside_git_checkout(resolved):
        raise ProjectRootSecurityError(
            "The protected project root cannot be inside a Git checkout."
        )


def _reject_reparse_points(root: Path) -> None:
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            raise ProjectRootSecurityError(
                f"The protected project root cannot traverse a link or junction: {current}"
            )
        if not current.exists():
            break


def _windows_drive_type(root: Path) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [ctypes.c_wchar_p]
    get_drive_type.restype = ctypes.c_uint
    return int(get_drive_type(root.anchor or os.fspath(root)))


def _inside_git_checkout(root: Path) -> bool:
    return any((parent / ".git").exists() for parent in (root, *root.parents))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _apply_private_windows_acl(root: Path) -> None:
    """Install a protected DACL for the user, SYSTEM, and Administrators."""

    current_sid = _current_windows_user_sid()
    descriptor_text = (
        "D:P"
        f"(A;OICI;FA;;;{current_sid})"
        "(A;OICI;FA;;;SY)"
        "(A;OICI;FA;;;BA)"
    )
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint),
    ]
    convert.restype = ctypes.c_int

    descriptor = ctypes.c_void_p()
    descriptor_size = ctypes.c_uint()
    if not convert(
        descriptor_text,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise _windows_security_error("create the private security descriptor")

    try:
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        ]
        get_dacl.restype = ctypes.c_int

        dacl_present = ctypes.c_int()
        dacl = ctypes.c_void_p()
        dacl_defaulted = ctypes.c_int()
        if not get_dacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value:
            raise _windows_security_error("read the private DACL")

        set_security = advapi32.SetNamedSecurityInfoW
        set_security.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_security.restype = ctypes.c_uint
        result = set_security(
            os.fspath(root),
            1,
            0x00000004 | 0x80000000,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise ProjectRootSecurityError(
                f"Windows refused the private project-root DACL: "
                f"{ctypes.WinError(result)}"
            )
    finally:
        local_free(descriptor)


def _verify_private_windows_acl(root: Path) -> None:
    current_sid = _current_windows_user_sid()
    descriptor = _windows_security_descriptor(root)
    _validate_private_windows_sddl(descriptor, current_sid)


def _validate_private_windows_sddl(descriptor: str, current_sid: str) -> None:
    owner = re.search(r"O:(S-\d(?:-\d+)+|[A-Z]{2})", descriptor)
    if not owner or owner.group(1) != current_sid:
        raise ProjectRootSecurityError(
            "The project root is not owned by the current Windows user."
        )

    dacl_offset = descriptor.find("D:")
    if dacl_offset < 0 or not descriptor[dacl_offset + 2 :].startswith("P"):
        raise ProjectRootSecurityError(
            "The project-root DACL still inherits permissions from its parent."
        )

    aliases = {
        "S-1-5-18": "SY",
        "S-1-5-32-544": "BA",
    }
    trustees: list[str] = []
    for encoded_ace in re.findall(r"\(([^()]*)\)", descriptor[dacl_offset + 2 :]):
        fields = encoded_ace.split(";")
        if len(fields) != 6:
            raise ProjectRootSecurityError("Windows returned an unreadable DACL entry.")
        ace_type, flags, rights, _object_guid, _inherit_guid, trustee = fields
        trustee = aliases.get(trustee, trustee)
        if (
            ace_type != "A"
            or "OI" not in flags
            or "CI" not in flags
            or rights != "FA"
        ):
            raise ProjectRootSecurityError(
                "The project-root DACL contains an unexpected access rule."
            )
        trustees.append(trustee)

    if sorted(trustees) != sorted([current_sid, "SY", "BA"]):
        raise ProjectRootSecurityError(
            "The project-root DACL grants access outside the approved identities."
        )


def _windows_security_descriptor(root: Path) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = ctypes.c_uint

    descriptor = ctypes.c_void_p()
    result = get_security(
        os.fspath(root),
        1,
        0x00000001 | 0x00000004,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ProjectRootSecurityError(
            f"Windows could not read the project-root security descriptor: "
            f"{ctypes.WinError(result)}"
        )

    try:
        convert = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(ctypes.c_uint),
        ]
        convert.restype = ctypes.c_int
        output = ctypes.c_wchar_p()
        output_length = ctypes.c_uint()
        if not convert(
            descriptor,
            1,
            0x00000001 | 0x00000004,
            ctypes.byref(output),
            ctypes.byref(output_length),
        ):
            raise _windows_security_error("format the project-root DACL")
        try:
            return output.value or ""
        finally:
            local_free(ctypes.cast(output, ctypes.c_void_p))
    finally:
        local_free(descriptor)


def _current_windows_user_sid() -> str:
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [
            ("sid", ctypes.c_void_p),
            ("attributes", wintypes.DWORD),
        ]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    open_token = advapi32.OpenProcessToken
    open_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    open_token.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not open_token(get_current_process(), 0x0008, ctypes.byref(token)):
        raise _windows_security_error("open the current Windows access token")

    try:
        get_information = advapi32.GetTokenInformation
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_information.restype = wintypes.BOOL
        required = wintypes.DWORD()
        get_information(token, 1, None, 0, ctypes.byref(required))
        if ctypes.get_last_error() != 122 or required.value == 0:
            raise _windows_security_error("size the current Windows user SID")

        buffer = ctypes.create_string_buffer(required.value)
        if not get_information(
            token,
            1,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise _windows_security_error("read the current Windows user SID")

        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        convert_sid.restype = wintypes.BOOL
        output = ctypes.c_wchar_p()
        if not convert_sid(token_user.user.sid, ctypes.byref(output)):
            raise _windows_security_error("format the current Windows user SID")
        try:
            return output.value or ""
        finally:
            local_free(ctypes.cast(output, ctypes.c_void_p))
    finally:
        close_handle(token)


def _windows_security_error(action: str) -> ProjectRootSecurityError:
    error = ctypes.get_last_error()
    return ProjectRootSecurityError(f"Windows could not {action}: {ctypes.WinError(error)}")
