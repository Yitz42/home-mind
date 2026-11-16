"""Ed25519 signatures in plain Python (RFC 8032, section 5.1), for signed pack manifests (#28). Slow (tens of
milliseconds a signature) and not constant-time: fine for checking one manifest, not for signing on a server that
others can time. Checked against the RFC's test vectors in tests/test_security.py.
"""
import hashlib

p = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
I = pow(2, (p - 1) // 4, p)


def _h(m):
    return hashlib.sha512(m).digest()


def _inv(x):
    return pow(x, p - 2, p)


def _xrecover(y):
    xx = (y * y - 1) * _inv(d * y * y + 1)
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p != 0:
        x = x * I % p
    if x % 2 != 0:
        x = p - x
    return x


By = 4 * _inv(5) % p
Bx = _xrecover(By)
B = (Bx, By, 1, Bx * By % p)       # extended coordinates (X, Y, Z, T)
ZERO = (0, 1, 1, 0)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    Bv = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = Bv - A, D - C, D + C, Bv + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _mul(s, P):
    Q = ZERO
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _eq(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0


def _enc(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % p, P[1] * zi % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _dec(s):
    if len(s) != 32:
        raise ValueError("bad point")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= p:
        raise ValueError("bad point")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = p - x
    P = (x, y, 1, x * y % p)
    if (-x * x + y * y - 1 - d * x * x * y * y) % p != 0:
        raise ValueError("not on the curve")
    return P


def _secret(sk):
    h = _h(sk)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(sk):
    a, _ = _secret(sk)
    return _enc(_mul(a, B))


def sign(sk, msg):
    a, prefix = _secret(sk)
    A = _enc(_mul(a, B))
    r = int.from_bytes(_h(prefix + msg), "little") % L
    R = _enc(_mul(r, B))
    k = int.from_bytes(_h(R + A + msg), "little") % L
    return R + int.to_bytes((r + k * a) % L, 32, "little")


def verify(pk, msg, sig):
    """True if sig is pk's signature of msg."""
    try:
        if len(sig) != 64:
            return False
        A = _dec(pk)
        R = _dec(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if s >= L:
            return False
        k = int.from_bytes(_h(sig[:32] + pk + msg), "little") % L
        return _eq(_mul(s, B), _add(R, _mul(k, A)))
    except (ValueError, TypeError):
        return False
