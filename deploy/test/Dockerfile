# Minimal systemd-enabled Ubuntu image for testing deploy/setup.sh and the
# systemd units against a disposable, fresh-ish Linux box - see
# deploy/test/run_docker_test.sh and the note in deploy/README.md about a
# known instability running this nested inside a WSL2 distro that already
# has its own systemd enabled (boot.systemd=true in wsl.conf). It runs
# fine on a normal Docker host (a real VPS, a CI runner, Docker Desktop's
# default backend, plain Linux) - the conflict is specifically nested
# systemd-in-systemd-in-WSL2, not Docker or systemd themselves.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
        systemd systemd-sysv dbus dbus-user-session \
        sudo python3 python3-venv python3-pip \
        rsync curl ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && (cd /lib/systemd/system/sysinit.target.wants/; for i in *; do [ "$i" = "systemd-tmpfiles-setup.service" ] || rm -f "$i"; done) \
    && rm -f /lib/systemd/system/multi-user.target.wants/* \
    && rm -f /etc/systemd/system/*.wants/* \
    && rm -f /lib/systemd/system/local-fs.target.wants/* \
    && rm -f /lib/systemd/system/sockets.target.wants/*udev* \
    && rm -f /lib/systemd/system/sockets.target.wants/*initctl* \
    && rm -f /lib/systemd/system/basic.target.wants/* \
    && (rm -f /lib/systemd/system/anaconda.target.wants/* || true)

# The repo (minus venv/build artifacts) - see run_docker_test.sh, which
# builds this context from the real repo root.
COPY repo/ /root/mizan/

VOLUME ["/sys/fs/cgroup"]
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
