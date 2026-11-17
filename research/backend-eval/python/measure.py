"""Stand-in for `/usr/bin/time -v` (not installed): wall time + peak RSS of a child.
Usage: measure.py <stdout_file> -- cmd args..."""
import resource, subprocess, sys, time
out = sys.argv[1]; cmd = sys.argv[sys.argv.index("--") + 1:]
t0 = time.perf_counter()
with open(out, "wb") as fh:
    rc = subprocess.run(cmd, stdout=fh).returncode
wall = time.perf_counter() - t0
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
print(f"exit={rc} wall_s={wall:.3f} user_s={ru.ru_utime:.3f} sys_s={ru.ru_stime:.3f} max_rss_kb={ru.ru_maxrss}", file=sys.stderr)
sys.exit(rc)
