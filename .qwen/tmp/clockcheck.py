import subprocess, time

out = subprocess.run(
    ["docker", "exec", "texas_security_company_manager-web-1", "date", "-u", "+%s"],
    capture_output=True, text=True,
)
before = int(time.time())
container = int(out.stdout.strip())
after = int(time.time())
print("host_before", before, "container", container, "host_after", after)
print("drift_container_minus_host_seconds", container - (before + after) // 2)
