"""Bounded UTF-8 SSE framing; events are data, never instructions."""
import codecs

from ..types import BackendError


class SSEDecoder:
    def __init__(self, limit=16 * 1024 * 1024):
        self.decoder = codecs.getincrementaldecoder('utf-8')('strict')
        self.buffer, self.data = [], []
        self.limit, self.event_size = limit, 0
        self.after_cr = False

    def feed(self, chunk, final=False):
        try:
            value = self.decoder.decode(chunk, final=final)
        except UnicodeError:
            raise BackendError('mcp:malformed_response') from None
        events = []
        for char in value:
            if self.after_cr:
                self.after_cr = False
                if char == '\n':
                    continue
            if char in '\r\n':
                line, self.buffer = ''.join(self.buffer), []
                self.after_cr = char == '\r'
                if line == '':
                    if self.data:
                        events.append('\n'.join(self.data))
                    self.data, self.event_size = [], 0
                elif line.startswith('data:') or line == 'data':
                    field = line[5:] if line.startswith('data:') else ''
                    self.data.append(field[1:] if field.startswith(' ') else field)
                # Comments, event/id/retry fields do not complete an RPC request.
            else:
                self.buffer.append(char)
                self.event_size += 1
                if self.event_size > self.limit:
                    raise BackendError('mcp:response_too_large')
        return events
