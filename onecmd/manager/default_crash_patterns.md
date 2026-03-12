# Crash detection patterns
# Format: name | regex | severity
# Severity: critical, error, warning
# Lines starting with # are comments

segfault | segfault|segmentation fault|sigsegv|signal 11 | critical
oom_killed | oom.?kill|out of memory|cannot allocate memory|killed.*oom | critical
process_exited | (?:process|pid \d+).*(?:exited|died|terminated|killed)|exit(?:ed)?\s+(?:with\s+)?(?:code|status)\s+[1-9]\d* | error
connection_refused | connection refused|econnrefused|connect\(\): connection refused | warning
address_in_use | address already in use|eaddrinuse|bind.*failed | error
systemd_failed | (?:systemd|systemctl).*(?:failed|inactive \(dead\))|Failed to start .+\.service|\.service.*(?:failed|entered failed state) | error
docker_exit | container.*(?:exited|died|stopped|unhealthy)|Exited \(\d+\)\s | error
unhandled_exception | Traceback \(most recent call last\)|Error: .+(?:FATAL|PANIC|unhandled)|FATAL ERROR|java\.lang\..*(?:Error|Exception).*at\s | error
service_crash | (?:nginx|apache|mysql|postgres|redis|mongodb|docker).*(?:crash|fatal|abort|core dump) | critical
