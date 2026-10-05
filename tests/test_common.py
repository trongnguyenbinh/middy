import io

import numpy as np

from common import EOF_HDR, HDR, emit, read_frame, write_frame


def test_frame_round_trip_and_eof():
    buf = io.BytesIO()
    write_frame(buf, 1, 2.5, np.arange(4, dtype=np.float64))     # cast to float32 on the wire
    write_frame(buf, 0, 2.75, np.zeros(0, np.float32))
    buf.write(EOF_HDR)
    buf.seek(0)
    stream, t_end, t_sent, x = read_frame(buf)
    assert (stream, t_end) == (1, 2.5) and t_sent > 0 and x.dtype == np.float32 and x.tolist() == [0, 1, 2, 3]
    assert read_frame(buf)[3].size == 0
    assert read_frame(buf) is None                               # EOF header
    assert read_frame(io.BytesIO(b"\x00" * (HDR.size - 1))) is None   # truncated header


def test_emit_writes_one_json_line_keeping_unicode():
    out = io.StringIO()
    emit({"type": "final", "text": "nhập kho"}, out)
    assert out.getvalue() == '{"type": "final", "text": "nhập kho"}\n'
