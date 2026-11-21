"""Build step: unpack sqlglot at the pinned commit into /opt/sqlglot (text files only)."""
import io, os, tarfile, urllib.request

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DEST = "/opt/sqlglot"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz", timeout=120).read()
os.makedirs(DEST)
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not m.isfile():
            continue
        rel = parts[1]
        if rel.endswith((".gz", ".png", ".jpg", ".svg", ".parquet")):
            continue
        path = os.path.join(DEST, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(tar.extractfile(m).read())
for dp, dn, fn in os.walk(DEST):
    os.chmod(dp, 0o755)
    for f in fn:
        os.chmod(os.path.join(dp, f), 0o644)
