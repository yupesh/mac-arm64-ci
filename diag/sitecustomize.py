"""Dump every Python stack periodically into the encrypted logs (targeted runs only)."""
import faulthandler
import os

_folder = os.environ.get("STACK_DUMP_DIR")
if _folder:
    _out = open(os.path.join(_folder, f"stacks-{os.getpid()}.txt"), "a")
    faulthandler.dump_traceback_later(10, repeat=True, file=_out)
