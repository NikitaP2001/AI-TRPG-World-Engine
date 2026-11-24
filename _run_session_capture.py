"""Run _run_session.py and capture output to session_output.txt"""
import sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force UTF-8 for stdout
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

from io import StringIO
import contextlib

buf = StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    import _run_session

output = buf.getvalue()
with open("session_output.txt", "w", encoding="utf-8") as f:
    f.write(output)
print(f"Written {len(output)} chars to session_output.txt")
