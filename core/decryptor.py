"""Database decryption - SQLCipher 4 decryption logic.
Based on wechat-decrypt project's decrypt_db.py."""
import hashlib
import hmac as hmac_mod
import os
import struct

from Crypto.Cipher import AES

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80  # IV(16) + HMAC(64)
SQLITE_HDR = b"SQLite format 3\x00"
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24
WAL_MAGIC_LITTLE_ENDIAN_CHECKSUM = 0x377F0682
WAL_MAGIC_BIG_ENDIAN_CHECKSUM = 0x377F0683


class WALSnapshotError(RuntimeError):
    """The WAL could not be parsed as one committed SQLite snapshot."""


def _wal_checksum(data, checksum=(0, 0), *, byteorder):
    if len(data) % 8:
        raise WALSnapshotError("wal_checksum_input_invalid")
    endian = "<" if byteorder == "little" else ">"
    words = struct.unpack(f"{endian}{len(data) // 4}I", data)
    s0, s1 = checksum
    for index in range(0, len(words), 2):
        s0 = (s0 + words[index] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[index + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def derive_mac_key(enc_key, salt):
    """Derive HMAC key from encryption key."""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def decrypt_page(enc_key, page_data, pgno):
    """Decrypt a single database page."""
    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]

    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytes(bytearray(SQLITE_HDR + decrypted + b"\x00" * RESERVE_SZ))
    else:
        encrypted = page_data[: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b"\x00" * RESERVE_SZ


def verify_page1(enc_key, page1_data):
    """Verify page 1 HMAC to confirm key correctness."""
    salt = page1_data[:SALT_SZ]
    mac_key = derive_mac_key(enc_key, salt)
    hmac_data = page1_data[SALT_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored_hmac = page1_data[PAGE_SZ - HMAC_SZ : PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def decrypt_database(db_path, out_path, enc_key_hex):
    """Decrypt an entire database file.

    Args:
        db_path: Encrypted database file path.
        out_path: Decrypted output path.
        enc_key_hex: Hex-encoded encryption key.

    Returns:
        int: Number of pages decrypted, 0 on failure.
    """
    enc_key = bytes.fromhex(enc_key_hex)
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ

    if file_size < PAGE_SZ:
        return 0

    # Verify key
    with open(db_path, "rb") as f:
        page1 = f.read(PAGE_SZ)

    if not verify_page1(enc_key, page1):
        return 0

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if page:
                    page = page + b"\x00" * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))

    return total_pages


def decrypt_wal(wal_path, out_path, enc_key_hex):
    """Apply only the last checksum-valid committed SQLite WAL snapshot.

    Frame checksums cover the encrypted page bytes, so validation must happen
    before SQLCipher page decryption.  An uncommitted valid tail is ignored.
    """
    if not os.path.exists(wal_path):
        return 0
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0

    enc_key = bytes.fromhex(enc_key_hex)
    with open(wal_path, "rb") as wf:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        if len(wal_hdr) != WAL_HEADER_SZ:
            raise WALSnapshotError("wal_header_truncated")
        magic, version, page_size = struct.unpack(">III", wal_hdr[:12])
        if magic == WAL_MAGIC_LITTLE_ENDIAN_CHECKSUM:
            checksum_byteorder = "little"
        elif magic == WAL_MAGIC_BIG_ENDIAN_CHECKSUM:
            checksum_byteorder = "big"
        else:
            raise WALSnapshotError("wal_magic_invalid")
        if version != 3_007_000 or page_size != PAGE_SZ:
            raise WALSnapshotError("wal_format_unsupported")
        checksum = _wal_checksum(
            wal_hdr[:24],
            byteorder=checksum_byteorder,
        )
        if checksum != struct.unpack(">II", wal_hdr[24:32]):
            raise WALSnapshotError("wal_header_checksum_invalid")
        wal_salt1, wal_salt2 = struct.unpack(">II", wal_hdr[16:24])
        frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
        frames = []
        last_commit_count = 0
        committed_db_pages = 0
        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno, db_pages, frame_salt1, frame_salt2 = struct.unpack(
                ">IIII", fh[:16]
            )
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            if pgno == 0:
                break
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                break
            checksum = _wal_checksum(
                fh[:8] + ep,
                checksum,
                byteorder=checksum_byteorder,
            )
            if checksum != struct.unpack(">II", fh[16:24]):
                break
            frames.append((pgno, ep))
            if db_pages:
                last_commit_count = len(frames)
                committed_db_pages = db_pages

    if not last_commit_count:
        return 0

    patched = 0
    with open(out_path, "r+b") as df:
        for pgno, encrypted_page in frames[:last_commit_count]:
            dec = decrypt_page(enc_key, encrypted_page, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1
        df.truncate(committed_db_pages * PAGE_SZ)

    return patched
