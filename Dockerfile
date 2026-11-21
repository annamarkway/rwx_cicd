# The scorer builds this on linux/amd64 and runs it with no network. The digest
# is the image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

# sqlglot source at the pinned commit: patches are analysed against it.
RUN python - <<PY
import io, tarfile, urllib.request, os, shutil
url = "https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
tarfile.open(fileobj=io.BytesIO(data)).extractall("/opt", filter="data")
os.rename("/opt/sqlglot-${SQLGLOT_SHA}", "/opt/sqlglot")
PY

COPY app /app
RUN chmod -R a+rX /app /opt/sqlglot
ENV PYTHONDONTWRITEBYTECODE=1 BR_SRC=/opt/sqlglot
CMD ["python", "/app/main.py"]
