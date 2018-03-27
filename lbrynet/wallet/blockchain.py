import os
import logging

from twisted.internet import threads, defer

from lbryum.util import hex_to_int, int_to_hex, rev_hex
from lbryum.hashing import hash_encode, Hash, PoWHash
from .stream import StreamController
from .constants import blockchain_params, HEADER_SIZE

log = logging.getLogger(__name__)


class BlockchainHeaders:

    def __init__(self, path, chain='lbrycrd_main'):
        self.path = path
        self.chain = chain
        self.max_target = blockchain_params[chain]['max_target']
        self.target_timespan = blockchain_params[chain]['target_timespan']
        self.genesis_bits = blockchain_params[chain]['genesis_bits']

        self._on_change_controller = StreamController()
        self.on_changed = self._on_change_controller.stream

        self._size = None
        self._write_lock = defer.DeferredLock()

        if not os.path.exists(path):
            with open(path, 'wb'):
                pass

    def sync_read_length(self):
        return os.path.getsize(self.path) / HEADER_SIZE

    def __len__(self):
        if self._size is None:
            self._size = self.sync_read_length()
        return self._size

    def sync_read_header(self, height):
        if 0 <= height < len(self):
            with open(self.path, 'rb') as f:
                f.seek(height * HEADER_SIZE)
                return f.read(HEADER_SIZE)

    def __getitem__(self, height):
        assert not isinstance(height, slice),\
            "Slicing of header chain has not been implemented yet."
        header = self.sync_read_header(height)
        return self._deserialize(height, header)

    @defer.inlineCallbacks
    def connect(self, start, headers):
        yield self._write_lock.acquire()
        try:
            yield threads.deferToThread(self._sync_connect, start, headers)
        finally:
            self._write_lock.release()

    def _sync_connect(self, start, headers):
        previous_header = None
        for header in self._iterate_headers(start, headers):
            height = header['block_height']
            if previous_header is None and height > 0:
                previous_header = self[height-1]
            self._verify_header(height, header, previous_header)
            previous_header = header

        with open(self.path, 'r+b') as f:
            f.seek(start * HEADER_SIZE)
            f.write(headers)
            f.truncate()

        _old_size = self._size
        self._size = self.sync_read_length()
        change = self._size - _old_size
        log.info('saved {} header blocks'.format(change))
        self._on_change_controller.add(change)

    def _iterate_headers(self, height, headers):
        assert len(headers) % HEADER_SIZE == 0
        for idx in range(len(headers) / HEADER_SIZE):
            start, end = idx * HEADER_SIZE, (idx + 1) * HEADER_SIZE
            header = headers[start:end]
            yield self._deserialize(height+idx, header)

    def _verify_header(self, height, header, previous_header):
        previous_hash = self._hash_header(previous_header)
        assert previous_hash == header['prev_block_hash'], \
            "prev hash mismatch: {} vs {}".format(previous_hash, header['prev_block_hash'])

        bits, target = self._calculate_lbry_next_work_required(height, previous_header, header)
        assert bits == header['bits'], \
            "bits mismatch: {} vs {} (hash: {})".format(
            bits, header['bits'], self._hash_header(header))

        _pow_hash = self._pow_hash_header(header)
        assert int('0x' + _pow_hash, 16) <= target, \
            "insufficient proof of work: {} vs target {}".format(
            int('0x' + _pow_hash, 16), target)

    @staticmethod
    def _serialize(header):
        return ''.join([
            int_to_hex(header['version'], 4),
            rev_hex(header['prev_block_hash']),
            rev_hex(header['merkle_root']),
            rev_hex(header['claim_trie_root']),
            int_to_hex(int(header['timestamp']), 4),
            int_to_hex(int(header['bits']), 4),
            int_to_hex(int(header['nonce']), 4)
        ])

    @staticmethod
    def _deserialize(height, header):
        return {
            'version': hex_to_int(header[0:4]),
            'prev_block_hash': hash_encode(header[4:36]),
            'merkle_root': hash_encode(header[36:68]),
            'claim_trie_root': hash_encode(header[68:100]),
            'timestamp': hex_to_int(header[100:104]),
            'bits': hex_to_int(header[104:108]),
            'nonce': hex_to_int(header[108:112]),
            'block_height': height
        }

    def _hash_header(self, header):
        if header is None:
            return '0' * 64
        return hash_encode(Hash(self._serialize(header).decode('hex')))

    def _pow_hash_header(self, header):
        if header is None:
            return '0' * 64
        return hash_encode(PoWHash(self._serialize(header).decode('hex')))

    def _calculate_lbry_next_work_required(self, height, first, last):
        """ See: lbrycrd/src/lbry.cpp """

        if height == 0:
            return self.genesis_bits, self.max_target

        # bits to target
        if self.chain != 'lbrycrd_regtest':
            bits = last['bits']
            bitsN = (bits >> 24) & 0xff
            assert 0x03 <= bitsN <= 0x1f, \
                "First part of bits should be in [0x03, 0x1d], but it was {}".format(hex(bitsN))
            bitsBase = bits & 0xffffff
            assert 0x8000 <= bitsBase <= 0x7fffff, \
                "Second part of bits should be in [0x8000, 0x7fffff] but it was {}".format(bitsBase)

        # new target
        retargetTimespan = self.target_timespan
        nActualTimespan = last['timestamp'] - first['timestamp']

        nModulatedTimespan = retargetTimespan + (nActualTimespan - retargetTimespan) // 8

        nMinTimespan = retargetTimespan - (retargetTimespan // 8)
        nMaxTimespan = retargetTimespan + (retargetTimespan // 2)

        # Limit adjustment step
        if nModulatedTimespan < nMinTimespan:
            nModulatedTimespan = nMinTimespan
        elif nModulatedTimespan > nMaxTimespan:
            nModulatedTimespan = nMaxTimespan

        # Retarget
        bnPowLimit = _ArithUint256(self.max_target)
        bnNew = _ArithUint256.SetCompact(last['bits'])
        bnNew *= nModulatedTimespan
        bnNew //= nModulatedTimespan
        if bnNew > bnPowLimit:
            bnNew = bnPowLimit

        return bnNew.GetCompact(), bnNew._value


class _ArithUint256:
    """ See: lbrycrd/src/arith_uint256.cpp """

    def __init__(self, value):
        self._value = value

    def __str__(self):
        return hex(self._value)

    @staticmethod
    def fromCompact(nCompact):
        """Convert a compact representation into its value"""
        nSize = nCompact >> 24
        # the lower 23 bits
        nWord = nCompact & 0x007fffff
        if nSize <= 3:
            return nWord >> 8 * (3 - nSize)
        else:
            return nWord << 8 * (nSize - 3)

    @classmethod
    def SetCompact(cls, nCompact):
        return cls(cls.fromCompact(nCompact))

    def bits(self):
        """Returns the position of the highest bit set plus one."""
        bn = bin(self._value)[2:]
        for i, d in enumerate(bn):
            if d:
                return (len(bn) - i) + 1
        return 0

    def GetLow64(self):
        return self._value & 0xffffffffffffffff

    def GetCompact(self):
        """Convert a value into its compact representation"""
        nSize = (self.bits() + 7) // 8
        nCompact = 0
        if nSize <= 3:
            nCompact = self.GetLow64() << 8 * (3 - nSize)
        else:
            bn = _ArithUint256(self._value >> 8 * (nSize - 3))
            nCompact = bn.GetLow64()
        # The 0x00800000 bit denotes the sign.
        # Thus, if it is already set, divide the mantissa by 256 and increase the exponent.
        if nCompact & 0x00800000:
            nCompact >>= 8
            nSize += 1
        assert (nCompact & ~0x007fffff) == 0
        assert nSize < 256
        nCompact |= nSize << 24
        return nCompact

    def __mul__(self, x):
        # Take the mod because we are limited to an unsigned 256 bit number
        return _ArithUint256((self._value * x) % 2 ** 256)

    def __ifloordiv__(self, x):
        self._value = (self._value // x)
        return self

    def __gt__(self, x):
        return self._value > x
