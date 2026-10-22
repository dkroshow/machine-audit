"""Lossless compact keys; non-inode identities occupy a separate SQL type."""

UINT64 = 1 << 64


def inode_key(value):
    if value is None:
        return None
    text = str(value)
    try:
        number = int(text)
    except ValueError:
        number = -1
    if 0 <= number < UINT64 and str(number) == text:
        return number if number < (1 << 63) else number - UINT64
    # SQLite preserves this as TEXT even in an INTEGER-affinity column. It
    # cannot collide with any signed representation of a native uint64 inode.
    # Negative partial-stream keys also use this namespace, not just fixtures.
    return 'fixture:' + text


def source_id(value):
    if value is None:
        return None
    if isinstance(value, int):
        return str(value if value >= 0 else value + UINT64)
    if isinstance(value, str) and value.startswith('fixture:'):
        return value[8:]
    raise ValueError('invalid_stored_inode_key')
