"""Encryption for sync traffic: AES-128-GCM with the key carried in the pairing QR code.

QGIS's Python has no `cryptography` package, but it always ships OpenSSL (the `ssl` module
needs it), so AES-GCM is done by calling OpenSSL's libcrypto through ctypes. Sealed data is
laid out as nonce (12 bytes) || ciphertext || tag (16 bytes), the same as the phone app's
expo-crypto, and travels as base64 text.
"""

import base64
import ctypes
import ctypes.util
import glob
import os
import secrets
import ssl
import sys

KEY_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
# Crockford base32: no I, L, O or U, so a code read aloud or typed is hard to get wrong.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_READ_AS = {"O": "0", "I": "1", "L": "1"}

_EVP_CTRL_GCM_SET_IVLEN = 0x9
_EVP_CTRL_GCM_GET_TAG = 0x10
_EVP_CTRL_GCM_SET_TAG = 0x11


# --- pairing codes ---------------------------------------------------------


def new_key():
    return secrets.token_bytes(KEY_BYTES)


def key_to_code(key):
    """16 key bytes as 26 base32 characters, grouped in fours: 1A2B-3C4D-..."""
    n = int.from_bytes(key, "big") << 2  # 128 bits -> 130, a whole number of 5-bit characters
    chars = "".join(ALPHABET[(n >> (5 * i)) & 31] for i in reversed(range(26)))
    return "-".join(chars[i:i + 4] for i in range(0, 26, 4))


def code_to_key(code):
    """The key for a typed or scanned pairing code, or None if it isn't one."""
    chars = [_READ_AS.get(c, c) for c in (code or "").upper() if c.isalnum()]
    if len(chars) != 26 or any(c not in ALPHABET for c in chars):
        return None
    n = 0
    for c in chars:
        n = (n << 5) | ALPHABET.index(c)
    if n & 3:
        return None
    return (n >> 2).to_bytes(KEY_BYTES, "big")


# --- OpenSSL ---------------------------------------------------------------


def _candidates():
    names = ["libcrypto-3-x64", "libcrypto-3", "libcrypto-1_1-x64", "libcrypto-1_1"]
    found = ctypes.util.find_library("crypto")
    if found:
        names.append(found)
    names += ["libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.dylib"]
    # Next to Python's own ssl module and executable (QGIS bundles on Windows and macOS).
    try:
        import _ssl
        roots = {os.path.dirname(getattr(_ssl, "__file__", "") or ""), os.path.dirname(sys.executable)}
    except ImportError:
        roots = {os.path.dirname(sys.executable)}
    roots |= {os.path.join(r, "..", sub) for r in list(roots) for sub in ("lib", "Frameworks", "bin")}
    for root in roots:
        for pattern in ("libcrypto*.dll", "libcrypto*.so*", "libcrypto*.dylib"):
            names += sorted(glob.glob(os.path.join(root, pattern)))
    return names


def _load_libcrypto():
    for name in _candidates():
        try:
            lib = ctypes.CDLL(name)
            lib.EVP_aes_128_gcm  # noqa: B018 - raises AttributeError if this isn't libcrypto
        except (OSError, AttributeError):
            continue
        p, i = ctypes.c_void_p, ctypes.c_int
        lib.EVP_CIPHER_CTX_new.restype = p
        lib.EVP_CIPHER_CTX_free.argtypes = [p]
        lib.EVP_aes_128_gcm.restype = p
        for fn in (lib.EVP_EncryptInit_ex, lib.EVP_DecryptInit_ex):
            fn.argtypes = [p, p, p, ctypes.c_char_p, ctypes.c_char_p]
        for fn in (lib.EVP_EncryptUpdate, lib.EVP_DecryptUpdate):
            fn.argtypes = [p, p, ctypes.POINTER(i), ctypes.c_char_p, i]
        for fn in (lib.EVP_EncryptFinal_ex, lib.EVP_DecryptFinal_ex):
            fn.argtypes = [p, p, ctypes.POINTER(i)]
        lib.EVP_CIPHER_CTX_ctrl.argtypes = [p, i, i, p]
        return lib
    raise RuntimeError(f"Mappa Mundi Sync needs OpenSSL for encryption and couldn't find it ({ssl.OPENSSL_VERSION}).")


_lib = None


def _crypto():
    global _lib
    if _lib is None:
        _lib = _load_libcrypto()
    return _lib


class Sealer:
    """Seals and opens messages with one pairing key."""

    def __init__(self, key):
        if len(key) != KEY_BYTES:
            raise ValueError("pairing key must be 16 bytes")
        self.key = bytes(key)
        self.lib = _crypto()

    def _run(self, encrypt, nonce, data, aad, tag=None):
        lib = self.lib
        ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise MemoryError("EVP_CIPHER_CTX_new failed")
        init, update = (lib.EVP_EncryptInit_ex, lib.EVP_EncryptUpdate) if encrypt else (lib.EVP_DecryptInit_ex, lib.EVP_DecryptUpdate)
        final = lib.EVP_EncryptFinal_ex if encrypt else lib.EVP_DecryptFinal_ex
        try:
            n = ctypes.c_int(0)
            ok = init(ctx, lib.EVP_aes_128_gcm(), None, None, None)
            ok = ok and lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_GCM_SET_IVLEN, NONCE_BYTES, None)
            ok = ok and init(ctx, None, None, self.key, nonce)
            if ok and aad:
                ok = update(ctx, None, ctypes.byref(n), aad, len(aad))
            out = ctypes.create_string_buffer(len(data) + 16)
            ok = ok and update(ctx, ctypes.addressof(out), ctypes.byref(n), data, len(data))
            written = n.value
            rest = ctypes.addressof(out) + written
            if not ok:
                raise ValueError("AES-GCM setup failed")
            if encrypt:
                if not final(ctx, rest, ctypes.byref(n)):
                    raise ValueError("AES-GCM encryption failed")
                tag_buf = ctypes.create_string_buffer(TAG_BYTES)
                if not lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_GCM_GET_TAG, TAG_BYTES, tag_buf):
                    raise ValueError("AES-GCM tag failed")
                return out.raw[:written + n.value], tag_buf.raw
            tag_buf = ctypes.create_string_buffer(bytes(tag), TAG_BYTES)
            lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_GCM_SET_TAG, TAG_BYTES, tag_buf)
            if final(ctx, rest, ctypes.byref(n)) <= 0:
                raise ValueError("Message failed authentication (wrong pairing code or altered data)")
            return out.raw[:written + n.value], None
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)

    def seal(self, plaintext, aad=b""):
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext, tag = self._run(True, nonce, bytes(plaintext), aad)
        return nonce + ciphertext + tag

    def open(self, sealed, aad=b""):
        if len(sealed) < NONCE_BYTES + TAG_BYTES:
            raise ValueError("Message too short")
        nonce, ciphertext, tag = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:-TAG_BYTES], sealed[-TAG_BYTES:]
        return self._run(False, nonce, ciphertext, aad, tag)[0]

    def seal_text(self, plaintext, aad=b""):
        """Sealed and base64-encoded, as it travels over HTTP."""
        return base64.b64encode(self.seal(plaintext, aad))

    def open_text(self, text, aad=b""):
        try:
            sealed = base64.b64decode(text, validate=True)
        except ValueError as e:
            raise ValueError("Message is not valid base64") from e
        return self.open(sealed, aad)
