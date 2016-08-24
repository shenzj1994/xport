#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Read SAS XPORT/XPT files.

Copyright (c) 2016 Michael Selik.
Inspired by Jack Cushman's original 2012 version.
'''

from __future__ import division, print_function
from collections import namedtuple
from datetime import datetime
from functools import partial
from io import BytesIO
import math
import struct


__version__ = (0, 3, 6)

__all__ = ['reader', 'DictReader']



# All "records" are 80 bytes long, padded if necessary.
# Character data are ASCII format.
# Integer data are IBM-style integer format.
# Floating point data are IBM-style double format.



######################################################################
### Reading XPT                                                   ####
######################################################################

Variable = namedtuple('Variable', 'name numeric position size')



def parse_date(timestring):
    '''
    Parse date from XPT formatted string (ex. '16FEB11:10:07:55')
    '''
    text = timestring.decode('ascii')
    return datetime.strptime(text, '%d%b%y:%H:%M:%S')



def ibm_to_ieee(ibm):
    '''
    Translate IBM-format floating point numbers (as bytes) to IEEE float.
    '''
    # IBM mainframe:    sign * 0.mantissa * 16 ** (exponent - 64)
    # Python uses IEEE: sign * 1.mantissa * 2 ** (exponent - 1023)

    # Pad-out to 8 bytes if necessary. We expect 2 to 8 bytes, but
    # there's no need to check; bizarre sizes will cause a struct
    # module unpack error.
    ibm = ibm.ljust(8, b'\x00')

    # parse the 64 bits of IBM float as one 8-byte unsigned long long
    ulong, = struct.unpack('>Q', ibm)

    # IBM: 1-bit sign, 7-bits exponent, 56-bits mantissa
    sign = ulong & 0x8000000000000000
    exponent = (ulong & 0x7f00000000000000) >> 56
    mantissa = ulong & 0x00ffffffffffffff

    if mantissa == 0:
        if ibm[0:1] == b'\x00':
            return 0.0
        elif ibm[0:1] in b'_.ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            return float('nan')
        else:
            raise ValueError('Neither zero nor NaN: %r' % ibm)

    # IBM-format exponent is base 16, so the mantissa can have up to 3
    # leading zero-bits in the binary mantissa. IEEE format exponent
    # is base 2, so we don't need any leading zero-bits and will shift
    # accordingly. This is one of the criticisms of IBM-format, its
    # wobbling precision.
    if ulong & 0x0080000000000000:
        shift = 3
    elif ulong & 0x0040000000000000:
        shift = 2
    elif ulong & 0x0020000000000000:
        shift = 1
    else:
        shift = 0
    mantissa >>= shift

    # clear the 1 bit to the left of the binary point
    # this is implicit in IEEE specification
    mantissa &= 0xffefffffffffffff

    # IBM exponent is excess 64, but we subtract 65, because of the
    # implicit 1 left of the radix point for the IEEE mantissa
    exponent -= 65
    # IBM exponent is base 16, IEEE is base 2, so we multiply by 4
    exponent <<= 2
    # IEEE exponent is excess 1023, but we also increment for each
    # right-shift when aligning the mantissa's first 1-bit
    exponent += shift + 1023

    # IEEE: 1-bit sign, 11-bits exponent, 52-bits mantissa
    # We didn't shift the sign bit, so it's already in the right spot
    ieee = sign | (exponent << 52) | mantissa
    return struct.unpack(">d", struct.pack(">Q", ieee))[0]



def _parse_field(raw, variable):
    if variable.numeric:
        return ibm_to_ieee(raw)
    return raw.rstrip().decode('ISO-8859-1')



class reader(object):
    '''
    Deserialize ``self._fp`` (a ``.read()``-supporting file-like object containing
    an XPT document) to a Python object.

    The returned object is an iterator.
    Each iteration returns an observation from the XPT file.

        with open('example.xpt', 'rb') as f:
            for row in xport.reader(f):
                process(row)
    '''

    def __init__(self, fp):
        self._fp = fp
        try:
            version, os, created, modified = self._read_header()
            self.version = version
            self.os = os
            self.created = created
            self.modified = modified

            namestr_size = self._read_member_header()[-1]
            nvars = self._read_namestr_header()
            self._variables = self._read_namestr_records(nvars, namestr_size)

            self._read_observations_header()
        except UnicodeDecodeError:
            msg = 'Expected a stream of bytes, got {stream}'
            raise TypeError(msg.format(stream=fp))


    @property
    def fields(self):
        return tuple(v.name for v in self._variables)


    @property
    def _row_size(self):
        return sum(v.length for v in self._variables)


    def __iter__(self):
        for obs in self._read_observations(self._variables):
            yield obs


    def _read_header(self):
        # --- line 1 -------------
        fmt = '>48s32s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        prefix, padding = tokens

        if prefix != b'HEADER RECORD*******LIBRARY HEADER RECORD!!!!!!!':
            raise ValueError('Invalid header: %r' % prefix)
        if padding != b'0' * 30:
            raise ValueError('Invalid header: %r' % padding)

        # --- line 2 -------------
        fmt = '>8s8s8s8s8s24s16s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        prefix = tokens[:3]
        version, os, _, created = tokens[3:]

        if prefix != (b'SAS', b'SAS', b'SASLIB'):
            raise ValueError('Invalid header: %r' % prefix)

        version = tuple(int(s) for s in version.split(b'.'))
        created = parse_date(created)

        # --- line 3 -------------
        fmt = '>16s64s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        modified, _ = tokens

        modified = parse_date(modified)

        # ------------------------
        return version, os, created, modified


    def _read_member_header(self):
        # --- line 1 -------------
        fmt = '>48s26s4s2s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        prefix, _, namestr_size, _ = tokens

        if prefix != b'HEADER RECORD*******MEMBER  HEADER RECORD!!!!!!!':
            raise ValueError('Invalid header: %r' % prefix)

        namestr_size = int(namestr_size)

        # --- line 2 -------------
        fmt = '>48s32s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        prefix, _ = tokens

        if prefix != b'HEADER RECORD*******DSCRPTR HEADER RECORD!!!!!!!':
            raise ValueError('Invalid header: %r' % prefix)

        # --- line 3 -------------
        fmt = '>8s8s8s8s8s24s16s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        prefix, dsname, sasdata, version, os, _, created = tokens

        if prefix != b'SAS':
            raise ValueError('Invalid header: %r' % prefix)
        if sasdata != b'SASDATA':
            raise ValueError('Invalid header: %r' % prefix)

        version = tuple(map(int, version.rstrip().split(b'.')))
        created = parse_date(created)

        # --- line 4 -------------
        fmt = '>16s16s40s8s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        modified, _, dslabel, dstype = tokens

        modified = parse_date(modified)

        # ------------------------
        return (dsname, dstype, dslabel,
                version, os,
                created, modified,
                namestr_size)


    def _read_namestr_header(self):
        # --- line 1 -------------
        fmt = '>48s6s4s22s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        prefix, _, number_of_variables, _ = tokens

        if prefix != b'HEADER RECORD*******NAMESTR HEADER RECORD!!!!!!!':
            raise ValueError('Invalid header: %r' % prefix)

        # ------------------------
        return int(number_of_variables)


    def _read_namestr_record(self, size):
        if size == 140:
            fmt = '>hhhh8s40s8shhh2s8shhl52s'
        else:
            assert size == 136
            fmt = '>hhhh8s40s8shhh2s8shhl48s'
        raw = self._fp.read(size)
        chunks = struct.unpack(fmt, raw)
        tokens = [t.rstrip() if isinstance(t, str) else t for t in chunks]

        is_numeric, _, length, number, name, label = tokens[:6]
        format_data = tokens[6:-2]
        position = tokens[-2]

        name = name.decode('ascii').rstrip()
        is_numeric = True if is_numeric == 1 else False

        if is_numeric and (length < 2 or length > 8):
            msg = 'Numerics must be floating points, 2 to 8 bytes long, not %r'
            raise NotImplementedError(msg % length)

        return Variable(name, is_numeric, position, length)


    def _read_namestr_records(self, n, size):
        variables = [self._read_namestr_record(size) for i in range(n)]
        spillover = n * size % 80
        if spillover != 0:
            padding = 80 - spillover
            self._fp.read(padding)
        return variables


    def _read_observations_header(self):
        # --- line 1 -------------
        fmt = '>48s32s'
        raw = self._fp.read(80)
        tokens = tuple(t.rstrip() for t in struct.unpack(fmt, raw))

        prefix, _ = tokens

        if prefix != b'HEADER RECORD*******OBS     HEADER RECORD!!!!!!!':
            raise ValueError('Invalid header: %r' % prefix)


    def _read_observations(self, variables):
        Row = namedtuple('Row', [v.name for v in variables])

        blocksize = sum(v.size for v in variables)
        padding = b' '
        sentinel = padding * blocksize

        count = 0
        while True:
            block = self._fp.read(blocksize)
            if len(block) < blocksize:
                if set(block) != set(padding):
                    raise ValueError('Incomplete record, {!r}'.format(block))
                remainder = count * blocksize % 80
                if remainder and len(block) != 80 - remainder:
                    raise ValueError('Insufficient padding at end of file')
                break
            elif block == sentinel:
                rest = self._fp.read()
                if set(rest) != set(padding):
                    raise NotImplementedError('Cannot read multiple members.')
                if blocksize + len(rest) != 80 - (count * blocksize % 80):
                    raise ValueError('Incorrect padding at end of file')
                break

            count += 1
            chunks = [block[v.position : v.position + v.size] for v in variables]
            yield Row._make(_parse_field(raw, v) for raw, v in zip(chunks, variables))



class DictReader(object):

    def __init__(self, fp):
        self.reader = reader(fp)

    def __iter__(self):
        return (row._asdict() for row in self.reader)



def load(fp):
    '''
    Read and return rows from the XPT-format table stored in a file.

    Deserialize ``fp`` (a ``.read()``-supporting file-like object
    containing an XPT document) to a list of rows. As XPT files are
    encoded in their own special format, the ``fp`` object must be in
    bytes-mode. ``Row`` objects will be namedtuples with attributes
    parsed from the XPT metadata.
    '''
    return list(reader(fp))



def loads(s):
    '''
    Read and return rows from the given XPT data.

    Deserialize ``s`` (a ``bytes`` instance containing an XPT
    document) to a list of rows. ``Row`` objects will be namedtuples
    with attributes parsed from the XPT metadata.
    '''
    return load(BytesIO(s))



def to_numpy(filename):
    '''
    Read a file in SAS XPT format and return a NumPy array.
    '''
    import numpy as np
    with open(filename, 'rb') as f:
        return np.vstack(reader(f))



def to_dataframe(filename):
    '''
    Read a file in SAS XPT format and return a Pandas DataFrame.
    '''
    import pandas as pd
    with open(filename, 'rb') as f:
        xptfile = reader(f)
        return pd.DataFrame(list(xptfile), columns=xptfile.fields)



######################################################################
### Writing XPT                                                   ####
######################################################################

import platform
from io import StringIO



class Overflow(ArithmeticError):
    'Number too large to express'

class Underflow(ArithmeticError):
    'Number too small to express, rounds to zero'



def ieee_to_ibm(ieee):
    '''
    Translate Python floating point numbers to IBM-format (as bytes).
    '''
    # Python uses IEEE: sign * 1.mantissa * 2 ** (exponent - 1023)
    # IBM mainframe:    sign * 0.mantissa * 16 ** (exponent - 64)

    if ieee == 0.0:
        return b'\x00' * 8
    if math.isnan(ieee):
        return b'_' + b'\x00' * 7
    if math.isinf(ieee):
        raise NotImplementedError('Cannot convert infinity')

    bits = struct.pack('>d', ieee)
    ulong, = struct.unpack('>Q', bits)

    sign = (ulong & (1 << 63)) >> 63                    # 1-bit     sign
    exponent = ((ulong & (0x7ff << 52)) >> 52) - 1023   # 11-bits   exponent
    mantissa = ulong & 0x000fffffffffffff               # 52-bits   mantissa/significand

    if exponent > 248:
        raise Overflow('Cannot store magnitude more than ~ 16 ** 63 as IBM-format')
    if exponent < -260:
        raise Underflow('Cannot store magnitude less than ~ 16 ** -65 as IBM-format')

    # IEEE mantissa has an implicit 1 left of the radix:    1.significand
    # IBM mantissa has an implicit 0 left of the radix:     0.significand
    # We must bitwise-or the implicit 1.mmm into the mantissa
    # later we will increment the exponent to account for this change
    mantissa = 0x0010000000000000 | mantissa

    # IEEE exponents are for base 2:    mantissa * 2 ** exponent
    # IBM exponents are for base 16:    mantissa * 16 ** exponent
    # We must divide the exponent by 4, since 16 ** x == 2 ** (4 * x)
    quotient, remainder = divmod(exponent, 4)
    exponent = quotient

    # We don't want to lose information;
    # the remainder from the divided exponent adjusts the mantissa
    mantissa <<= remainder

    # Increment exponent, because of earlier adjustment to mantissa
    # this corresponds to the 1.mantissa vs 0.mantissa implicit bit
    exponent += 1

    # IBM exponents are excess 64
    exponent += 64

    # IBM has 1-bit sign, 7-bits exponent, and 56-bits mantissa.
    # We must shift the sign and exponent into their places.
    sign <<= 63
    exponent <<= 56

    # We lose some precision, but who said floats were perfect?
    return struct.pack('>Q', sign | exponent | mantissa)




######################################################################
### Main                                                          ####
######################################################################

import argparse
import sys



def parse_args(*args, **kwargs):
    if sys.version_info < (3, 0):
        stdin = sys.stdin
    else:
        stdin = sys.stdin.buffer

    parser = argparse.ArgumentParser(description='Read SAS XPORT/XPT files.')
    parser.add_argument('input',
                        type=argparse.FileType('rb'),
                        nargs='?',
                        default=stdin,
                        help='XPORT/XPT file to read, defaults to stdin')
    return parser.parse_args(*args, **kwargs)



if __name__ == '__main__':
    args = parse_args()
    with args.input:
        xpt = reader(args.input)
        print(','.join(xpt.fields))
        for row in xpt:
            print(','.join(map(str, row)))



