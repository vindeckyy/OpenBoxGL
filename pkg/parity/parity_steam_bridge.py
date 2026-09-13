"""Dependency-free Steam ``shortcuts.vdf`` bridge helpers.

The Steam shortcut file is a small binary KeyValues document.  Its wire
format uses four one-byte tags: ``0x00`` for a record, ``0x01`` for a
NUL-terminated string, ``0x02`` for an unsigned four-byte integer, and
``0x08`` to close a record.  Steam adds fields over time, so this module
keeps the complete ordered record tree instead of projecting entries onto a
fixed schema.  That makes an OpenBox update safe around fields it does not
know about.

Only this module owns the optional file mutation helpers.  They are
deliberately conservative: previews never write, malformed or oversized
files are rejected before a write is staged, no-op applies do not touch the
file, and remove writes an empty valid document rather than unlinking the
user's ``shortcuts.vdf``.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import tempfile
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Binary KeyValues tags used by shortcuts.vdf.  The aliases are intentionally
# public: callers building fixtures should not need to repeat magic numbers.
TYPE_RECORD = 0x00
TYPE_STRING = 0x01
TYPE_INT32 = 0x02
TYPE_END = 0x08
TAG_RECORD = TYPE_RECORD
TAG_INT32 = TYPE_INT32
TAG_STRING = TYPE_STRING
TAG_END = TYPE_END

ROOT_KEY = "shortcuts"
OPENBOX_APPID_MASK = 0x80000000

# A shortcuts file is normally a few kilobytes.  The generous bound prevents
# a corrupted path from becoming an unbounded allocation while allowing a
# large collection and arbitrary Steam metadata to survive a round trip.
MAX_SHORTCUTS_BYTES = 16 * 1024 * 1024
MAX_VDF_BYTES = MAX_SHORTCUTS_BYTES
MAX_FILE_BYTES = MAX_SHORTCUTS_BYTES
MAX_STRING_BYTES = 1024 * 1024
MAX_RECORDS = 100_000
MAX_DEPTH = 32


class SteamBridgeError(ValueError):
    """Base class for invalid Steam Bridge input."""


class ShortcutsCorruptError(SteamBridgeError):
    """Raised when a shortcuts.vdf byte stream is structurally invalid."""


class ShortcutsTooLargeError(SteamBridgeError):
    """Raised before parsing or writing an input/output over the size bound."""


class ShortcutsStaleError(SteamBridgeError):
    """Raised when an apply plan no longer describes the current file."""


# A few descriptive aliases make the exceptions convenient for integrations
# that use ``VDF`` terminology.
VDFError = SteamBridgeError
VDFCorruptError = ShortcutsCorruptError
VDFTooLargeError = ShortcutsTooLargeError
PreviewStaleError = ShortcutsStaleError


def _decode_text(value: bytes) -> str:
    """Decode VDF text without losing foreign non-UTF-8 bytes."""

    return bytes(value).decode("utf-8", errors="surrogateescape")


def _encode_text(value: Any, *, what: str = "text") -> bytes:
    """Encode public text values while retaining surrogateescaped bytes."""

    if isinstance(value, bytes):
        encoded = value
    elif isinstance(value, bytearray):
        encoded = bytes(value)
    elif isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="surrogateescape")
        except UnicodeEncodeError as error:
            raise SteamBridgeError(f"{what} is not valid UTF-8 text.") from error
    else:
        encoded = str(value).encode("utf-8")
    if b"\x00" in encoded:
        raise SteamBridgeError(f"{what} cannot contain NUL.")
    if len(encoded) > MAX_STRING_BYTES:
        raise ShortcutsTooLargeError(f"{what} is too large.")
    return encoded


@dataclass
class VDFRecord:
    """One ordered binary KeyValues record.

    ``tag`` is one of :data:`TYPE_RECORD`, :data:`TYPE_INT32`, or
    :data:`TYPE_STRING`.  A record value is a list of ``VDFRecord`` objects;
    an integer is represented as a Python ``int`` in the unsigned 32-bit
    range; and a string is represented as ``str``.  ``raw_key`` and
    ``raw_value`` are retained by the parser so a foreign byte string can be
    encoded back exactly, including bytes that are not valid UTF-8.
    """

    key: str
    tag: int
    value: Any
    raw_key: bytes | None = field(default=None, repr=False, compare=False)
    raw_value: bytes | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.key, (bytes, bytearray)):
            self.key = _decode_text(bytes(self.key))
        elif not isinstance(self.key, str):
            self.key = str(self.key)
        if self.tag == TYPE_STRING and isinstance(self.value, (bytes, bytearray)):
            if self.raw_value is None:
                self.raw_value = bytes(self.value)
            self.value = _decode_text(bytes(self.value))

    @property
    def type(self) -> int:
        """Alias for ``tag`` used by callers that call it a record type."""

        return self.tag

    @property
    def name(self) -> str:
        """Alias for the VDF key."""

        return self.key

    @property
    def children(self) -> list[VDFRecord]:
        """Return object children, or an empty list for scalar records."""

        return self.value if self.tag == TYPE_RECORD and isinstance(self.value, list) else []

    def clone(self, *, key: str | None = None) -> VDFRecord:
        """Copy this record and all nested children."""

        value = self.value
        if self.tag == TYPE_RECORD:
            value = [child.clone() for child in self.children]
        return VDFRecord(
            self.key if key is None else key,
            self.tag,
            value,
            raw_key=self.raw_key,
            raw_value=self.raw_value,
        )


@dataclass
class ShortcutsVDF(Mapping[str, Any]):
    """Lossless parsed shortcuts.vdf document.

    ``records`` contains the entries inside the root ``shortcuts`` object in
    their original order.  The object is intentionally lightweight and
    mapping-friendly: ``document.to_dict()`` returns the familiar
    ``{"shortcuts": {"0": {...}}}`` shape, while ``document.records`` is the
    lossless form used for edits.
    """

    records: list[VDFRecord] = field(default_factory=list)
    root_key: str = ROOT_KEY
    raw_root_key: bytes | None = field(default=None, repr=False, compare=False)
    source_empty: bool = False

    @property
    def entries(self) -> list[VDFRecord]:
        """Alias for the root records."""

        return self.records

    @property
    def shortcuts(self) -> dict[str, Any]:
        """Return the root entries as a mapping."""

        return _records_to_mapping(self.records)

    @property
    def data(self) -> dict[str, Any]:
        """Alias for :meth:`to_dict`."""

        return self.to_dict()

    @property
    def root(self) -> VDFRecord:
        """Return a record view of the root object."""

        return VDFRecord(self.root_key, TYPE_RECORD, self.records, raw_key=self.raw_root_key)

    def clone(self) -> ShortcutsVDF:
        return ShortcutsVDF(
            records=[record.clone() for record in self.records],
            root_key=self.root_key,
            raw_root_key=self.raw_root_key,
            source_empty=self.source_empty,
        )

    def to_dict(self) -> dict[str, Any]:
        return {self.root_key: _records_to_mapping(self.records)}

    as_dict = to_dict

    # Mapping-like conveniences keep the document pleasant to use in small
    # integrations without sacrificing its ordered record representation.
    def __getitem__(self, key: str) -> Any:
        if key == self.root_key:
            return _records_to_mapping(self.records)
        for record in self.records:
            if record.key == key:
                return _record_value_to_python(record)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        return key == self.root_key or any(record.key == key for record in self.records)

    def __iter__(self):
        yield self.root_key

    def __len__(self) -> int:
        return 1

    def keys(self):
        return (self.root_key,)

    def items(self):
        return ((self.root_key, _records_to_mapping(self.records)),)

    def values(self):
        return (_records_to_mapping(self.records),)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ShortcutsVDF):
            return self.root_key == other.root_key and self.records == other.records
        if isinstance(other, Mapping):
            return self.to_dict() == dict(other)
        return NotImplemented


# Readable aliases used by different callers.
VDFDocument = ShortcutsVDF
ShortcutDocument = ShortcutsVDF
Record = VDFRecord


class _Parser:
    def __init__(self, data: bytes, *, max_bytes: int) -> None:
        self.data = data
        self.limit = max_bytes
        self.position = 0
        self.record_count = 0

    def _require(self, length: int) -> None:
        if length < 0 or self.position + length > len(self.data):
            raise ShortcutsCorruptError("Truncated shortcuts.vdf record.")

    def _byte(self) -> int:
        self._require(1)
        value = self.data[self.position]
        self.position += 1
        return value

    def _cstring(self, *, what: str) -> tuple[str, bytes]:
        start = self.position
        end = self.data.find(b"\x00", start)
        if end < 0:
            raise ShortcutsCorruptError(f"Unterminated VDF {what}.")
        size = end - start
        if size > MAX_STRING_BYTES:
            raise ShortcutsTooLargeError(f"VDF {what} is too large.")
        raw = self.data[start:end]
        self.position = end + 1
        if what == "key" and not raw:
            raise ShortcutsCorruptError("VDF keys cannot be empty.")
        return _decode_text(raw), raw

    def _count_record(self) -> None:
        self.record_count += 1
        if self.record_count > MAX_RECORDS:
            raise ShortcutsTooLargeError("shortcuts.vdf contains too many records.")

    def _object_children(self, depth: int) -> list[VDFRecord]:
        if depth > MAX_DEPTH:
            raise ShortcutsTooLargeError("shortcuts.vdf nesting is too deep.")
        children: list[VDFRecord] = []
        while True:
            tag = self._byte()
            if tag == TYPE_END:
                return children
            if tag not in {TYPE_RECORD, TYPE_INT32, TYPE_STRING}:
                raise ShortcutsCorruptError(f"Unknown VDF record tag 0x{tag:02x}.")
            self._count_record()
            key, raw_key = self._cstring(what="key")
            if tag == TYPE_RECORD:
                value = self._object_children(depth + 1)
                children.append(VDFRecord(key, tag, value, raw_key=raw_key))
            elif tag == TYPE_INT32:
                self._require(4)
                value = struct.unpack_from("<I", self.data, self.position)[0]
                self.position += 4
                children.append(VDFRecord(key, tag, value, raw_key=raw_key))
            else:
                value, raw_value = self._cstring(what="string")
                children.append(VDFRecord(key, tag, value, raw_key=raw_key, raw_value=raw_value))


def _validate_limit(max_bytes: int) -> int:
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError) as error:
        raise ValueError("max_bytes must be a positive integer.") from error
    if limit <= 0:
        raise ValueError("max_bytes must be a positive integer.")
    return limit


def _source_bytes(source: bytes | bytearray | memoryview | str | os.PathLike[str], *, max_bytes: int) -> bytes:
    limit = _validate_limit(max_bytes)
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    else:
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError:
            raise
        if size > limit:
            raise ShortcutsTooLargeError("shortcuts.vdf is too large.")
        data = path.read_bytes()
    if len(data) > limit:
        raise ShortcutsTooLargeError("shortcuts.vdf is too large.")
    return data


def parse_shortcuts(
    source: bytes | bytearray | memoryview | str | os.PathLike[str],
    *,
    max_bytes: int = MAX_SHORTCUTS_BYTES,
) -> ShortcutsVDF:
    """Parse a shortcuts.vdf byte string or file path losslessly.

    An empty file is a valid empty fixture and remains empty when encoded
    again until a record is added.  Non-empty files must contain exactly one
    root ``shortcuts`` object with no trailing bytes.
    """

    data = _source_bytes(source, max_bytes=max_bytes)
    if not data:
        return ShortcutsVDF(source_empty=True)
    parser = _Parser(data, max_bytes=max_bytes)
    tag = parser._byte()
    if tag != TYPE_RECORD:
        raise ShortcutsCorruptError("shortcuts.vdf must begin with a record tag.")
    root_key, raw_root_key = parser._cstring(what="root key")
    if root_key.casefold() != ROOT_KEY:
        raise ShortcutsCorruptError("shortcuts.vdf has an unexpected root key.")
    records = parser._object_children(0)
    if parser.position != len(data):
        raise ShortcutsCorruptError("Trailing bytes after the shortcuts.vdf root.")
    return ShortcutsVDF(records=records, root_key=root_key, raw_root_key=raw_root_key)


def parse_shortcuts_vdf(source, *, max_bytes: int = MAX_SHORTCUTS_BYTES, as_dict: bool = False):
    """Compatibility spelling for :func:`parse_shortcuts`."""

    document = parse_shortcuts(source, max_bytes=max_bytes)
    return document.to_dict() if as_dict else document


def decode_shortcuts(source, *, max_bytes: int = MAX_SHORTCUTS_BYTES, as_dict: bool = False):
    return parse_shortcuts_vdf(source, max_bytes=max_bytes, as_dict=as_dict)


def parse_vdf(source, *, max_bytes: int = MAX_SHORTCUTS_BYTES, as_dict: bool = False):
    return parse_shortcuts_vdf(source, max_bytes=max_bytes, as_dict=as_dict)


def decode_vdf(source, *, max_bytes: int = MAX_SHORTCUTS_BYTES, as_dict: bool = False):
    return parse_shortcuts_vdf(source, max_bytes=max_bytes, as_dict=as_dict)


parse_binary_vdf = parse_shortcuts


def _record_value_to_python(record: VDFRecord) -> Any:
    if record.tag == TYPE_RECORD:
        return _records_to_mapping(record.children)
    if record.tag in {TYPE_INT32, TYPE_STRING}:
        return record.value
    raise SteamBridgeError(f"Unsupported VDF record tag 0x{record.tag:02x}.")


def _records_to_mapping(records: Iterable[VDFRecord]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for record in records:
        result[record.key] = _record_value_to_python(record)
    return result


def record_to_dict(record: VDFRecord) -> dict[str, Any]:
    """Return one record in the familiar mapping form."""

    return {record.key: _record_value_to_python(record)}


def _mapping_record_descriptor(value: Mapping[str, Any], *, key: str = "") -> VDFRecord | None:
    if "tag" not in value and "type" not in value:
        return None
    raw_tag = value.get("tag", value.get("type"))
    if isinstance(raw_tag, str):
        tags = {
            "record": TYPE_RECORD,
            "object": TYPE_RECORD,
            "int32": TYPE_INT32,
            "int": TYPE_INT32,
            "string": TYPE_STRING,
        }
        raw_tag = tags.get(raw_tag.casefold(), raw_tag)
    try:
        tag = int(raw_tag)
    except (TypeError, ValueError):
        raise SteamBridgeError("Invalid VDF record tag.") from None
    record_key = value.get("key", key)
    raw_value = value.get("value")
    if tag == TYPE_RECORD:
        children = _records_from_python(raw_value if raw_value is not None else {})
        return VDFRecord(str(record_key), tag, children)
    if tag == TYPE_INT32:
        return VDFRecord(str(record_key), tag, _coerce_int32(raw_value))
    if tag == TYPE_STRING:
        return VDFRecord(str(record_key), tag, "" if raw_value is None else raw_value)
    raise SteamBridgeError(f"Unsupported VDF record tag 0x{tag:02x}.")


def _record_from_python(key: str, value: Any) -> VDFRecord:
    if isinstance(value, VDFRecord):
        return value.clone(key=str(key))
    if isinstance(value, Mapping):
        descriptor = _mapping_record_descriptor(value, key=str(key))
        if descriptor is not None:
            return descriptor
        return VDFRecord(str(key), TYPE_RECORD, _records_from_python(value))
    if isinstance(value, bool):
        return VDFRecord(str(key), TYPE_INT32, int(value))
    if isinstance(value, int):
        return VDFRecord(str(key), TYPE_INT32, value)
    if isinstance(value, (list, tuple)):
        children = []
        for index, item in enumerate(value):
            if isinstance(item, VDFRecord):
                children.append(item.clone(key=item.key or str(index)))
            else:
                children.append(_record_from_python(str(index), item))
        return VDFRecord(str(key), TYPE_RECORD, children)
    return VDFRecord(str(key), TYPE_STRING, "" if value is None else value)


def _records_from_python(value: Any) -> list[VDFRecord]:
    if isinstance(value, ShortcutsVDF):
        return [record.clone() for record in value.records]
    if isinstance(value, Mapping):
        return [_record_from_python(str(key), item) for key, item in value.items()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        records: list[VDFRecord] = []
        for index, item in enumerate(value):
            if isinstance(item, VDFRecord):
                records.append(item.clone(key=item.key or str(index)))
            elif isinstance(item, Mapping) and ("tag" in item or "type" in item) and "value" in item:
                descriptor = _mapping_record_descriptor(item, key=str(index))
                if descriptor is not None:
                    records.append(descriptor)
            elif isinstance(item, Mapping) and (
                _looks_like_shortcut(item) or "appid" in {str(key).casefold() for key in item}
            ):
                records.append(_shortcut_record_from_mapping(item, key=str(index)))
            elif isinstance(item, Mapping) and len(item) == 1:
                key, child = next(iter(item.items()))
                records.append(_record_from_python(str(key), child))
            else:
                raise SteamBridgeError("A VDF record sequence must contain records or mappings.")
        return records
    raise SteamBridgeError("VDF records must be a mapping or sequence.")


def _document_from_python(value: Any) -> ShortcutsVDF:
    if isinstance(value, ShortcutsVDF):
        return value
    if isinstance(value, VDFRecord):
        if value.tag != TYPE_RECORD or value.key.casefold() != ROOT_KEY:
            raise SteamBridgeError("The VDF root must be a shortcuts record.")
        return ShortcutsVDF(
            records=[record.clone() for record in value.children],
            root_key=value.key,
            raw_root_key=value.raw_key,
        )
    if isinstance(value, Mapping):
        if ROOT_KEY in value:
            root_value = value[ROOT_KEY]
            records = _records_from_python(root_value)
            return ShortcutsVDF(records=records)
        records = _records_from_python(value)
        return ShortcutsVDF(records=records)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return ShortcutsVDF(records=_records_from_python(value))
    raise SteamBridgeError("A shortcuts document must be a ShortcutsVDF or mapping.")


def _coerce_int32(value: Any) -> int:
    if isinstance(value, bool):
        result = int(value)
    else:
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise SteamBridgeError("VDF int32 values must be integers.") from error
    if result < 0:
        result += 2**32
    if result < 0 or result > 0xFFFFFFFF:
        raise SteamBridgeError("VDF int32 value is outside the unsigned 32-bit range.")
    return result


def _append_checked(output: bytearray, value: bytes | bytearray, *, max_bytes: int) -> None:
    output.extend(value)
    if len(output) > max_bytes:
        raise ShortcutsTooLargeError("Encoded shortcuts.vdf is too large.")


def _encoded_key(record: VDFRecord) -> bytes:
    current = _encode_text(record.key, what="VDF key")
    if not current:
        raise SteamBridgeError("VDF keys cannot be empty.")
    if record.raw_key is not None and bytes(record.raw_key) == current:
        return bytes(record.raw_key)
    return current


def _encoded_string(record: VDFRecord) -> bytes:
    current = _encode_text(record.value, what="VDF string")
    if record.raw_value is not None and bytes(record.raw_value) == current:
        return bytes(record.raw_value)
    return current


def _encode_record(record: VDFRecord, output: bytearray, *, max_bytes: int, depth: int, count: list[int]) -> None:
    if not isinstance(record, VDFRecord):
        raise SteamBridgeError("VDF records must be VDFRecord instances.")
    if depth > MAX_DEPTH:
        raise ShortcutsTooLargeError("Encoded shortcuts.vdf nesting is too deep.")
    if record.tag not in {TYPE_RECORD, TYPE_INT32, TYPE_STRING}:
        raise SteamBridgeError(f"Unsupported VDF record tag 0x{record.tag:02x}.")
    count[0] += 1
    if count[0] > MAX_RECORDS:
        raise ShortcutsTooLargeError("Encoded shortcuts.vdf contains too many records.")
    _append_checked(output, bytes((record.tag,)), max_bytes=max_bytes)
    _append_checked(output, _encoded_key(record) + b"\x00", max_bytes=max_bytes)
    if record.tag == TYPE_RECORD:
        if not isinstance(record.value, list):
            raise SteamBridgeError("VDF record values must be lists of child records.")
        for child in record.value:
            _encode_record(child, output, max_bytes=max_bytes, depth=depth + 1, count=count)
        _append_checked(output, bytes((TYPE_END,)), max_bytes=max_bytes)
    elif record.tag == TYPE_INT32:
        _append_checked(output, struct.pack("<I", _coerce_int32(record.value)), max_bytes=max_bytes)
    else:
        _append_checked(output, _encoded_string(record) + b"\x00", max_bytes=max_bytes)


def encode_shortcuts(
    document: ShortcutsVDF | Mapping[str, Any] | Sequence[Any],
    *,
    max_bytes: int = MAX_SHORTCUTS_BYTES,
    preserve_empty: bool = True,
) -> bytes:
    """Encode a lossless document or familiar mapping to shortcuts.vdf bytes."""

    limit = _validate_limit(max_bytes)
    parsed = _document_from_python(document)
    if preserve_empty and parsed.source_empty and not parsed.records:
        return b""
    output = bytearray()
    root = VDFRecord(parsed.root_key, TYPE_RECORD, parsed.records, raw_key=parsed.raw_root_key)
    _encode_record(root, output, max_bytes=limit, depth=0, count=[0])
    return bytes(output)


def encode_shortcuts_vdf(document, *, max_bytes: int = MAX_SHORTCUTS_BYTES, preserve_empty: bool = True) -> bytes:
    return encode_shortcuts(document, max_bytes=max_bytes, preserve_empty=preserve_empty)


def encode_vdf(document, *, max_bytes: int = MAX_SHORTCUTS_BYTES, preserve_empty: bool = True) -> bytes:
    return encode_shortcuts(document, max_bytes=max_bytes, preserve_empty=preserve_empty)


def encode_binary_vdf(document, *, max_bytes: int = MAX_SHORTCUTS_BYTES, preserve_empty: bool = True) -> bytes:
    return encode_shortcuts(document, max_bytes=max_bytes, preserve_empty=preserve_empty)


encode_binary_shortcuts = encode_shortcuts


def write_shortcuts_bytes(path: str | os.PathLike[str], data: bytes, *, mode: int | None = None) -> Path:
    """Atomically replace one regular shortcuts file with already validated bytes.

    This low-level helper never unlinks the destination.  A temporary file is
    created beside it and replaced only after the complete payload is flushed.
    Symlink destinations and non-regular existing paths are rejected.
    """

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like.")
    payload = bytes(data)
    if len(payload) > MAX_SHORTCUTS_BYTES:
        raise ShortcutsTooLargeError("Encoded shortcuts.vdf is too large.")
    # Validate before creating a staging file.  The high-level operations
    # already encode a checked document, but keeping this boundary safe avoids
    # turning the public low-level helper into a raw corrupt-file writer.
    parse_shortcuts(payload)
    target = Path(path)
    if target.is_symlink():
        raise SteamBridgeError("Refusing to replace a symlink shortcuts.vdf.")
    if target.exists() and not target.is_file():
        raise SteamBridgeError("Refusing to replace a non-file shortcuts.vdf.")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    target_mode = mode
    if target_mode is None and target.exists():
        target_mode = stat.S_IMODE(target.stat().st_mode)
    if target_mode is None:
        target_mode = 0o600
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, target_mode)
        os.replace(temporary, target)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        # Only the private staging path is cleaned up here; the destination is
        # never unlinked, even if an fsync or replace fails.
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def read_shortcuts(path: str | os.PathLike[str], *, max_bytes: int = MAX_SHORTCUTS_BYTES, missing_ok: bool = False) -> ShortcutsVDF:
    target = Path(path)
    if missing_ok and not target.exists() and not target.is_symlink():
        return ShortcutsVDF(source_empty=True)
    return parse_shortcuts(target, max_bytes=max_bytes)


load_shortcuts = read_shortcuts
read_shortcuts_vdf = read_shortcuts


def appid_for(exe: str | os.PathLike[str], name: str) -> int:
    """Return Steam's deterministic non-Steam app id for an OpenBox shortcut."""

    exe_bytes = _encode_text(os.fspath(exe) if isinstance(exe, os.PathLike) else exe, what="executable")
    name_bytes = _encode_text(name, what="shortcut name")
    return (zlib.crc32(exe_bytes + name_bytes) & 0xFFFFFFFF) | OPENBOX_APPID_MASK


