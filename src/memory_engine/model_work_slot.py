"""Nonblocking interprocess background-inference slot, separate from SQLite locks."""
from contextlib import contextmanager
import os


@contextmanager
def background_slot(home):
    # Contains one sentinel byte only, never model inputs, vectors or credentials.
    with (home/'embedding-worker.lock').open('a+b') as stream:
        if stream.tell()==0:
            stream.write(b'\0');stream.flush()
        stream.seek(0);locked=False
        try:
            if os.name=='nt':
                import msvcrt
                try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);locked=True
                except OSError:pass
            else:
                import fcntl
                try:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);locked=True
                except BlockingIOError:pass
            yield locked
        finally:
            if locked:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
