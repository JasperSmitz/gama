#!/usr/bin/env python3
"""Wall time + peak RSS of a child command (stand-in for /usr/bin/time -v,
which is not installed on this host). Usage: measure.py <cmd...>"""
import resource, subprocess, sys, time
t = time.monotonic()
r = subprocess.run(sys.argv[1:], stdout=subprocess.DEVNULL)
wall = time.monotonic() - t
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
print(f"exit={r.returncode} wall_s={wall:.3f} user_s={ru.ru_utime:.3f} sys_s={ru.ru_stime:.3f} max_rss_kb={ru.ru_maxrss}")