def deterministic_appid(exe, name) -> int:
    return appid_for(exe, name)


def shortcut_appid(exe, name) -> int:
    return appid_for(exe, name)


def appid(exe, name) -> int:
    return appid_for(exe, name)


compute_appid = appid_for
synthetic_appid = appid_for
steam_appid_for = appid_for
appid_for_shortcut = appid_for
crc32_appid = appid_for


def _field(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    lowered = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return default


def make_shortcut(
    name: str,
    exe: str | os.PathLike[str],
    *,
    start_dir: str | os.PathLike[str] = "",
    icon: str | os.PathLike[str] = "",
    shortcut_path: str = "",
    launch_options: str = "",
    is_hidden: int | bool = 0,
    allow_desktop_config: int | bool = 1,
    openvr: int | bool = 0,
    devkit: int | bool = 0,
    devkit_game_id: str = "",
    last_play_time: int = 0,
    tags: Mapping[str, Any] | Sequence[Any] | None = None,
    appid_value: int | None = None,
    appid: int | None = None,
    openbox_game_id: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard Steam shortcut mapping for an OpenBox launcher.

    ``appid_value`` is optional for fixture/import use; normal bridge entries
    use :func:`appid_for` and therefore always carry the high-bit marker.
    Unknown ``extra`` fields are retained and encoded after standard fields.
    """

    name_text = _decode_text(_encode_text(name, what="shortcut name"))
    exe_text = _decode_text(_encode_text(os.fspath(exe) if isinstance(exe, os.PathLike) else exe, what="executable"))
    if not name_text.strip() or not exe_text.strip():
        raise SteamBridgeError("A bridge shortcut requires a name and executable.")
    if appid_value is not None and appid is not None and _coerce_int32(appid_value) != _coerce_int32(appid):
        raise SteamBridgeError("appid and appid_value disagree.")
    selected_appid = appid_value if appid_value is not None else appid
    fields: dict[str, Any] = {
        "appid": appid_for(exe_text, name_text) if selected_appid is None else _coerce_int32(selected_appid),
        "AppName": name_text,
        "Exe": exe_text,
        "StartDir": os.fspath(start_dir) if isinstance(start_dir, os.PathLike) else str(start_dir or ""),
        "icon": os.fspath(icon) if isinstance(icon, os.PathLike) else str(icon or ""),
        "ShortcutPath": str(shortcut_path or ""),
        "LaunchOptions": str(launch_options or ""),
        "IsHidden": int(bool(is_hidden)),
        "AllowDesktopConfig": int(bool(allow_desktop_config)),
        "OpenVR": int(bool(openvr)),
        "Devkit": int(bool(devkit)),
        "DevkitGameID": str(devkit_game_id or ""),
        "LastPlayTime": _coerce_int32(last_play_time),
        "tags": dict(tags) if isinstance(tags, Mapping) else list(tags or ()),
    }
    if str(openbox_game_id or "").strip():
        fields["OpenBoxGameID"] = str(openbox_game_id).strip()
    for key, value in (extra or {}).items():
        if key not in fields:
            fields[str(key)] = value
    return fields


build_shortcut = make_shortcut
make_bridge_shortcut = make_shortcut


def shortcut_from_game(
    game: Mapping[str, Any],
    *,
    launcher_exe: str | os.PathLike[str] | None = None,
    openbox_exe: str | os.PathLike[str] | None = None,
    launch_options: str | None = None,
) -> dict[str, Any]:
    """Project one OpenBox game row onto a Steam shortcut mapping.

    A caller that supplies ``launcher_exe``/``openbox_exe`` gets the natural
    ``--play <game_id>`` default.  Existing explicit ``launch_options`` win.
    """

    if not isinstance(game, Mapping):
        raise SteamBridgeError("game must be a mapping.")
    name = _field(game, "AppName", "name", default="")
    executable = launcher_exe or openbox_exe or _field(game, "Exe", "exe", default=None)
    if executable is None:
        executable = _field(game, "path", default="")
    options = launch_options
    if options is None:
        options = _field(game, "LaunchOptions", "launch_options", default="")
    if not options and (launcher_exe or openbox_exe):
        game_id = _field(game, "game_id", "id", default="")
        if game_id != "":
            options = f"--play {game_id}"
    return make_shortcut(
        name,
        executable,
        start_dir=_field(game, "StartDir", "start_dir", default=""),
        icon=_field(game, "icon", "Icon", default=""),
        shortcut_path=_field(game, "ShortcutPath", "shortcut_path", default=""),
        launch_options=options or "",
        is_hidden=_field(game, "IsHidden", "is_hidden", default=0),
        allow_desktop_config=_field(game, "AllowDesktopConfig", "allow_desktop_config", default=1),
        openvr=_field(game, "OpenVR", "openvr", default=0),
        devkit=_field(game, "Devkit", "devkit", default=0),
        devkit_game_id=_field(game, "DevkitGameID", "devkit_game_id", default=""),
        last_play_time=_field(game, "LastPlayTime", "last_play_time", default=0),
        tags=_field(game, "tags", "Tags", default=None),
        openbox_game_id=str(_field(game, "game_id", "id", default="") or ""),
    )


game_to_shortcut = shortcut_from_game
entry_for_game = shortcut_from_game


def _looks_like_shortcut(value: Mapping[str, Any]) -> bool:
    keys = {str(key).casefold() for key in value}
    return bool(keys & {"appname", "name", "exe", "path", "launchoptions", "launch_options", "game_id"})


def _shortcut_record_from_mapping(value: Mapping[str, Any], *, key: str = "") -> VDFRecord:
    if not _looks_like_shortcut(value) and "appid" not in {str(item).casefold() for item in value}:
        # A mapping with no shortcut hints is still accepted as a raw VDF
        # record, which is useful for callers adding a typed foreign entry.
        descriptor = _mapping_record_descriptor(value, key=key)
        if descriptor is not None:
            return descriptor
    name = _field(value, "AppName", "name", default="")
    exe = _field(value, "Exe", "exe", default=None)
    if exe is None:
        exe = _field(value, "path", default="")
    name_text = _decode_text(_encode_text(name, what="shortcut name"))
    exe_text = _decode_text(_encode_text(exe, what="executable"))
    explicit_appid = _field(value, "appid", "app_id", "steam_app_id", default=None)
    appid_value = appid_for(exe_text, name_text) if explicit_appid is None else _coerce_int32(explicit_appid)
    standard = make_shortcut(
        name_text,
        exe_text,
        start_dir=_field(value, "StartDir", "start_dir", default=""),
        icon=_field(value, "icon", "Icon", default=""),
        shortcut_path=_field(value, "ShortcutPath", "shortcut_path", default=""),
        launch_options=_field(value, "LaunchOptions", "launch_options", default=""),
        is_hidden=_field(value, "IsHidden", "is_hidden", default=0),
        allow_desktop_config=_field(value, "AllowDesktopConfig", "allow_desktop_config", default=1),
        openvr=_field(value, "OpenVR", "openvr", default=0),
        devkit=_field(value, "Devkit", "devkit", default=0),
        devkit_game_id=_field(value, "DevkitGameID", "devkit_game_id", default=""),
        last_play_time=_field(value, "LastPlayTime", "last_play_time", default=0),
        tags=_field(value, "tags", "Tags", default=None),
        appid_value=appid_value,
        extra={
            str(k): v
            for k, v in value.items()
            if str(k)
            not in {
                "appid",
                "app_id",
                "steam_app_id",
                "AppName",
                "name",
                "Exe",
                "exe",
                "path",
                "StartDir",
                "start_dir",
                "icon",
                "Icon",
                "ShortcutPath",
                "shortcut_path",
                "LaunchOptions",
                "launch_options",
                "IsHidden",
                "is_hidden",
                "AllowDesktopConfig",
                "allow_desktop_config",
                "OpenVR",
                "openvr",
                "Devkit",
                "devkit",
                "DevkitGameID",
                "devkit_game_id",
                "LastPlayTime",
                "last_play_time",
                "tags",
                "Tags",
            }
        },
    )
    return VDFRecord(str(key), TYPE_RECORD, _records_from_python(standard))


def _normalise_shortcuts(value: Any) -> list[VDFRecord]:
    if isinstance(value, ShortcutsVDF):
        return [record.clone() for record in value.records]
    if isinstance(value, VDFRecord):
        return [value.clone()]
    if isinstance(value, Mapping):
        if ROOT_KEY in value:
            value = value[ROOT_KEY]
        elif _looks_like_shortcut(value) or "appid" in {str(item).casefold() for item in value}:
            return [_shortcut_record_from_mapping(value)]
        if isinstance(value, Mapping):
            records = []
            for key, item in value.items():
                if isinstance(item, VDFRecord):
                    records.append(item.clone(key=str(key)))
                elif isinstance(item, Mapping):
                    records.append(_shortcut_record_from_mapping(item, key=str(key)))
                else:
                    records.append(_record_from_python(str(key), item))
            return records
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        records = []
        for item in value:
            if isinstance(item, VDFRecord):
                records.append(item.clone())
            elif isinstance(item, Mapping):
                if "key" in item and ("tag" in item or "type" in item) and "value" in item:
                    descriptor = _mapping_record_descriptor(item)
                    if descriptor is not None:
                        records.append(descriptor)
                else:
                    records.append(_shortcut_record_from_mapping(item))
            else:
                raise SteamBridgeError("Shortcut entries must be mappings or VDFRecord instances.")
        return records
    raise SteamBridgeError("Shortcut entries must be a mapping or sequence.")


def _record_fields(record: VDFRecord) -> dict[str, VDFRecord]:
    if record.tag != TYPE_RECORD:
        return {}
    return {child.key: child for child in record.children}


def _record_appid(record: VDFRecord) -> int | None:
    field = _record_fields(record).get("appid")
    if field is None:
        return None
    if field.tag == TYPE_INT32:
        try:
            return _coerce_int32(field.value)
        except SteamBridgeError:
            return None
    if field.tag == TYPE_STRING:
        try:
            return _coerce_int32(field.value)
        except SteamBridgeError:
            return None
    return None


def _record_text(record: VDFRecord, *keys: str) -> str:
    fields = _record_fields(record)
    for key in keys:
        field = fields.get(key)
        if field is not None and field.tag == TYPE_STRING:
            return str(field.value)
    return ""


def _record_openbox_game_id(record: VDFRecord) -> str:
    """Return the stable OpenBox identity carried by a bridge record."""

    fields = _record_fields(record)
    for key, record_field in fields.items():
        if key.casefold() in {"openboxgameid", "openbox_game_id", "game_id", "id"}:
            if record_field.tag in {TYPE_STRING, TYPE_INT32}:
                value = str(record_field.value or "").strip()
                if value:
                    return value
    return ""


def _record_with_appid(record: VDFRecord, appid_value: int) -> VDFRecord:
    """Clone a shortcut while replacing its appid without stale raw bytes."""

    selected = _coerce_int32(appid_value)
    children: list[VDFRecord] = []
    replaced = False
    for child in record.children:
        if child.key.casefold() != "appid":
            children.append(child.clone())
            continue
        replaced = True
        if child.tag == TYPE_STRING:
            children.append(VDFRecord(child.key, TYPE_STRING, str(selected), raw_key=child.raw_key))
        else:
            children.append(VDFRecord(child.key, TYPE_INT32, selected, raw_key=child.raw_key))
    if not replaced:
        children.insert(0, VDFRecord("appid", TYPE_INT32, selected))
    return VDFRecord(record.key, TYPE_RECORD, children, raw_key=record.raw_key)


def _collision_appid(record: VDFRecord, occupied: set[int]) -> int:
    """Derive a stable high-bit appid for a same-title identity collision."""

    seed = b"\x00".join(
        _encode_text(value, what=label)
        for value, label in (
            (_record_text(record, "Exe", "exe"), "executable"),
            (_record_text(record, "AppName", "name"), "shortcut name"),
            (_record_openbox_game_id(record), "OpenBox game id"),
        )
    )
    salt = 0
    while True:
        suffix = b"" if salt == 0 else b"\x00" + str(salt).encode("ascii")
        candidate = (zlib.crc32(seed + suffix) & 0xFFFFFFFF) | OPENBOX_APPID_MASK
        if candidate not in occupied:
            return candidate
        salt += 1


def is_openbox_appid(value: Any) -> bool:
    try:
        return bool(_coerce_int32(value) & OPENBOX_APPID_MASK)
    except SteamBridgeError:
        return False


def is_bridge_shortcut(record: VDFRecord | Mapping[str, Any]) -> bool:
    if isinstance(record, Mapping):
        record = _shortcut_record_from_mapping(record)
    if not isinstance(record, VDFRecord):
        return False
    fields = _record_fields(record)
    for actual, record_field in fields.items():
        if actual.casefold() in {"openboxgameid", "openbox_game_id"}:
            return record_field.tag in {TYPE_STRING, TYPE_INT32} and bool(str(record_field.value or "").strip())
    return False


def _record_semantically_equal(left: VDFRecord, right: VDFRecord) -> bool:
    if left.key != right.key or left.tag != right.tag:
        return False
    if left.tag == TYPE_RECORD:
        return len(left.children) == len(right.children) and all(
            _record_semantically_equal(a, b) for a, b in zip(left.children, right.children, strict=True)
        )
    return left.value == right.value


def _merge_record(current: VDFRecord, proposed: VDFRecord) -> VDFRecord:
    """Update known fields while retaining current foreign children/order."""

    if current.tag != TYPE_RECORD or proposed.tag != TYPE_RECORD:
        return proposed.clone(key=current.key)
    children = [child.clone() for child in current.children]
    positions: dict[str, int] = {}
    for index, child in enumerate(children):
        positions.setdefault(child.key, index)
    for child in proposed.children:
        index = positions.get(child.key)
        if index is None:
            positions[child.key] = len(children)
            children.append(child.clone())
        else:
            children[index] = child.clone()
    return VDFRecord(current.key, TYPE_RECORD, children, raw_key=current.raw_key)


def _next_record_key(records: Sequence[VDFRecord]) -> str:
    used = {record.key for record in records}
    numeric = [int(key) for key in used if key.isdigit()]
    candidate = max(numeric, default=-1) + 1
    while str(candidate) in used:
        candidate += 1
    return str(candidate)


def _public_record(record: VDFRecord) -> dict[str, Any]:
    return {
        "key": record.key,
        "tag": record.tag,
        "value": _public_value(record.value, record.tag),
    }


def _public_value(value: Any, tag: int) -> Any:
    if tag == TYPE_RECORD:
        return [_public_record(item) for item in value or []]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return _decode_text(bytes(value))
    return value


def _records_from_public(records: Any) -> list[VDFRecord]:
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes, bytearray)):
        result = []
        for item in records:
            if isinstance(item, VDFRecord):
                result.append(item.clone())
            elif isinstance(item, Mapping):
                descriptor = _mapping_record_descriptor(item)
                if descriptor is None:
                    raise SteamBridgeError("Invalid public VDF record descriptor.")
                result.append(descriptor)
            else:
                raise SteamBridgeError("Invalid public VDF record descriptor.")
        return result
    raise SteamBridgeError("Invalid public VDF record list.")


def _public_target(value: Any) -> Any:
    """Make a remove target safe to carry in a serialized preview plan."""

    if isinstance(value, VDFRecord):
        return {
            "appid": _record_appid(value),
            "name": _record_text(value, "AppName", "name"),
            "exe": _record_text(value, "Exe", "exe"),
        }
    if isinstance(value, Mapping):
        return {str(key): _public_target(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_target(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return _decode_text(bytes(value))
    return value


def _file_state(path: str | os.PathLike[str], *, max_bytes: int) -> tuple[Path, bytes, bool]:
    target = Path(path)
    if target.is_symlink():
        raise SteamBridgeError("Refusing to use a symlink shortcuts.vdf.")
    if not target.exists():
        return target, b"", False
    if not target.is_file():
        raise SteamBridgeError("shortcuts.vdf path is not a regular file.")
    data = _source_bytes(target, max_bytes=max_bytes)
    return target, data, True


def _digest(data: bytes, exists: bool) -> str | None:
    return hashlib.sha256(data).hexdigest() if exists else None


def _plan_base(document: ShortcutsVDF, data: bytes, exists: bool, path: Path, *, operation: str) -> dict[str, Any]:
    return {
        "format": "openbox-steam-bridge-preview",
        "version": 1,
        "preview": True,
        "operation": operation,
        "path": str(path),
        "base_exists": exists,
        "base_sha256": _digest(data, exists),
        "current_count": len(document.records),
        "foreign_count": sum(1 for record in document.records if not is_bridge_shortcut(record)),
    }


def _apply_plan_for_records(
    document: ShortcutsVDF,
    data: bytes,
    exists: bool,
    path: Path,
    proposed: Sequence[VDFRecord],
    *,
    operation: str = "apply",
) -> dict[str, Any]:
    current = [record.clone() for record in document.records]
    output = [record.clone() for record in current]
    added: list[VDFRecord] = []
    updated: list[dict[str, Any]] = []
    unchanged = 0
    by_appid: dict[int, int] = {}
    by_game_id: dict[str, int] = {}
    for index, record in enumerate(output):
        record_id = _record_appid(record)
        if record_id is not None:
            by_appid.setdefault(record_id, index)
        game_id = _record_openbox_game_id(record) if is_bridge_shortcut(record) else ""
        if game_id:
            by_game_id.setdefault(game_id, index)
    seen: set[int] = set()
    seen_game_ids: set[str] = set()
    normalized: list[VDFRecord] = []
    for record in proposed:
        item = record.clone()
        record_id = _record_appid(item)
        if record_id is None:
            raise SteamBridgeError("Every bridge shortcut must have an appid.")
        game_id = _record_openbox_game_id(item)
        if game_id and game_id in seen_game_ids:
            raise SteamBridgeError(f"Duplicate bridge game id: {game_id}.")
        index = by_game_id.get(game_id) if game_id else None
        if index is not None:
            # OpenBoxGameID is the stable identity. Preserve the appid that
            # is already attached to that identity even when a fresh title /
            # executable projection produces the base CRC again.
            existing_appid = _record_appid(output[index])
            if existing_appid is not None:
                record_id = existing_appid
                item = _record_with_appid(item, record_id)
        appid_owner = by_appid.get(record_id)
        if index is not None and appid_owner is not None and appid_owner != index and is_bridge_shortcut(output[appid_owner]):
            # A title/executable collision is harmless only when the stable
            # OpenBox identity also matches.  Give the new identity its own
            # deterministic Steam appid before updating or adding it.
            occupied = {value for value in by_appid if value != record_id}
            record_id = _collision_appid(item, occupied)
            item = _record_with_appid(item, record_id)
        elif index is None:
            index = appid_owner
            if index is not None and is_bridge_shortcut(output[index]):
                existing_game_id = _record_openbox_game_id(output[index])
                if not game_id or existing_game_id != game_id:
                    occupied = set(by_appid)
                    record_id = _collision_appid(item, occupied)
                    item = _record_with_appid(item, record_id)
                    index = None
            elif index is not None and not _record_semantically_equal(output[index], item):
                # A low-bit id belongs to Steam/another tool unless the caller
                # supplied the exact foreign record.  Never overwrite it during
                # an OpenBox add/update pass.
                index = None
        if record_id in seen:
            raise SteamBridgeError(f"Duplicate bridge appid: {record_id}.")
        seen.add(record_id)
        if game_id:
            seen_game_ids.add(game_id)
        normalized.append(item)
        if index is not None and not is_bridge_shortcut(output[index]) and not _record_semantically_equal(output[index], item):
            # A low-bit id belongs to Steam/another tool unless the caller
            # supplied the exact foreign record.  Never overwrite it during
            # an OpenBox add/update pass.
            index = None
        if index is None:
            item.key = _next_record_key(output)
            added.append(item)
            by_appid[record_id] = len(output)
            output.append(item.clone())
        elif _record_semantically_equal(output[index], item):
            unchanged += 1
        else:
            before = output[index]
            merged = _merge_record(before, item)
            output[index] = merged
            if _record_semantically_equal(before, merged):
                unchanged += 1
            else:
                updated.append({"before": _public_record(before), "after": _public_record(merged)})
    changed = len(output) != len(current) or any(
        not _record_semantically_equal(left, right) for left, right in zip(output, current, strict=True)
    )
    plan = _plan_base(document, data, exists, path, operation=operation)
    plan.update(
        {
            "desired_count": len(normalized),
            "added": len(added),
            "updated": len(updated),
            "removed": 0,
            "unchanged": unchanged,
            "changed": changed,
            "additions": [_public_record(record) for record in added],
            "updates": updated,
            "removals": [],
            "desired_records": [_public_record(record) for record in normalized],
            "result_records": [_public_record(record) for record in output],
        }
    )
    return plan


def preview_shortcuts(
    path: str | os.PathLike[str],
    shortcuts: Any = (),
    *,
    max_bytes: int = MAX_SHORTCUTS_BYTES,
) -> dict[str, Any]:
    """Read-only preview of adding/updating bridge shortcuts at ``path``."""

    target, data, exists = _file_state(path, max_bytes=max_bytes)
    document = parse_shortcuts(data, max_bytes=max_bytes)
    proposed = _normalise_shortcuts(shortcuts)
    return _apply_plan_for_records(document, data, exists, target, proposed)


preview = preview_shortcuts
preview_bridge = preview_shortcuts


def _target_descriptors(targets: Any) -> list[Any]:
    if isinstance(targets, Sequence) and not isinstance(targets, (str, bytes, bytearray)):
        return list(targets)
    return [targets]


def _record_matches_target(record: VDFRecord, target: Any) -> bool:
    if not is_bridge_shortcut(record):
        return False
    record_id = _record_appid(record)
    fields = _record_fields(record)
    name = _record_text(record, "AppName", "name")
    if isinstance(target, VDFRecord):
        target = {"appid": _record_appid(target), "name": _record_text(target, "AppName", "name"), "exe": _record_text(target, "Exe", "exe")}
    if isinstance(target, Mapping):
        explicit = _field(target, "appid", "app_id", "steam_app_id", default=None)
        if explicit is not None:
            try:
                if record_id == _coerce_int32(explicit):
                    return True
            except SteamBridgeError:
                pass
        target_name = _field(target, "AppName", "name", default=None)
        target_exe = _field(target, "Exe", "exe", default=None)
        if target_name is not None and target_exe is not None:
            try:
                return record_id == appid_for(target_exe, target_name)
            except SteamBridgeError:
                return False
        game_id = _field(target, "OpenBoxGameId", "OpenBoxGameID", "openbox_game_id", "game_id", "id", default=None)
        if game_id is not None:
            for key in ("OpenBoxGameId", "OpenBoxGameID", "openbox_game_id", "game_id", "id"):
                candidate = fields.get(key)
                if candidate is not None and str(candidate.value) == str(game_id):
                    return True
        return False
    if isinstance(target, (int, bool)):
        return record_id == _coerce_int32(target)
    text = str(target)
    try:
        if text.isdigit() and record_id == _coerce_int32(text):
            return True
    except SteamBridgeError:
        pass
    return text == name


def _remove_plan(
    document: ShortcutsVDF,
    data: bytes,
    exists: bool,
    path: Path,
    targets: Any,
) -> dict[str, Any]:
    descriptors = _target_descriptors(targets)
    if not descriptors:
        raise SteamBridgeError("At least one shortcut target is required.")
    removed_records = []
    kept_records = []
    for record in document.records:
        if any(_record_matches_target(record, target) for target in descriptors):
            removed_records.append(record)
        else:
            kept_records.append(record)
    plan = _plan_base(document, data, exists, path, operation="remove")
    plan.update(
        {
            "desired_count": len(kept_records),
            "added": 0,
            "updated": 0,
            "removed": len(removed_records),
            "unchanged": len(kept_records),
            "changed": bool(removed_records),
            "additions": [],
            "updates": [],
            "removals": [_public_record(record) for record in removed_records],
            "desired_records": [_public_record(record) for record in kept_records],
            "result_records": [_public_record(record) for record in kept_records],
            "targets": [_public_target(target) for target in descriptors],
        }
    )
    return plan


def preview_remove(
    path: str | os.PathLike[str],
    targets: Any,
    *,
    max_bytes: int = MAX_SHORTCUTS_BYTES,
) -> dict[str, Any]:
    """Read-only preview of removing selected OpenBox bridge entries."""

    target, data, exists = _file_state(path, max_bytes=max_bytes)
    document = parse_shortcuts(data, max_bytes=max_bytes)
    return _remove_plan(document, data, exists, target, targets)


remove_preview = preview_remove


def build_bridge_plan(
    path: str | os.PathLike[str],
    shortcuts: Any = (),
    *,
    operation: str = "apply",
    max_bytes: int = MAX_SHORTCUTS_BYTES,
) -> dict[str, Any]:
    """Build an apply or remove preview using the plan-oriented API spelling."""

    if operation == "remove":
        return preview_remove(path, shortcuts, max_bytes=max_bytes)
    if operation != "apply":
        raise SteamBridgeError("Steam Bridge plan operation must be apply or remove.")
    return preview_shortcuts(path, shortcuts, max_bytes=max_bytes)


def _commit_plan(
    path: str | os.PathLike[str],
    plan: Mapping[str, Any],
    *,
    max_bytes: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or plan.get("format") != "openbox-steam-bridge-preview":
        raise SteamBridgeError("Invalid Steam Bridge preview plan.")
    if "base_sha256" not in plan or "base_exists" not in plan:
        raise SteamBridgeError("Steam Bridge preview plan has no base fingerprint.")
    target, data, exists = _file_state(path, max_bytes=max_bytes)
    plan_path = str(plan.get("path") or "")
    if plan_path and Path(plan_path).expanduser().resolve(strict=False) != target.expanduser().resolve(strict=False):
        raise SteamBridgeError("Steam Bridge plan targets a different shortcuts.vdf path.")
    actual_digest = _digest(data, exists)
    expected_digest = plan.get("base_sha256")
    if actual_digest != expected_digest or bool(plan.get("base_exists")) != exists:
        raise ShortcutsStaleError("shortcuts.vdf changed after the preview.")
    operation = str(plan.get("operation") or "apply")
    if operation not in {"apply", "remove"}:
        raise SteamBridgeError("Invalid Steam Bridge plan operation.")
    document = parse_shortcuts(data, max_bytes=max_bytes)
    if operation == "remove":
        # The desired list in a remove plan is already the complete kept set;
        # recompute it after the stale check so a caller cannot turn a remove
        # preview into an arbitrary file rewrite by editing its kept-record
        # list.
        result_plan = _remove_plan(document, data, exists, target, plan.get("targets", []))
        records = _records_from_public(result_plan.get("desired_records", []))
    else:
        records = _records_from_public(plan.get("desired_records", []))
        result_plan = _apply_plan_for_records(document, data, exists, target, records)
    changed = bool(result_plan.get("changed"))
    if not changed or dry_run:
        result = dict(result_plan)
        result.update({"applied": False, "written": False})
        return result
    result_records = _records_from_public(result_plan.get("result_records", []))
    result_document = ShortcutsVDF(
        records=result_records,
        root_key=document.root_key,
        raw_root_key=document.raw_root_key,
    )
    encoded = encode_shortcuts(result_document, max_bytes=max_bytes, preserve_empty=False)
    write_shortcuts_bytes(target, encoded)
    result = dict(result_plan)
    result.update({"applied": True, "written": True})
    return result


def apply_shortcuts(
    path: str | os.PathLike[str],
    shortcuts: Any = None,
    *,
    plan: Mapping[str, Any] | None = None,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_SHORTCUTS_BYTES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a preview or shortcut collection with an atomic, non-destructive write."""

    if plan is None and isinstance(shortcuts, Mapping) and shortcuts.get("format") == "openbox-steam-bridge-preview":
        plan = shortcuts
    if plan is None:
        plan = preview_shortcuts(path, () if shortcuts is None else shortcuts, max_bytes=max_bytes)
    if expected_sha256 is not None:
        plan = dict(plan)
        plan["base_sha256"] = expected_sha256
    return _commit_plan(path, plan, max_bytes=max_bytes, dry_run=dry_run)


apply = apply_shortcuts
apply_bridge = apply_shortcuts
commit = apply_shortcuts


def apply_bridge_plan(
    plan: Mapping[str, Any],
    path: str | os.PathLike[str] | None = None,
    *,
    max_bytes: int = MAX_SHORTCUTS_BYTES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a serialized bridge plan, defaulting to the previewed path."""

    target = path if path is not None else plan.get("path") if isinstance(plan, Mapping) else None
    if not target:
        raise SteamBridgeError("A shortcuts.vdf path is required for plan apply.")
    return _commit_plan(target, plan, max_bytes=max_bytes, dry_run=dry_run)


apply_plan = apply_bridge_plan
plan_bridge = build_bridge_plan


def remove_shortcuts(
    path: str | os.PathLike[str],
    targets: Any,
    *,
    apply: bool = True,
    max_bytes: int = MAX_SHORTCUTS_BYTES,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove selected bridge entries without unlinking the shortcuts file."""

    plan = preview_remove(path, targets, max_bytes=max_bytes)
    if not apply:
        return {key: value for key, value in plan.items() if not str(key).startswith("_")}
    return _commit_plan(path, plan, max_bytes=max_bytes, dry_run=dry_run)


remove = remove_shortcuts
remove_bridge = remove_shortcuts
remove_plan = preview_remove


def build_document(records: Sequence[VDFRecord] | Iterable[VDFRecord] = ()) -> ShortcutsVDF:
    """Small convenience constructor for fixture and integration code."""

    return ShortcutsVDF(records=[record.clone() for record in records])


__all__ = [
    "TYPE_RECORD",
    "TYPE_INT32",
    "TYPE_STRING",
    "TYPE_END",
    "TAG_RECORD",
    "TAG_INT32",
    "TAG_STRING",
    "TAG_END",
    "ROOT_KEY",
    "OPENBOX_APPID_MASK",
    "MAX_SHORTCUTS_BYTES",
    "MAX_VDF_BYTES",
    "MAX_FILE_BYTES",
    "MAX_STRING_BYTES",
    "MAX_RECORDS",
    "MAX_DEPTH",
    "SteamBridgeError",
    "ShortcutsCorruptError",
    "ShortcutsTooLargeError",
    "ShortcutsStaleError",
    "VDFError",
    "VDFCorruptError",
    "VDFTooLargeError",
    "PreviewStaleError",
    "VDFRecord",
    "Record",
    "ShortcutsVDF",
    "ShortcutDocument",
    "VDFDocument",
    "parse_shortcuts",
    "parse_shortcuts_vdf",
    "decode_shortcuts",
    "parse_vdf",
    "decode_vdf",
    "parse_binary_vdf",
    "encode_shortcuts",
    "encode_shortcuts_vdf",
    "encode_vdf",
    "encode_binary_vdf",
    "encode_binary_shortcuts",
    "read_shortcuts",
    "read_shortcuts_vdf",
    "load_shortcuts",
    "write_shortcuts_bytes",
    "record_to_dict",
    "appid_for",
    "deterministic_appid",
    "shortcut_appid",
    "appid",
    "compute_appid",
    "synthetic_appid",
    "steam_appid_for",
    "appid_for_shortcut",
    "crc32_appid",
    "make_shortcut",
    "build_shortcut",
    "make_bridge_shortcut",
    "shortcut_from_game",
    "game_to_shortcut",
    "entry_for_game",
    "is_openbox_appid",
    "is_bridge_shortcut",
    "preview_shortcuts",
    "preview_remove",
    "preview",
    "preview_bridge",
    "remove_preview",
    "build_bridge_plan",
    "apply_shortcuts",
    "apply",
    "apply_bridge",
    "commit",
    "apply_bridge_plan",
    "apply_plan",
    "plan_bridge",
    "remove_shortcuts",
    "remove",
    "remove_bridge",
    "remove_plan",
    "build_document",
]
