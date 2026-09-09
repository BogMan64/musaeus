import subprocess

def get_duration(path):
    # ruleid: duration-ffprobe-outside-duration-module
    subprocess.run(["ffprobe", "-show_entries", "format=duration", str(path)])

def unrelated(path):
    # ok: duration-ffprobe-outside-duration-module
    subprocess.run(["ffprobe", "-show_entries", "stream=sample_rate", str(path)])
