#!/usr/bin/env python3
"""Stand-in for `/usr/bin/time -v` (not installed): wall time + peak RSS of a child.
usage: measure.py <stdout_file> -- cmd args...   (prints a summary to stderr)"""
import resource, subprocess, sys, time
out, cmd = sys.argv[1], sys.argv[3:]
t = time.monotonic()
with open(out, "wb") as f:
    rc = subprocess.run(cmd, stdout=f).returncode
wall = time.monotonic() - t
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
print(f"exit={rc} wall_s={wall:.3f} user_s={ru.ru_utime:.3f} sys_s={ru.ru_stime:.3f} max_rss_kb={ru.ru_maxrss}", file=sys.stderr)
