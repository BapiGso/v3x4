#!/usr/bin/env python3
"""Patch a raw BIOS ROM for the v3x4 DXE driver.

The tool intentionally has no third-party dependencies.  It performs the two
operations usually done by hand in UEFITool for this project:

* neutralize matching Intel microcode update blobs in-place;
* insert a generated FFS driver file after the DXE Core FFS file.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import uuid
from pathlib import Path


DXE_CORE_GUID = "D6A2CB7F-6A18-4E2F-B43B-9920A733700A"
DEFAULT_CPUID = "306F2"

FVH_SIGNATURE = b"_FVH"
FVH_SIGNATURE_OFFSET = 0x28
FVH_ATTRIBUTES_OFFSET = 0x2C
FVH_HEADER_LENGTH_OFFSET = 0x30
FVH_REVISION_OFFSET = 0x37
EFI_FVB2_ERASE_POLARITY = 0x00000800

FFS_HEADER_SIZE = 24
FFS_HEADER2_SIZE = 32
FFS_ATTRIB_LARGE_FILE = 0x01
FFS_ATTRIB_CHECKSUM = 0x40
FFS_FIXED_CHECKSUM = 0xAA
EFI_FV_FILETYPE_FFS_PAD = 0xF0


class CliError(RuntimeError):
    """Raised for user-facing command errors."""


@dataclasses.dataclass(frozen=True)
class CpuidPattern:
    raw: str
    value: int
    mask: int
    exact: bool

    def matches(self, cpuid: int) -> bool:
        return (cpuid & self.mask) == self.value

    def label(self) -> str:
        if self.exact:
            return f"{self.value:05X}"
        width = max(1, (self.mask.bit_length() + 3) // 4)
        return f"*{self.value:0{width}X}"


@dataclasses.dataclass(frozen=True)
class MicrocodeEntry:
    offset: int
    total_size: int
    data_size: int
    cpuid: int
    revision: int
    date: int
    processor_flags: int


@dataclasses.dataclass(frozen=True)
class FfsFile:
    offset: int
    size: int
    aligned_end: int
    header_size: int
    guid: uuid.UUID
    file_type: int
    attributes: int


@dataclasses.dataclass(frozen=True)
class FirmwareVolume:
    index: int
    offset: int
    length: int
    header_length: int
    erase_byte: int
    checksum_ok: bool
    files: tuple[FfsFile, ...]
    free_start: int | None
    free_kind: str

    @property
    def end(self) -> int:
        return self.offset + self.length

    @property
    def free_size(self) -> int:
        if self.free_start is None:
            return 0
        return self.end - self.free_start


@dataclasses.dataclass(frozen=True)
class FfsBlob:
    guid: uuid.UUID
    data: bytes
    size: int
    aligned_size: int


@dataclasses.dataclass(frozen=True)
class InsertTarget:
    fv: FirmwareVolume
    insert_offset: int
    anchor: FfsFile | None


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def read_u16(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def read_u32(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def read_u64(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 8], "little")


def sum8(data: bytes | bytearray) -> int:
    return sum(data) & 0xFF


def checksum8(data: bytes | bytearray) -> int:
    return (-sum8(data)) & 0xFF


def sum16(data: bytes | bytearray) -> int:
    total = 0
    for offset in range(0, len(data) - 1, 2):
        total = (total + read_u16(data, offset)) & 0xFFFF
    if len(data) % 2:
        total = (total + data[-1]) & 0xFFFF
    return total


def checksum32_is_zero(data: bytes | bytearray) -> bool:
    if len(data) % 4:
        return False
    total = 0
    for offset in range(0, len(data), 4):
        total = (total + read_u32(data, offset)) & 0xFFFFFFFF
    return total == 0


def parse_guid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise CliError(f"invalid GUID: {value}") from exc


def parse_offset(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise CliError(f"invalid offset: {value}") from exc


def parse_cpuid_pattern(value: str) -> CpuidPattern:
    raw = value.strip()
    text = raw.lower().removeprefix("0x").replace("_", "")
    if not text or any(ch not in "0123456789abcdef" for ch in text):
        raise CliError(f"invalid CPUID: {value}")
    if len(text) > 8:
        raise CliError(f"CPUID is wider than 32 bits: {value}")

    parsed = int(text, 16)
    if parsed > 0xFFFFFFFF:
        raise CliError(f"CPUID is wider than 32 bits: {value}")

    exact = len(text) > 4
    mask = 0xFFFFFFFF if exact else (1 << (4 * len(text))) - 1
    return CpuidPattern(raw=raw, value=parsed & mask, mask=mask, exact=exact)


def parse_cpuid_patterns(values: list[str] | None) -> list[CpuidPattern]:
    selected = values if values else [DEFAULT_CPUID]
    return [parse_cpuid_pattern(value) for value in selected]


def read_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CliError(f"failed to read {path}: {exc}") from exc


def write_file(path: Path, data: bytes | bytearray, force: bool) -> None:
    if path.exists() and not force:
        raise CliError(f"{path} already exists; pass --force to overwrite it")
    try:
        path.write_bytes(bytes(data))
    except OSError as exc:
        raise CliError(f"failed to write {path}: {exc}") from exc


def default_output_path(input_path: Path) -> Path:
    suffix = input_path.suffix or ".rom"
    return input_path.with_name(f"{input_path.stem}.v3x4{suffix}")


def parse_microcode_at(data: bytes | bytearray, offset: int) -> MicrocodeEntry | None:
    if offset < 0 or offset + 48 > len(data):
        return None

    header_version = read_u32(data, offset)
    loader_revision = read_u32(data, offset + 20)
    if header_version != 1 or loader_revision != 1:
        return None

    cpuid = read_u32(data, offset + 12)
    data_size = read_u32(data, offset + 28)
    total_size = read_u32(data, offset + 32)
    reserved = data[offset + 36 : offset + 48]
    if any(reserved):
        return None

    if total_size == 0:
        total_size = 2048
    if data_size == 0:
        data_size = 2000
    if total_size < 2048 or total_size % 1024 != 0:
        return None
    if data_size + 48 > total_size:
        return None
    if offset + total_size > len(data):
        return None

    blob = data[offset : offset + total_size]
    if not checksum32_is_zero(blob):
        return None

    return MicrocodeEntry(
        offset=offset,
        total_size=total_size,
        data_size=data_size,
        cpuid=cpuid,
        revision=read_u32(data, offset + 4),
        date=read_u32(data, offset + 8),
        processor_flags=read_u32(data, offset + 24),
    )


def find_microcodes(data: bytes | bytearray, stride: int) -> list[MicrocodeEntry]:
    if stride not in (1, 4, 16):
        raise CliError("--scan-stride must be 1, 4, or 16")

    entries: list[MicrocodeEntry] = []
    seen: set[int] = set()
    limit = max(0, len(data) - 48 + 1)
    for offset in range(0, limit, stride):
        entry = parse_microcode_at(data, offset)
        if entry is None or entry.offset in seen:
            continue
        seen.add(entry.offset)
        entries.append(entry)
    return entries


def ffs_header_size_and_length(data: bytes | bytearray, offset: int, limit: int) -> tuple[int, int] | None:
    if offset + FFS_HEADER_SIZE > limit:
        return None
    attributes = data[offset + 19]
    size = data[offset + 20] | (data[offset + 21] << 8) | (data[offset + 22] << 16)
    if attributes & FFS_ATTRIB_LARGE_FILE:
        if offset + FFS_HEADER2_SIZE > limit:
            return None
        size = read_u64(data, offset + 24)
        header_size = FFS_HEADER2_SIZE
    else:
        header_size = FFS_HEADER_SIZE
    if size < header_size or offset + size > limit:
        return None
    return header_size, size


def parse_ffs_file(data: bytes | bytearray, offset: int, fv_end: int) -> FfsFile | None:
    parsed = ffs_header_size_and_length(data, offset, fv_end)
    if parsed is None:
        return None
    header_size, size = parsed
    guid = uuid.UUID(bytes_le=bytes(data[offset : offset + 16]))
    return FfsFile(
        offset=offset,
        size=size,
        aligned_end=align_up(offset + size, 8),
        header_size=header_size,
        guid=guid,
        file_type=data[offset + 18],
        attributes=data[offset + 19],
    )


def is_erased(data: bytes | bytearray, start: int, end: int, erase_byte: int) -> bool:
    return all(byte == erase_byte for byte in data[start:end])


def parse_fv_at(data: bytes | bytearray, start: int, index: int) -> FirmwareVolume | None:
    if start < 0 or start + 0x38 > len(data):
        return None
    if data[start + FVH_SIGNATURE_OFFSET : start + FVH_SIGNATURE_OFFSET + 4] != FVH_SIGNATURE:
        return None

    length = read_u64(data, start + 0x20)
    attributes = read_u32(data, start + FVH_ATTRIBUTES_OFFSET)
    header_length = read_u16(data, start + FVH_HEADER_LENGTH_OFFSET)
    revision = data[start + FVH_REVISION_OFFSET]
    if revision not in (2, 3):
        return None
    if header_length < 0x38 or header_length > length:
        return None
    if length <= 0 or start + length > len(data):
        return None

    erase_byte = 0xFF if attributes & EFI_FVB2_ERASE_POLARITY else 0x00
    header = data[start : start + header_length]
    checksum_ok = sum16(header) == 0

    files: list[FfsFile] = []
    fv_end = start + length
    cursor = align_up(start + header_length, 8)
    free_start: int | None = None
    free_kind = "none"

    while cursor + FFS_HEADER_SIZE <= fv_end:
        if is_erased(data, cursor, min(cursor + FFS_HEADER_SIZE, fv_end), erase_byte):
            free_start = cursor
            free_kind = "erased"
            break
        file = parse_ffs_file(data, cursor, fv_end)
        if file is None:
            break
        if file.aligned_end > fv_end:
            break
        files.append(file)
        cursor = file.aligned_end

    if files:
        last_file = files[-1]
        if (
            last_file.file_type == EFI_FV_FILETYPE_FFS_PAD
            and last_file.aligned_end <= fv_end
            and is_erased(data, last_file.aligned_end, fv_end, erase_byte)
        ):
            free_start = last_file.offset
            free_kind = "terminal-pad"

    return FirmwareVolume(
        index=index,
        offset=start,
        length=length,
        header_length=header_length,
        erase_byte=erase_byte,
        checksum_ok=checksum_ok,
        files=tuple(files),
        free_start=free_start,
        free_kind=free_kind,
    )


def find_firmware_volumes(data: bytes | bytearray) -> list[FirmwareVolume]:
    volumes: list[FirmwareVolume] = []
    index = 0
    search_from = 0
    while True:
        signature_at = data.find(FVH_SIGNATURE, search_from)
        if signature_at < 0:
            break
        start = signature_at - FVH_SIGNATURE_OFFSET
        fv = parse_fv_at(data, start, index)
        if fv is not None:
            volumes.append(fv)
            index += 1
        search_from = signature_at + 1
    return volumes


def validate_ffs_blob(raw: bytes) -> FfsBlob:
    if len(raw) < FFS_HEADER_SIZE:
        raise CliError("FFS file is too small")

    parsed = ffs_header_size_and_length(raw, 0, len(raw))
    if parsed is None:
        raise CliError("FFS header size is invalid")
    header_size, size = parsed
    trailing = raw[size:]
    if trailing and not all(byte in (0x00, 0xFF) for byte in trailing):
        raise CliError("FFS file has non-padding bytes after its declared size")

    blob = raw[:size]
    header_for_sum = bytearray(blob[:header_size])
    header_for_sum[17] = 0
    header_for_sum[23] = 0
    if sum8(header_for_sum) != 0:
        raise CliError("FFS header checksum is invalid")

    attributes = blob[19]
    if attributes & FFS_ATTRIB_CHECKSUM:
        if ((sum8(blob[header_size:size]) + blob[17]) & 0xFF) != 0:
            raise CliError("FFS body checksum is invalid")
    elif blob[17] != FFS_FIXED_CHECKSUM:
        raise CliError("FFS fixed checksum byte is invalid")

    return FfsBlob(
        guid=uuid.UUID(bytes_le=blob[:16]),
        data=blob,
        size=size,
        aligned_size=align_up(size, 8),
    )


def ranges_intersect(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def update_dirty_ffs_checksums(image: bytearray, dirty_ranges: list[tuple[int, int]]) -> int:
    updates = 0
    pending = list(dirty_ranges)
    for _ in range(8):
        if not pending:
            break
        new_dirty: list[tuple[int, int]] = []
        volumes = find_firmware_volumes(image)
        for fv in volumes:
            for file in fv.files:
                if not (file.attributes & FFS_ATTRIB_CHECKSUM):
                    continue
                body_range = (file.offset + file.header_size, file.offset + file.size)
                if not any(ranges_intersect(body_range, dirty) for dirty in pending):
                    continue
                new_checksum = checksum8(image[body_range[0] : body_range[1]])
                checksum_offset = file.offset + 17
                if image[checksum_offset] == new_checksum:
                    continue
                image[checksum_offset] = new_checksum
                new_dirty.append((checksum_offset, checksum_offset + 1))
                updates += 1
        pending = new_dirty
    return updates


def select_insert_target(
    image: bytes | bytearray,
    ffs: FfsBlob,
    anchor_guid: uuid.UUID,
    mode: str,
    fv_offset: int | None,
    allow_duplicate: bool,
) -> InsertTarget:
    volumes = find_firmware_volumes(image)
    if not volumes:
        raise CliError("no firmware volumes found; make sure this is a raw ROM/BIOS-region image")

    candidates: list[InsertTarget] = []
    duplicate_locations: list[tuple[FirmwareVolume, FfsFile]] = []
    anchor_locations = 0
    enough_free_without_anchor = 0

    for fv in volumes:
        if fv_offset is not None and fv.offset != fv_offset:
            continue
        if fv.free_start is None or fv.free_size < ffs.aligned_size:
            continue

        enough_free_without_anchor += 1
        for file in fv.files:
            if file.guid == ffs.guid:
                duplicate_locations.append((fv, file))

        if mode == "append":
            candidates.append(InsertTarget(fv=fv, insert_offset=fv.free_start, anchor=None))
            continue

        anchors = [file for file in fv.files if file.guid == anchor_guid]
        anchor_locations += len(anchors)
        for anchor in anchors:
            insert_offset = anchor.aligned_end if mode == "after" else anchor.offset
            if insert_offset <= fv.free_start:
                candidates.append(InsertTarget(fv=fv, insert_offset=insert_offset, anchor=anchor))

    if duplicate_locations and not allow_duplicate:
        where = ", ".join(
            f"FV@0x{fv.offset:X}/file@0x{file.offset:X}" for fv, file in duplicate_locations
        )
        raise CliError(
            f"FFS GUID {ffs.guid} already exists at {where}; pass --allow-duplicate to insert anyway"
        )

    if candidates:
        return candidates[0]

    if fv_offset is not None:
        raise CliError(f"no usable FV found at offset 0x{fv_offset:X}")
    if mode != "append" and anchor_locations == 0:
        raise CliError(
            f"DXE Core anchor {anchor_guid} was not found; run scan, or pass --anchor-guid/--insert append"
        )
    if enough_free_without_anchor == 0:
        raise CliError(f"no FV has {ffs.aligned_size} bytes of trailing free space")
    raise CliError("anchor was found, but there is not enough trailing free space after it")


def insert_ffs(image: bytearray, ffs: FfsBlob, target: InsertTarget) -> None:
    fv = target.fv
    if fv.free_start is None:
        raise CliError("selected FV has no trailing free space")
    if fv.free_size < ffs.aligned_size:
        raise CliError("selected FV does not have enough trailing free space")
    if target.insert_offset > fv.free_start:
        raise CliError("insert offset is after the FV free area")

    insert_offset = target.insert_offset
    shift_end = fv.free_start
    shifted = bytes(image[insert_offset:shift_end])
    image[insert_offset + ffs.aligned_size : shift_end + ffs.aligned_size] = shifted
    image[insert_offset : insert_offset + ffs.size] = ffs.data
    image[insert_offset + ffs.size : insert_offset + ffs.aligned_size] = bytes(
        [fv.erase_byte]
    ) * (ffs.aligned_size - ffs.size)
    image[shift_end + ffs.aligned_size : fv.end] = bytes([fv.erase_byte]) * (
        fv.end - shift_end - ffs.aligned_size
    )


def print_microcode_entries(entries: list[MicrocodeEntry], patterns: list[CpuidPattern]) -> None:
    if not entries:
        print("microcodes: none found")
        return
    print(f"microcodes: {len(entries)} found")
    for entry in entries:
        mark = "*" if any(pattern.matches(entry.cpuid) for pattern in patterns) else " "
        print(
            f"{mark} @0x{entry.offset:08X} cpuid=0x{entry.cpuid:05X} "
            f"rev=0x{entry.revision:08X} date=0x{entry.date:08X} "
            f"size=0x{entry.total_size:X} flags=0x{entry.processor_flags:X}"
        )


def print_firmware_volumes(volumes: list[FirmwareVolume], anchor_guid: uuid.UUID, verbose: bool) -> None:
    if not volumes:
        print("firmware volumes: none found")
        return
    print(f"firmware volumes: {len(volumes)} found")
    for fv in volumes:
        anchors = [file for file in fv.files if file.guid == anchor_guid]
        if not verbose and not anchors:
            continue
        checksum = "ok" if fv.checksum_ok else "bad"
        free = f"0x{fv.free_size:X} {fv.free_kind}" if fv.free_start is not None else "none"
        print(
            f"FV[{fv.index}] @0x{fv.offset:08X} len=0x{fv.length:X} "
            f"files={len(fv.files)} free={free} checksum={checksum}"
        )
        for file in anchors:
            print(
                f"  anchor {file.guid} @0x{file.offset:08X} "
                f"size=0x{file.size:X} type=0x{file.file_type:02X}"
            )
        if verbose:
            for file in fv.files:
                if file.guid == anchor_guid:
                    continue
                print(
                    f"  file {file.guid} @0x{file.offset:08X} "
                    f"size=0x{file.size:X} type=0x{file.file_type:02X}"
                )


def run_scan(args: argparse.Namespace) -> int:
    image = read_file(Path(args.rom))
    patterns = parse_cpuid_patterns(args.cpuid)
    anchor_guid = parse_guid(args.anchor_guid)
    print(f"ROM: {args.rom} ({len(image)} bytes)")
    print("target CPUID patterns: " + ", ".join(pattern.label() for pattern in patterns))
    print_microcode_entries(find_microcodes(image, args.scan_stride), patterns)
    print_firmware_volumes(find_firmware_volumes(image), anchor_guid, args.verbose)
    return 0


def run_patch(args: argparse.Namespace) -> int:
    input_path = Path(args.rom)
    output_path = Path(args.out) if args.out else default_output_path(input_path)
    if input_path.resolve() == output_path.resolve():
        raise CliError("refusing to overwrite the input ROM; choose a different --out path")

    image = bytearray(read_file(input_path))
    patterns = parse_cpuid_patterns(args.cpuid)
    fill_byte = int(args.fill, 16)
    if fill_byte < 0 or fill_byte > 0xFF:
        raise CliError("--fill must be a byte value, for example FF")

    dirty_ranges: list[tuple[int, int]] = []
    if not args.no_microcode:
        entries = find_microcodes(image, args.scan_stride)
        matches = [entry for entry in entries if any(pattern.matches(entry.cpuid) for pattern in patterns)]
        if not matches and not args.allow_missing_microcode:
            raise CliError(
                "no matching microcode found; pass --allow-missing-microcode to continue anyway"
            )
        for entry in matches:
            print(
                f"remove microcode @0x{entry.offset:08X} cpuid=0x{entry.cpuid:05X} "
                f"rev=0x{entry.revision:08X} size=0x{entry.total_size:X}"
            )
            dirty_ranges.append((entry.offset, entry.offset + entry.total_size))
            if not args.dry_run:
                image[entry.offset : entry.offset + entry.total_size] = bytes([fill_byte]) * entry.total_size

        if dirty_ranges and not args.dry_run:
            checksum_updates = update_dirty_ffs_checksums(image, dirty_ranges)
            if checksum_updates:
                print(f"updated {checksum_updates} affected FFS checksum byte(s)")

    if not args.no_insert:
        if not args.ffs:
            raise CliError("--ffs is required unless --no-insert is used")
        ffs = validate_ffs_blob(read_file(Path(args.ffs)))
        anchor_guid = parse_guid(args.anchor_guid)
        fv_offset = parse_offset(args.fv_offset) if args.fv_offset else None
        target = select_insert_target(
            image=image,
            ffs=ffs,
            anchor_guid=anchor_guid,
            mode=args.insert,
            fv_offset=fv_offset,
            allow_duplicate=args.allow_duplicate,
        )
        anchor_text = (
            f" after anchor @0x{target.anchor.offset:08X}" if target.anchor is not None else ""
        )
        print(
            f"insert FFS {ffs.guid} size=0x{ffs.size:X} into "
            f"FV[{target.fv.index}] @0x{target.fv.offset:08X}{anchor_text}; "
            f"free=0x{target.fv.free_size:X}"
        )
        if not args.dry_run:
            insert_ffs(image, ffs, target)

    if args.dry_run:
        print("dry run: no output written")
        return 0

    write_file(output_path, image, args.force)
    print(f"wrote patched ROM: {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove Haswell-EP 306F2 microcode and insert v3x4.ffs into a raw BIOS ROM.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="show microcodes and DXE Core FVs")
    scan.add_argument("rom", help="raw BIOS/BIOS-region ROM backup")
    scan.add_argument(
        "--cpuid",
        action="append",
        help="target CPUID; values with <=4 hex digits are suffix matches, e.g. 06F2",
    )
    scan.add_argument("--anchor-guid", default=DXE_CORE_GUID, help="DXE Core anchor GUID")
    scan.add_argument("--scan-stride", type=int, choices=(1, 4, 16), default=16)
    scan.add_argument("-v", "--verbose", action="store_true", help="list every parsed FFS file")
    scan.set_defaults(func=run_scan)

    patch = subparsers.add_parser("patch", help="patch a ROM and write a new ROM")
    patch.add_argument("rom", help="raw BIOS/BIOS-region ROM backup")
    patch.add_argument("--ffs", help="generated v3x4.ffs to insert")
    patch.add_argument("-o", "--out", help="output ROM path; default is <input>.v3x4.rom")
    patch.add_argument(
        "--cpuid",
        action="append",
        help=f"microcode CPUID to remove; default {DEFAULT_CPUID}",
    )
    patch.add_argument("--fill", default="FF", help="byte used to neutralize microcode blobs")
    patch.add_argument("--anchor-guid", default=DXE_CORE_GUID, help="DXE Core anchor GUID")
    patch.add_argument("--insert", choices=("after", "before", "append"), default="after")
    patch.add_argument("--fv-offset", help="only insert into the FV starting at this offset")
    patch.add_argument("--scan-stride", type=int, choices=(1, 4, 16), default=16)
    patch.add_argument("--allow-duplicate", action="store_true", help="allow inserting an existing FFS GUID")
    patch.add_argument(
        "--allow-missing-microcode",
        action="store_true",
        help="continue if the selected microcode is not found",
    )
    patch.add_argument("--no-microcode", action="store_true", help="skip microcode removal")
    patch.add_argument("--no-insert", action="store_true", help="skip FFS insertion")
    patch.add_argument("--dry-run", action="store_true", help="show planned changes without writing")
    patch.add_argument("--force", action="store_true", help="overwrite an existing output file")
    patch.set_defaults(func=run_patch)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
