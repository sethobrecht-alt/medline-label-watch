"""AES-256-GCM envelope with a PBKDF2-SHA256 key from the passcode.
The browser page (index.html) decrypts the same format with WebCrypto."""
import base64, gzip, hashlib, json, secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITER = 250000


def encrypt_obj(obj, passcode):
    raw = json.dumps(obj, separators=(',', ':')).encode()
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    key = hashlib.pbkdf2_hmac('sha256', passcode.encode(), salt, ITER, 32)
    ct = AESGCM(key).encrypt(iv, gzip.compress(raw, 9), None)
    b = lambda x: base64.b64encode(x).decode()
    return {'v': 1, 'kdf': 'PBKDF2-SHA256', 'iter': ITER, 'salt': b(salt), 'iv': b(iv), 'ct': b(ct)}


def decrypt_obj(env, passcode):
    d = lambda x: base64.b64decode(x)
    key = hashlib.pbkdf2_hmac('sha256', passcode.encode(), d(env['salt']), env['iter'], 32)
    return json.loads(gzip.decompress(AESGCM(key).decrypt(d(env['iv']), d(env['ct']), None)))
