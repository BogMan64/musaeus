# MUSAEUS — container image for handing the pipeline to someone else.
#
# Pinned to the environment MUSAEUS was actually developed and tested on:
# Debian 12 (bookworm), Python 3.11, ffmpeg 5.1.x, fpcalc 1.5.1. The point
# of the image is that a tester gets those versions rather than whatever
# their distro ships -- several bugs in this project's history were version-
# or flag-specific (ffmpeg consuming stdin, the AAC encoder capping its own
# sample rate), and reproducing a report is impossible without knowing the
# encoder.
FROM python:3.11-slim-bookworm

# ffmpeg/ffprobe: canonicalize, forge, corrupt, albumart, audit
# libchromaprint-tools: provides fpcalc, for AcoustID fingerprinting
# git:    workspace.py shells out to it (musaeus/workspace.py:53), and the
#         interactive console imports that module -- without git the console
#         raises instead of reporting "unknown".
# procps: idle_throttle.py runs `ps -eo pid,ppid,comm` (line 110) to find the
#         encoder children it pauses.
# Both were found by running the test suite inside the image: 8 errors and 1
# failure, all of them these two binaries, none of them a code fault.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libchromaprint-tools \
      git \
      procps \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY musaeus ./musaeus
COPY scripts ./scripts

# Both optional extras are installed deliberately.
#   fuzzy (rapidfuzz) -- neardupe and canonicalize DEGRADE without it rather
#     than failing, so a tester without it would be silently testing a
#     weaker pipeline than the one being reported on.
#   bpm (essentia)    -- without it the BPM stage does not skip, it CRASHES:
#     "essentia not installed" as a StageError, which fails the whole run.
#     Verified by running it: the first image built without this extra
#     finished "with errors" for exactly that reason.
RUN pip install --no-cache-dir ".[fuzzy,bpm]"

# Run as a non-root user whose ids the host can override at build time, so
# files MUSAEUS writes into the bind-mounted vault belong to the person
# running it and not to root. Without this a tester ends up needing sudo to
# delete files the container created in their own music folder.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "$GID" musaeus || true \
 && useradd -u "$UID" -g "$GID" -m -s /bin/bash musaeus \
 && mkdir -p /vault /state \
 && chown -R "$UID:$GID" /vault /state

ENV MUSAEUS_VAULT_ROOT=/vault \
    MUSAEUS_DB_PATH=/state/musaeus.db \
    MUSAEUS_RECOVERY=/state/recovery \
    MUSAEUS_NO_IDLE_THROTTLE=1 \
    PYTHONUNBUFFERED=1

# PYTHONUNBUFFERED matters more here than it looks. stdout is block-buffered
# when it is not a terminal but stderr never is, so without it a container
# log interleaves the two out of order and a crash appears to happen pages
# before the work that caused it. That cost real debugging time on the host.
#
# MUSAEUS_NO_IDLE_THROTTLE is set because the pause-while-the-machine-is-in-use
# feature opens an X11 display (DISPLAY defaults to ":0"), and there is no X
# server in a container. Left unset it would try, and fail, on every check.

# Seed the settings file the setup wizard looks for.
#
# needs_setup() (musaeus/setup/wizard.py) tests only for
# ~/.config/musaeus/settings.env -- it does NOT consult the process
# environment, so MUSAEUS_VAULT_ROOT being exported is not enough and every
# command drops into the interactive wizard, which then aborts because a
# container has no one to answer it. Seeding the file satisfies the gate;
# the real configuration still comes from the environment above, because
# config.py reads os.environ directly.
RUN mkdir -p /home/musaeus/.config/musaeus \
 && printf 'MUSAEUS_VAULT_ROOT=/vault\n' > /home/musaeus/.config/musaeus/settings.env \
 && chown -R "$UID:$GID" /home/musaeus/.config

USER musaeus
WORKDIR /vault
ENTRYPOINT ["musaeus"]
CMD ["--help"]
